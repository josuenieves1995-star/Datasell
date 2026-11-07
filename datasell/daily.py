"""Daily update: re-scrape every saved search and keep the database current.

Run it once with `python -m datasell daily` (e.g. from cron), or let
`python -m datasell serve` run it automatically every day at DATASELL_DAILY_AT
(default 23:30, the server's local time or DATASELL_TZ).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from statistics import median

from . import db, market
from .scraper import BlockedError, ScrapeError, fetch, parse_result_count, scrape_url

log = logging.getLogger("datasell.daily")

DAILY_AT = os.environ.get("DATASELL_DAILY_AT", "23:30")
TZ = os.environ.get("DATASELL_TZ") or None
# How many brand + item-type sell-through checks to refresh per daily run.
BRAND_CHECKS = int(os.environ.get("DATASELL_BRAND_CHECKS", "10"))

# Only one pull at a time, whether scheduled or started with "Run now".
_run_lock = threading.Lock()


def is_running() -> bool:
    return _run_lock.locked()


def count_active(sold_url: str, min_price: float) -> int | None:
    """Live listings for the same search, for the sell-through rate."""
    return parse_result_count(fetch(market.active_version(sold_url, min_price)))


def run_saved_searches(db_path: str | None = None, trigger: str = "manual",
                       min_price: float | None = None, scrape=scrape_url,
                       delay_between: float = 3.0, active_counter=count_active,
                       brand_check=market.check, brand_checks: int = BRAND_CHECKS) -> dict | None:
    """Scrape all active saved searches. Returns a summary, or None if a run
    is already in progress."""
    if not _run_lock.acquire(blocking=False):
        return None
    try:
        floor = db.MIN_PRICE if min_price is None else min_price
        conn = db.connect(db_path)
        try:
            run_id = db.start_run(conn, trigger)
            searches = db.list_searches(conn, active_only=True)
            known = db.known_item_ids(conn)
            totals = {"added": 0, "updated": 0, "skipped": 0, "below_min": 0}
            errors: list[str] = []
            for i, s in enumerate(searches):
                if i:
                    time.sleep(delay_between)
                try:
                    stats: dict = {}
                    listings = scrape(s["url"], pages=s["pages"], min_price=floor, known_ids=known,
                                      stats=stats)
                    counts = db.save_many(conn, listings, floor)
                    _record_sell_through(conn, s["id"], stats, listings, floor, active_counter)
                    known.update(l["item_id"] for l in listings if l.get("item_id"))
                    for k in totals:
                        totals[k] += counts[k]
                    db.update_search(conn, s["id"], last_run_at=db._now(),
                                     last_added=counts["added"], last_error=None)
                except ScrapeError as e:
                    errors.append(f"{s['name'] or s['url']}: {e}")
                    db.update_search(conn, s["id"], last_run_at=db._now(), last_error=str(e))
            if brand_checks:
                errors += _refresh_brand_checks(conn, brand_check, brand_checks, delay_between)
            try:
                db.backup(conn)
            except OSError as e:
                errors.append(f"Backup to {db.BACKUP_DIR} failed: {e}")
            db.finish_run(conn, run_id, len(searches), totals, errors)
            log.info("Daily pull (%s): %d searches, %d added, %d updated, %d errors",
                     trigger, len(searches), totals["added"], totals["updated"], len(errors))
            return {"run_id": run_id, "searches": len(searches), **totals, "errors": errors}
        finally:
            conn.close()
    finally:
        _run_lock.release()


def _record_sell_through(conn, search_id: int, stats: dict, listings: list[dict],
                         floor: float, active_counter) -> None:
    sold = stats.get("total_results")
    prices = [l["price"] for l in listings if l.get("price") and l["price"] >= floor]
    try:
        active = active_counter(stats["url"], floor) if stats.get("url") else None
    except ScrapeError:
        active = None
    db.update_search(conn, search_id, sold_count=sold, active_count=active,
                     str=market.sell_through(sold, active),
                     median_price=round(median(prices), 2) if prices else None,
                     str_checked_at=db._now())


def _refresh_brand_checks(conn, check, limit: int, delay: float) -> list[str]:
    """Sell-through for the brands that sell most often at $30+ (weekly)."""
    errors = []
    queries = db.stale_market_queries(conn, db.top_brand_combos(conn, limit=limit * 3))[:limit]
    for i, q in enumerate(queries):
        if i:
            time.sleep(delay)
        try:
            m = check(q, all_prices=False)
            db.save_market_check(conn, q, m)
            db.save_many(conn, m.get("comps_min") or [])
        except BlockedError as e:
            errors.append(f"Brand check '{q}': {e}")
            break  # eBay is blocking us; don't keep trying tonight
        except ScrapeError as e:
            errors.append(f"Brand check '{q}': {e}")
    return errors


def _now(tz: str | None) -> datetime:
    return datetime.now(ZoneInfo(tz)) if tz else datetime.now().astimezone()


def _parse_at(at: str) -> tuple[int, int]:
    h, m = at.strip().split(":")
    return int(h), int(m)


def next_run_time(now: datetime, at: str = DAILY_AT) -> datetime:
    h, m = _parse_at(at)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def is_due(now: datetime, last_run_iso: str | None, at: str = DAILY_AT) -> bool:
    """True if no scheduled run has happened since the most recent run time.

    If the app was off at run time, this makes it catch up as soon as it
    starts again instead of waiting for the next night.
    """
    h, m = _parse_at(at)
    slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < slot:
        slot -= timedelta(days=1)  # most recent slot was yesterday's
    if not last_run_iso:
        return True
    last = datetime.fromisoformat(last_run_iso)
    return last.astimezone(now.tzinfo) < slot


class DailyScheduler(threading.Thread):
    """Background thread that runs the daily pull once a day."""

    def __init__(self, db_path: str | None = None, at: str = DAILY_AT, tz: str | None = TZ,
                 check_every: int = 60):
        super().__init__(daemon=True, name="datasell-daily")
        _parse_at(at)  # fail fast on a bad time
        self.db_path, self.at, self.tz, self.check_every = db_path, at, tz, check_every
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        log.info("Daily pull scheduled at %s (%s)", self.at, self.tz or "server local time")
        while not self._stop_event.is_set():
            try:
                conn = db.connect(self.db_path)
                last = db.last_scheduled_run(conn)
                conn.close()
                if is_due(_now(self.tz), last["started_at"] if last else None, self.at):
                    run_saved_searches(self.db_path, trigger="schedule")
            except Exception:  # keep the scheduler alive no matter what
                log.exception("Daily pull failed")
            self._stop_event.wait(self.check_every)
