"""Flask web app: add eBay sold URLs, browse the clothing database, view stats."""

from __future__ import annotations

import base64
import math
import os
import threading

from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for

from . import appraise, daily, db, market, vision
from .scraper import ScrapeError, is_search_url, parse_html, scrape_url

PER_PAGE = 48
EDITABLE = ["title", "price", "sold_date", "brand", "category", "department", "size",
            "color", "material", "style", "condition", "era"]


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("DATASELL_SECRET", "dev-only-change-me")
    app.config["DB_PATH"] = db_path or db.DEFAULT_DB
    app.config["DAILY_AT"] = daily.DAILY_AT
    app.config["SCHEDULER_ON"] = False  # set by start_scheduler()
    app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # two phone photos
    # Swappable in tests.
    app.config["IDENTIFY"] = vision.identify
    app.config["MARKET_CHECK"] = market.check

    def conn():
        if "conn" not in g:
            g.conn = db.connect(app.config["DB_PATH"])
        return g.conn

    @app.teardown_appcontext
    def close(_exc):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    def current_filters() -> dict:
        keys = ["q", "sort", "min_price", "max_price", "vintage", "date_from", "date_to"] + db.FILTERABLE
        return {k: request.args.get(k, "").strip() for k in keys if request.args.get(k, "").strip()}

    @app.context_processor
    def globals_():
        return {"min_price": db.MIN_PRICE, "fast_str": market.FAST_STR, "ok_str": market.OK_STR}

    def run_scrape(urls: list[str], pages: int) -> tuple[dict, list[str]]:
        totals = {"added": 0, "updated": 0, "skipped": 0, "below_min": 0}
        errors = []
        for u in urls:
            try:
                counts = db.save_many(conn(), scrape_url(u, pages=pages, min_price=db.MIN_PRICE))
                for k in totals:
                    totals[k] += counts[k]
            except ScrapeError as e:
                errors.append(f"{u}: {e}")
        return totals, errors

    # ------------------------------------------------------------ pages

    @app.route("/")
    def browse():
        filters = current_filters()
        page = max(1, request.args.get("page", 1, type=int))
        rows, total = db.query(conn(), filters, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
        summary = db.stats(conn(), filters)["overall"]
        args = {k: v for k, v in request.args.items() if k != "page"}
        return render_template(
            "browse.html", rows=rows, total=total, page=page,
            pages=max(1, math.ceil(total / PER_PAGE)), filters=filters,
            facets=db.facet_values(conn()), sorts=db.SORTS, summary=summary, args=args,
        )

    @app.route("/add", methods=["GET", "POST"])
    def add():
        if request.method == "POST":
            pages = max(1, min(10, request.form.get("pages", 1, type=int) or 1))
            html = request.form.get("html", "").strip()
            urls = [u.strip() for u in request.form.get("urls", "").splitlines() if u.strip()]
            if html:
                try:
                    listings = parse_html(html, urls[0] if urls else None)
                    counts = db.save_many(conn(), listings)
                    flash(f"Parsed pasted HTML: {counts['added']} added, {counts['updated']} updated, "
                          f"{counts['below_min']} under ${db.MIN_PRICE:g} ignored.", "ok")
                except ScrapeError as e:
                    flash(str(e), "error")
            elif urls:
                if request.form.get("save_daily"):
                    saved = sum(db.add_search(conn(), u, pages) for u in urls if is_search_url(u))
                    if saved:
                        flash(f"Saved {saved} search(es) for the daily update.", "ok")
                counts, errors = run_scrape(urls, pages)
                flash(f"{counts['added']} added, {counts['updated']} updated, "
                      f"{counts['below_min']} under ${db.MIN_PRICE:g} ignored, from {len(urls)} URL(s).",
                      "ok" if not errors else "warn")
                for e in errors:
                    flash(e, "error")
            else:
                flash("Enter at least one eBay URL or paste page HTML.", "error")
            return redirect(url_for("add"))
        return render_template("add.html")

    # ------------------------------------------------------------ daily updates

    def start_background_run() -> bool:
        if daily.is_running():
            return False
        threading.Thread(target=daily.run_saved_searches,
                         args=(app.config["DB_PATH"], "manual"), daemon=True).start()
        return True

    @app.route("/searches", methods=["GET", "POST"])
    def searches():
        if request.method == "POST":
            url = request.form.get("url", "").strip()
            pages = max(1, min(10, request.form.get("pages", 3, type=int) or 3))
            if not is_search_url(url) or "ebay." not in url:
                flash("That doesn't look like an eBay search URL.", "error")
            elif db.add_search(conn(), url, pages, request.form.get("name", "").strip() or None):
                flash("Search saved. It will be pulled in the next daily update.", "ok")
            else:
                flash("That search is already saved.", "warn")
            return redirect(url_for("searches"))
        nxt = daily.next_run_time(daily._now(daily.TZ), app.config["DAILY_AT"])
        return render_template("searches.html", searches=db.list_searches(conn()), presets=market.PRESET_CATEGORIES,
                               runs=db.recent_runs(conn()), next_run=nxt,
                               scheduler_on=app.config["SCHEDULER_ON"], running=daily.is_running(),
                               daily_at=app.config["DAILY_AT"])

    @app.post("/searches/<int:search_id>/toggle")
    def toggle_search(search_id: int):
        row = conn().execute("SELECT active FROM searches WHERE id = ?", (search_id,)).fetchone()
        if row:
            db.update_search(conn(), search_id, active=0 if row["active"] else 1)
        return redirect(url_for("searches"))

    @app.post("/searches/<int:search_id>/delete")
    def delete_search(search_id: int):
        db.delete_search(conn(), search_id)
        flash("Search removed.", "ok")
        return redirect(url_for("searches"))

    @app.post("/searches/run")
    def run_now():
        if start_background_run():
            flash("Pulling all saved searches now. Refresh in a minute to see the results.", "ok")
        else:
            flash("An update is already running.", "warn")
        return redirect(url_for("searches"))

    @app.post("/searches/preset")
    def add_preset():
        cat = request.form.get("category", "")
        names = dict((cid, label) for label, cid in market.PRESET_CATEGORIES)
        if cat not in names:
            flash("Pick a category.", "error")
            return redirect(url_for("searches"))
        keyword = request.form.get("keyword", "").strip()
        url = market.search_url(keyword, True, db.MIN_PRICE, category=cat)
        name = f"{keyword} · {names[cat]}" if keyword else names[cat]
        if db.add_search(conn(), url, 3, name):
            flash(f"Saved “{name}” for daily updates.", "ok")
        else:
            flash("That search is already saved.", "warn")
        return redirect(url_for("searches"))

    # ------------------------------------------------------------ photo check

    def _thumb(jpeg: bytes) -> str:
        small = vision.prepare_image(jpeg, max_edge=420, quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(small).decode()

    def _parse_cost(raw: str | None) -> float | None:
        try:
            return round(float(raw), 2) if raw not in (None, "") else None
        except ValueError:
            return None

    def _evaluate(query: str, ident: dict, cost: float | None) -> tuple[dict, dict]:
        """Market check + verdict. Falls back to the broad query if the exact one has no comps."""
        check = app.config["MARKET_CHECK"]
        m = appraise.run_market(query, conn(), ident, check=check)
        broad = (ident.get("broad_query") or "").strip()
        if m.get("source") == "ebay" and not m.get("median_price") and broad and broad != query:
            m = appraise.run_market(broad, conn(), ident, check=check)
            m["note"] = f"No sales for “{query}”, so these are comps for “{broad}”."
        v = appraise.verdict(m, cost, ident.get("confidence", "high"), ident.get("red_flags"))
        m.pop("comps_min", None)
        return m, v

    def _save_result(appraisal_id: int | None, query: str, ident: dict, cost, m: dict, v: dict, **extra):
        fields = dict(ident=ident, query=m.get("query") or query, cost=cost, verdict=v["verdict"],
                      reasons=v["reasons"], est_price=v["est_price"], list_price=v["list_price"],
                      max_buy=v["max_buy"], profit=v["profit"], str=m.get("str"), market=m, **extra)
        if appraisal_id:
            db.update_appraisal(conn(), appraisal_id, **fields)
            return appraisal_id
        return db.add_appraisal(conn(), **fields)

    @app.route("/check", methods=["GET", "POST"])
    def check_item():
        if request.method == "GET":
            return render_template("check.html", history=db.list_appraisals(conn(), 20),
                                   api_ok=vision.api_configured())
        cost = _parse_cost(request.form.get("cost"))
        query = request.form.get("query", "").strip()
        front = request.files.get("front")
        if query and not (front and front.filename):
            ident = {"search_query": query, "confidence": "high", "red_flags": []}
            m, v = _evaluate(query, ident, cost)
            return redirect(url_for("check_result", appraisal_id=_save_result(None, query, ident, cost, m, v)))
        if not (front and front.filename):
            flash("Add a photo of the item (and ideally its tag), or type what it is.", "error")
            return redirect(url_for("check_item"))
        try:
            front_jpeg = vision.prepare_image(front.read())
            tag = request.files.get("tag")
            tag_jpeg = vision.prepare_image(tag.read()) if tag and tag.filename else None
            ident = app.config["IDENTIFY"](front_jpeg, tag_jpeg, request.form.get("hint", "").strip())
        except vision.IdentifyError as e:
            flash(str(e), "error")
            return redirect(url_for("check_item"))
        query = ident.get("search_query", "")
        m, v = _evaluate(query, ident, cost)
        aid = _save_result(None, query, ident, cost, m, v, front_thumb=_thumb(front_jpeg),
                           tag_thumb=_thumb(tag_jpeg) if tag_jpeg else None)
        return redirect(url_for("check_result", appraisal_id=aid))

    @app.get("/check/<int:appraisal_id>")
    def check_result(appraisal_id: int):
        a = db.get_appraisal(conn(), appraisal_id)
        if not a:
            abort(404)
        return render_template("check_result.html", a=a, m=a["market"], ident=a["ident"])

    @app.post("/check/<int:appraisal_id>/recheck")
    def recheck(appraisal_id: int):
        a = db.get_appraisal(conn(), appraisal_id)
        if not a:
            abort(404)
        query = request.form.get("query", "").strip() or a["query"]
        cost = _parse_cost(request.form.get("cost")) if "cost" in request.form else a["cost"]
        # The seller confirmed the search, so the ID is no longer in doubt.
        ident = dict(a["ident"], search_query=query, broad_query="", confidence="high")
        m, v = _evaluate(query, ident, cost)
        _save_result(appraisal_id, query, a["ident"], cost, m, v)
        return redirect(url_for("check_result", appraisal_id=appraisal_id))

    @app.post("/check/<int:appraisal_id>/delete")
    def delete_check(appraisal_id: int):
        db.delete_appraisal(conn(), appraisal_id)
        return redirect(url_for("check_item"))

    # ------------------------------------------------------------ fast sellers

    @app.get("/fast")
    def fast_sellers():
        show_all = request.args.get("all") == "1"
        rows = []
        for s in db.list_searches(conn()):
            if s["str"] is not None:
                rows.append({"name": s["name"] or s["url"], "url": s["url"], "kind": "Saved search",
                             "sold": s["sold_count"], "active": s["active_count"], "str": s["str"],
                             "median": s["median_price"], "checked": s["str_checked_at"]})
        for c in db.market_checks(conn()):
            if c["str"] is not None:
                rows.append({"name": c["query"], "url": market.search_url(c["query"], True, db.MIN_PRICE),
                             "kind": "Brand", "sold": c["sold_min_count"], "active": c["active_min_count"],
                             "str": c["str"], "median": c["median_price"], "checked": c["checked_at"]})
        hidden = 0
        if not show_all:
            kept = [r for r in rows if r["str"] >= market.OK_STR]
            hidden = len(rows) - len(kept)
            rows = kept
        rows.sort(key=lambda r: -r["str"])
        return render_template("fast.html", rows=rows, hidden=hidden, show_all=show_all,
                               speed=market.speed_label)

    @app.route("/stats")
    def stats_page():
        filters = current_filters()
        group_by = request.args.get("group_by", "brand")
        min_count = max(1, request.args.get("min_count", 1, type=int))
        s = db.stats(conn(), filters, group_by=group_by, min_count=min_count)
        return render_template("stats.html", s=s, filters=filters, facets=db.facet_values(conn()),
                               group_options=db.FILTERABLE + ["vintage"], min_count=min_count)

    @app.route("/listing/<int:listing_id>", methods=["GET", "POST"])
    def listing(listing_id: int):
        row = db.get(conn(), listing_id)
        if not row:
            abort(404)
        if request.method == "POST":
            updates = {f: request.form.get(f, "").strip() or None for f in EDITABLE}
            if updates["price"] is not None:
                try:
                    updates["price"] = float(updates["price"])
                except ValueError:
                    flash("Price must be a number.", "error")
                    return redirect(url_for("listing", listing_id=listing_id))
            updates["vintage"] = 1 if request.form.get("vintage") else 0
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn().execute(f"UPDATE listings SET {sets} WHERE id = ?", list(updates.values()) + [listing_id])
            conn().commit()
            flash("Saved.", "ok")
            return redirect(url_for("listing", listing_id=listing_id))
        return render_template("listing.html", r=db.row_to_dict(row), editable=EDITABLE)

    @app.post("/listing/<int:listing_id>/delete")
    def delete_listing(listing_id: int):
        db.delete(conn(), listing_id)
        flash("Listing deleted.", "ok")
        return redirect(url_for("browse"))

    @app.route("/export.csv")
    def export_csv():
        rows, _ = db.query(conn(), current_filters(), limit=None)
        return Response(db.to_csv(rows), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=datasell_listings.csv"})

    # ------------------------------------------------------------ JSON API

    @app.get("/api/listings")
    def api_listings():
        limit = min(500, request.args.get("limit", 100, type=int))
        offset = request.args.get("offset", 0, type=int)
        rows, total = db.query(conn(), current_filters(), limit=limit, offset=offset)
        return jsonify(total=total, listings=[db.row_to_dict(r) for r in rows])

    @app.get("/api/stats")
    def api_stats():
        return jsonify(db.stats(conn(), current_filters(), group_by=request.args.get("group_by", "brand")))

    @app.post("/api/scrape")
    def api_scrape():
        data = request.get_json(silent=True) or {}
        urls = data.get("urls") or ([data["url"]] if data.get("url") else [])
        if data.get("html"):
            try:
                counts = db.save_many(conn(), parse_html(data["html"], urls[0] if urls else None))
                return jsonify(counts=counts, errors=[])
            except ScrapeError as e:
                return jsonify(error=str(e)), 400
        if not urls:
            return jsonify(error="Provide 'urls' (list), 'url', or 'html'."), 400
        counts, errors = run_scrape(urls, max(1, min(10, int(data.get("pages", 1)))))
        return jsonify(counts=counts, errors=errors)

    @app.post("/api/run-daily")
    def api_run_daily():
        return jsonify(started=start_background_run())

    @app.template_filter("money")
    def money(v, currency=None):
        if v is None:
            return "—"
        sym = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "C$", "AUD": "A$"}.get(currency or "USD", "")
        return f"{sym}{v:,.2f}"

    return app


def start_scheduler(app: Flask) -> daily.DailyScheduler:
    sched = daily.DailyScheduler(app.config["DB_PATH"], at=app.config["DAILY_AT"])
    sched.start()
    app.config["SCHEDULER_ON"] = True
    return sched


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
