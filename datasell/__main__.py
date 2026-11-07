"""Command line: python -m datasell <command>

  serve                     run the web app (http://127.0.0.1:5000) + daily auto-update
  add URL [URL ...]         scrape eBay sold URLs into the database
  watch URL [--pages N]     save a search URL for the daily update
  searches                  list saved searches
  daily                     run the daily update now (for cron / Task Scheduler)
  prune                     delete stored sales under the minimum price
  backup [FOLDER]           copy the database + a CSV of solds (default DATASELL_BACKUP_DIR)
  check "QUERY" [--cost X]  is this item worth buying? (price, sell-through, verdict)
  import FILE.html [--url]  parse a saved eBay page
  export [FILE.csv]         write the database to CSV
  stats [--by FIELD]        print median prices grouped by a field
"""

import argparse
import sys

from . import db
from .scraper import ScrapeError, is_search_url, parse_html, scrape_url


def main(argv=None):
    p = argparse.ArgumentParser(prog="datasell", description="Used clothing database from eBay sold listings")
    p.add_argument("--db", default=db.DEFAULT_DB, help="SQLite file (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p.add_argument("--min-price", type=float, default=None,
                   help=f"ignore sales under this price (default: ${db.MIN_PRICE:g}, env DATASELL_MIN_PRICE)")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=5000); s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--no-schedule", action="store_true", help="don't run the daily update in the background")
    a = sub.add_parser("add"); a.add_argument("urls", nargs="+"); a.add_argument("--pages", type=int, default=1)
    a.add_argument("--watch", action="store_true", help="also save search URLs for the daily update")
    w = sub.add_parser("watch"); w.add_argument("url"); w.add_argument("--pages", type=int, default=3); w.add_argument("--name")
    sub.add_parser("searches"); sub.add_parser("daily"); sub.add_parser("prune")
    bk = sub.add_parser("backup"); bk.add_argument("folder", nargs="?")
    ck = sub.add_parser("check"); ck.add_argument("query"); ck.add_argument("--cost", type=float)
    i = sub.add_parser("import"); i.add_argument("files", nargs="+"); i.add_argument("--url")
    e = sub.add_parser("export"); e.add_argument("out", nargs="?", default="-")
    st = sub.add_parser("stats"); st.add_argument("--by", default="brand", choices=db.FILTERABLE + ["vintage"])
    args = p.parse_args(argv)
    if args.min_price is not None:
        db.MIN_PRICE = args.min_price
    if db.restore_if_missing(args.db):
        print(f"Restored your database from the backup in {db.BACKUP_DIR}")

    if args.cmd == "serve":
        import logging
        from .app import create_app, start_scheduler
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
        app = create_app(args.db)
        if not args.no_schedule:
            start_scheduler(app)
            print(f"Daily update runs at {app.config['DAILY_AT']} while this is running.")
        app.run(host=args.host, port=args.port)
        return 0
    if args.cmd == "daily":
        from .daily import run_saved_searches
        r = run_saved_searches(args.db, trigger="schedule")
        if r is None:
            print("An update is already running."); return 1
        print(f"{r['searches']} searches: {r['added']} added, {r['updated']} updated, "
              f"{r['below_min']} under ${db.MIN_PRICE:g} ignored")
        for e in r["errors"]:
            print("  ERROR:", e, file=sys.stderr)
        return 1 if r["errors"] else 0

    conn = db.connect(args.db)
    if args.cmd == "watch":
        if not is_search_url(args.url):
            print("Not an eBay search URL", file=sys.stderr); return 1
        print("Saved." if db.add_search(conn, args.url, args.pages, args.name) else "Already saved.")
        return 0
    if args.cmd == "searches":
        for s in db.list_searches(conn):
            state = "on " if s["active"] else "off"
            print(f"[{s['id']}] {state} pages={s['pages']} last={s['last_run_at'] or 'never'} "
                  f"added={s['last_added'] if s['last_added'] is not None else '-'}  {s['name'] or s['url']}")
        return 0
    if args.cmd == "check":
        from .appraise import run_market, verdict
        m = run_market(args.query, conn)
        v = verdict(m, args.cost)
        print({"BUY": "BUY IT", "MAYBE": "MAYBE", "PASS": "PASS"}[v["verdict"]])
        for r in v["reasons"]:
            print(" -", r)
        if m.get("source") == "local":
            print(f"(eBay unreachable: {m['error']} Used your saved data instead.)")
        return 0
    if args.cmd == "backup":
        where = db.backup(conn, args.folder)
        if not where:
            print("No backup folder. Pass one, or set DATASELL_BACKUP_DIR.", file=sys.stderr)
            return 1
        print(f"Backed up to {where}")
        return 0
    if args.cmd == "prune":
        print(f"Deleted {db.prune_below(conn)} listings under ${db.MIN_PRICE:g}")
        return 0
    if args.cmd == "add":
        failed = 0
        for url in args.urls:
            try:
                if args.watch and is_search_url(url):
                    db.add_search(conn, url, max(args.pages, 1))
                listings = scrape_url(url, pages=args.pages, min_price=db.MIN_PRICE)
                c = db.save_many(conn, listings)
                print(f"{url}\n  found {len(listings)}: {c['added']} added, {c['updated']} updated, "
                      f"{c['below_min']} under ${db.MIN_PRICE:g} ignored")
            except ScrapeError as ex:
                failed += 1
                print(f"{url}\n  ERROR: {ex}", file=sys.stderr)
        return 1 if failed else 0
    if args.cmd == "import":
        for path in args.files:
            with open(path, encoding="utf-8", errors="replace") as f:
                listings = parse_html(f.read(), args.url)
            c = db.save_many(conn, listings)
            print(f"{path}: {len(listings)} found, {c['added']} added, {c['updated']} updated")
        return 0
    if args.cmd == "export":
        rows, _ = db.query(conn, {}, limit=None)
        text = db.to_csv(rows)
        if args.out == "-":
            sys.stdout.write(text)
        else:
            with open(args.out, "w", newline="", encoding="utf-8") as f:
                f.write(text)
            print(f"Wrote {len(rows)} listings to {args.out}")
        return 0
    if args.cmd == "stats":
        s = db.stats(conn, group_by=args.by)
        o = s["overall"]
        print(f"{o['listings']} listings, median ${o['median'] or 0:.2f}, avg ${o['avg'] or 0:.2f}\n")
        print(f"{args.by:<28}{'sold':>6}{'median':>10}{'avg':>10}")
        for g in s["groups"]:
            print(f"{str(g['name'])[:27]:<28}{g['count']:>6}{g['median']:>10.2f}{g['avg']:>10.2f}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
