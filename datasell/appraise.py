"""Turn an identified item + market data into a Buy / Maybe / Pass verdict."""

from __future__ import annotations

import os

from . import db, market
from .scraper import ScrapeError

FEE_RATE = float(os.environ.get("DATASELL_FEE_RATE", "0.15"))     # marketplace fees
MIN_PROFIT = float(os.environ.get("DATASELL_MIN_PROFIT", "15"))   # profit worth the effort
MIN_COMPS = 5


def _money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.2f}"


def verdict(m: dict, cost: float | None = None, confidence: str = "high",
            red_flags: list[str] | None = None) -> dict:
    """Decide from a market check (or local comps) whether to buy.

    Rules (min price defaults to $30):
      PASS  typical sale under the minimum, or sell-through is slow,
            or (with a cost) profit under DATASELL_MIN_PROFIT
      BUY   typical sale at or above the minimum, sell-through fast,
            enough comps, a confident ID and the profit works
      MAYBE everything in between (moderate speed, few comps, unsure ID)
    """
    floor = m.get("min_price") or db.MIN_PRICE
    price = m.get("median_price")
    str_ = m.get("str")
    comps = m.get("sold_count") if m.get("sold_count") is not None else len(m.get("comps") or [])
    reasons: list[str] = []
    score = "BUY"

    def lower(to: str):
        nonlocal score
        order = {"BUY": 2, "MAYBE": 1, "PASS": 0}
        if order[to] < order[score]:
            score = to

    if price is None and m.get("source") == "local":
        lower("MAYBE")
        reasons.append("Couldn't reach eBay and none of your saved sales match. Check the eBay links by hand.")
    elif price is None:
        lower("PASS" if comps == 0 else "MAYBE")
        reasons.append("No sold comps found. Try a broader search." if comps == 0
                       else "Couldn't work out a typical price.")
    elif price < floor:
        share = m.get("share_over_min")
        lower("PASS")
        msg = f"Usually sells for {_money(price)}, under your ${floor:g} minimum"
        if share:
            msg += f" (only {share:.0%} of sales were ${floor:g}+)"
        reasons.append(msg + ".")
    else:
        reasons.append(f"Typical sale {_money(price)} (middle half {_money(m.get('p25_price'))}–"
                       f"{_money(m.get('p75_price'))}).")

    if str_ is None:
        lower("MAYBE")
        reasons.append("Sell-through rate unknown (eBay couldn't be checked).")
    else:
        sold, active = m.get("sold_min_count"), m.get("active_min_count")
        detail = f"{str_:.0%} sell-through at ${floor:g}+ ({sold} sold in ~90 days vs {active} listed)"
        if str_ >= market.FAST_STR:
            reasons.append(f"Fast: {detail}.")
        elif str_ >= market.OK_STR:
            lower("MAYBE")
            reasons.append(f"Moderate: {detail}.")
        else:
            lower("PASS")
            reasons.append(f"Slow: {detail}.")

    if price is not None and 0 < comps < MIN_COMPS:
        lower("MAYBE")
        reasons.append(f"Only {comps} sold comps, so the price is a rough guess.")

    profit = None
    if price is not None:
        net = price * (1 - FEE_RATE)
        if cost is not None:
            profit = round(net - cost, 2)
            if profit < MIN_PROFIT:
                lower("PASS" if profit < MIN_PROFIT / 2 else "MAYBE")
                reasons.append(f"Profit about {_money(profit)} after ~{FEE_RATE:.0%} fees, "
                               f"under your {_money(MIN_PROFIT)} target.")
            else:
                reasons.append(f"Profit about {_money(profit)} after ~{FEE_RATE:.0%} fees.")
        else:
            reasons.append(f"Pay up to {_money(max(0, round(net - MIN_PROFIT, 2)))} to make "
                           f"{_money(MIN_PROFIT)}+ after ~{FEE_RATE:.0%} fees.")

    if confidence == "low":
        lower("MAYBE")
        reasons.append("Not sure about the ID. Check the search below matches the item.")
    for flag in red_flags or []:
        reasons.append(f"Watch out: {flag}")

    return {
        "verdict": score,
        "reasons": reasons,
        "est_price": price,
        "list_price": m.get("p75_price") or price,
        "max_buy": round(max(0, price * (1 - FEE_RATE) - MIN_PROFIT), 2) if price is not None else None,
        "profit": profit,
    }


def run_market(query: str, conn=None, ident: dict | None = None, check=market.check) -> dict:
    """Live eBay check, falling back to the local database if eBay is blocked."""
    try:
        m = check(query)
        if conn is not None:  # the $30+ sold comps also grow the database
            db.save_many(conn, m.get("comps_min") or [])
        m["source"] = "ebay"
        return m
    except ScrapeError as e:
        local = market.local_comps(conn, (ident or {}).get("brand"), (ident or {}).get("category")) \
            if conn is not None else {"count": 0, "median_price": None, "comps": []}
        return {
            "query": query, "source": "local", "error": str(e), "min_price": db.MIN_PRICE,
            "median_price": local["median_price"], "p25_price": None, "p75_price": local.get("p75_price"),
            "sold_count": local["count"], "str": None, "speed": "Unknown", "comps": local["comps"],
            "sold_min_count": None, "active_min_count": None, "share_over_min": None,
            "sold_url": market.search_url(query, True), "active_url": market.search_url(query, False),
        }
