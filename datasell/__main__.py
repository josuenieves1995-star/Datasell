"""Command line: python -m datasell <command>

  serve                     run the web app (http://127.0.0.1:5000)
  add URL [URL ...]         scrape eBay sold URLs into the database
  import FILE.html [--url]  parse a saved eBay page
  export [FILE.csv]         write the database to CSV
  stats [--by FIELD]        print median prices grouped by a field
"""

import argparse
import sys

from . import db
from .scraper import ScrapeError, parse_html, scrape_url


def main(argv=None):
    p = argparse.ArgumentParser(prog="datasell", description="Used clothing database from eBay sold listings")
    p.add_argument("--db", default=db.DEFAULT_DB, help="SQLite file (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=5000); s.add_argument("--host", default="127.0.0.1")
    a = sub.add_parser("add"); a.add_argument("urls", nargs="+"); a.add_argument("--pages", type=int, default=1)
    i = sub.add_parser("import"); i.add_argument("files", nargs="+"); i.add_argument("--url")
    e = sub.add_parser("export"); e.add_argument("out", nargs="?", default="-")
    st = sub.add_parser("stats"); st.add_argument("--by", default="brand", choices=db.FILTERABLE + ["vintage"])
    args = p.parse_args(argv)

    if args.cmd == "serve":
        from .app import create_app
        create_app(args.db).run(host=args.host, port=args.port)
        return 0

    conn = db.connect(args.db)
    if args.cmd == "add":
        failed = 0
        for url in args.urls:
            try:
                listings = scrape_url(url, pages=args.pages)
                c = db.save_many(conn, listings)
                print(f"{url}\n  found {len(listings)}: {c['added']} added, {c['updated']} updated")
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
