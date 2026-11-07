"""Tests for photo checks, sell-through and verdicts."""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

from datasell import appraise, daily, db, market, vision
from datasell.app import create_app
from datasell.scraper import BlockedError


def page(count, prices, start=500000000000):
    """A fake eBay sold/active search page with a result count and cards."""
    cards = "".join(
        f'<li class="s-card" data-listingid="{start + i}"><div class="s-card__caption"><span>Sold  Oct {i % 27 + 1}, 2026</span></div>'
        f'<a class="su-link" href="https://www.ebay.com/itm/{start + i}"><div class="s-card__title"><span>Patagonia Retro X Fleece Jacket Mens L #{i}</span></div></a>'
        f'<span class="s-card__price">${p:.2f}</span></li>'
        for i, p in enumerate(prices))
    return (f'<html><body><h1 class="srp-controls__count-heading"><span class="BOLD">{count:,}</span> results for patagonia</h1>'
            f'<ul class="srp-results">{cards}</ul></body></html>')


def fake_ebay(sold_min=60, active_min=40, sold_all=200, prices_all=None, prices_min=None):
    prices_all = prices_all or [25, 40, 55, 60, 65, 70, 80, 95]
    prices_min = prices_min or [40, 55, 60, 65, 70, 80, 95]
    calls = []

    def fetch_page(url, session=None):
        calls.append(url)
        q = parse_qs(urlparse(url).query)
        if "LH_Sold" not in q:
            return page(active_min, [])
        if "_udlo" in q:
            return page(sold_min, prices_min)
        return page(sold_all, prices_all, start=600000000000)
    return fetch_page, calls


def jpeg(color="red", size=(2400, 1800)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------- market

def test_search_urls():
    sold = market.search_url("carhartt jacket", True, 30)
    q = parse_qs(urlparse(sold).query)
    assert q["LH_Sold"] == ["1"] and q["_udlo"] == ["30"] and q["_sacat"] == ["11450"]
    assert q["LH_ItemCondition"] == ["3000"] and q["_sop"] == ["13"]
    active = parse_qs(urlparse(market.active_version(sold)).query)
    assert "LH_Sold" not in active and "_sop" not in active and active["_udlo"] == ["30"]
    assert market.sell_through(60, 40) == 0.6 and market.sell_through(0, 0) is None
    assert [market.speed_label(x) for x in (0.6, 0.3, 0.1, None)] == ["Fast", "Moderate", "Slow", "Unknown"]


def test_market_check():
    fetch_page, calls = fake_ebay()
    m = market.check("patagonia retro x", fetch_page=fetch_page)
    assert len(calls) == 3
    assert (m["sold_min_count"], m["active_min_count"], m["sold_count"]) == (60, 40, 200)
    assert m["str"] == 0.6 and m["speed"] == "Fast"
    assert m["median_price"] == 62.5 and m["share_over_min"] == 0.875
    m2 = market.check("x", all_prices=False, fetch_page=fake_ebay()[0])
    assert m2["sold_count"] is None and m2["median_price"] == 65


# ---------------------------------------------------------------- verdict

def mk(**kw):
    base = dict(min_price=30, median_price=60, p25_price=45, p75_price=75, str=0.6,
                sold_min_count=60, active_min_count=40, sold_count=200, share_over_min=0.9, comps=[1] * 8)
    base.update(kw)
    return base


def test_verdicts():
    v = appraise.verdict(mk(), cost=8)
    assert v["verdict"] == "BUY" and v["profit"] == 43.0 and v["list_price"] == 75
    assert appraise.verdict(mk(median_price=22, share_over_min=0.2))["verdict"] == "PASS"
    assert appraise.verdict(mk(str=0.1))["verdict"] == "PASS"
    assert appraise.verdict(mk(str=0.35))["verdict"] == "MAYBE"
    assert appraise.verdict(mk(str=None))["verdict"] == "MAYBE"
    assert appraise.verdict(mk(sold_count=3))["verdict"] == "MAYBE"
    assert appraise.verdict(mk(), confidence="low")["verdict"] == "MAYBE"
    assert appraise.verdict(mk(), cost=40)["verdict"] == "MAYBE"    # $11 profit: under $15 target
    assert appraise.verdict(mk(), cost=50)["verdict"] == "PASS"
    assert appraise.verdict(mk(median_price=None, sold_count=0, comps=[]))["verdict"] == "PASS"
    no_cost = appraise.verdict(mk())
    assert no_cost["max_buy"] == 36.0 and any("Pay up to $36.00" in r for r in no_cost["reasons"])
    assert any("fake" in r for r in appraise.verdict(mk(), red_flags=["fake logo"])["reasons"])


def test_run_market_falls_back_to_local(tmp_path):
    conn = db.connect(str(tmp_path / "x.db"))
    db.save_many(conn, [{"item_id": str(i), "title": "Carhartt Jacket", "price": p, "brand": "Carhartt",
                         "category": "Jacket"} for i, p in enumerate([40, 50, 60])])

    def blocked(q):
        raise BlockedError("bot check")
    m = appraise.run_market("carhartt detroit", conn, {"brand": "carhartt", "category": "Jacket"}, check=blocked)
    assert m["source"] == "local" and m["median_price"] == 50 and m["sold_count"] == 3
    assert appraise.verdict(m)["verdict"] == "MAYBE"  # can't be BUY without sell-through
    m = appraise.run_market("unknown thing", conn, {"brand": "Nobody"}, check=blocked)
    v = appraise.verdict(m)
    assert v["verdict"] == "MAYBE" and "Couldn't reach eBay" in v["reasons"][0]


# ---------------------------------------------------------------- vision

IDENT = {
    "brand": "Patagonia", "sub_brand_or_line": "Retro-X", "item_type": "fleece jacket", "category": "Jacket",
    "department": "Men", "size": "L", "color": "Tan", "material": "Polyester fleece", "made_in": "USA",
    "rn_or_style_number": "RN51884", "era": "1990s", "vintage": True, "notable_features": ["deep pile"],
    "condition_notes": "light pilling", "red_flags": [], "search_query": "Patagonia Retro X Fleece Jacket",
    "broad_query": "Patagonia Fleece Jacket", "listing_title": "Vintage 90s Patagonia Retro X Fleece Jacket Mens L USA",
    "confidence": "high",
}


def test_prepare_image_downscales():
    out = vision.prepare_image(jpeg())
    img = Image.open(io.BytesIO(out))
    assert max(img.size) == vision.MAX_EDGE and img.format == "JPEG"
    with pytest.raises(vision.IdentifyError):
        vision.prepare_image(b"not an image")


def test_identify_request_and_parse():
    sent = {}

    def create(**kw):
        sent.update(kw)
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=json.dumps(IDENT))])
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    out = vision.identify(b"front", b"tag", hint="made in usa", client=client)
    assert out["brand"] == "Patagonia"
    assert sent["model"] == "claude-opus-5-5" and sent["fallbacks"] == "default"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    images = [b for b in sent["messages"][0]["content"] if b["type"] == "image"]
    assert len(images) == 2 and "made in usa" in sent["messages"][0]["content"][-1]["text"]

    def refuse(**kw):
        return SimpleNamespace(stop_reason="refusal", content=[])
    with pytest.raises(vision.IdentifyError):
        vision.identify(b"f", None, client=SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=refuse))))


def test_identify_with_real_sdk_against_fake_server():
    """The installed anthropic SDK accepts our request and parses the reply."""
    import anthropic
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["beta"] = self.headers.get("anthropic-beta")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps(IDENT)}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = anthropic.Anthropic(api_key="test", base_url=f"http://127.0.0.1:{server.server_port}",
                                     http_client=anthropic.DefaultHttpxClient(trust_env=False), max_retries=0)
        out = vision.identify(vision.prepare_image(jpeg()), None, client=client)
    finally:
        server.shutdown()
    assert out["search_query"] == "Patagonia Retro X Fleece Jacket"
    assert seen["path"].startswith("/v1/messages") and "server-side-fallback-2026-07-01" in seen["beta"]
    assert seen["body"]["fallbacks"] == "default" and seen["body"]["output_config"]["effort"] == "medium"


# ---------------------------------------------------------------- daily sell-through

def test_daily_records_sell_through_and_brand_checks(tmp_path):
    path = str(tmp_path / "d.db")
    c = db.connect(path)
    db.add_search(c, "https://www.ebay.com/sch/i.html?_nkw=patagonia&LH_Sold=1", 1, "Patagonia")
    c.close()

    def scrape(url, pages, min_price, known_ids, stats=None):
        stats.update(total_results=80, url=url)
        return [{"item_id": str(i), "title": "Patagonia Retro X Fleece Jacket", "price": p,
                 "brand": "Patagonia", "category": "Jacket"} for i, p in enumerate([40, 60, 80, 90])]
    checked = []

    def brand_check(q, all_prices=True):
        checked.append(q)
        return {"sold_min_count": 30, "active_min_count": 70, "str": 0.3, "median_price_min": 55, "comps_min": []}

    r = daily.run_saved_searches(path, scrape=scrape, delay_between=0,
                                 active_counter=lambda url, floor: 20, brand_check=brand_check)
    assert not r["errors"]
    c = db.connect(path)
    s = db.list_searches(c)[0]
    assert (s["sold_count"], s["active_count"], s["str"], s["median_price"]) == (80, 20, 0.8, 70)
    assert checked == ["Patagonia Jacket"]
    assert db.market_checks(c)[0]["str"] == 0.3
    # Checked this week, so tomorrow's run skips it.
    daily.run_saved_searches(path, scrape=scrape, delay_between=0,
                             active_counter=lambda url, floor: 20, brand_check=brand_check)
    assert checked == ["Patagonia Jacket"]


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE searches (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, name TEXT, "
                "pages INTEGER NOT NULL DEFAULT 3, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, "
                "last_run_at TEXT, last_added INTEGER, last_error TEXT)")
    old.commit()
    old.close()
    c = db.connect(path)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(searches)")}
    assert {"str", "sold_count", "active_count"} <= cols


# ---------------------------------------------------------------- web

@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "web.db"))
    app.testing = True
    app.config["IDENTIFY"] = lambda front, tag, hint="": dict(IDENT)
    app.config["MARKET_CHECK"] = lambda q: market.check(q, fetch_page=fake_ebay()[0])
    return app.test_client()


def test_photo_check_flow(client):
    assert b"Is it worth it?" in client.get("/check").data
    r = client.post("/check", data={"front": (io.BytesIO(jpeg()), "front.jpg"),
                                    "tag": (io.BytesIO(jpeg("white")), "tag.jpg"), "cost": "8"},
                    content_type="multipart/form-data", follow_redirects=True)
    html = r.data.decode()
    assert "Buy it" in html and "60%" in html and "Patagonia" in html and "$45.12" in html
    assert "data:image/jpeg;base64," in html
    # The $30+ comps were added to the database.
    assert client.get("/api/listings").get_json()["total"] == 7
    # Fix the search and re-check with a higher cost.
    r = client.post("/check/1/recheck", data={"query": "patagonia retro x vest", "cost": "50"}, follow_redirects=True)
    assert "Pass" in r.data.decode() and "patagonia retro x vest" in r.data.decode()
    assert b"Recent checks" in client.get("/check").data
    client.post("/check/1/delete")
    assert b"Recent checks" not in client.get("/check").data


def test_text_check_and_errors(client):
    r = client.post("/check", data={"query": "patagonia retro x"}, follow_redirects=True)
    assert "Buy it" in r.data.decode() and "max to pay" in r.data.decode()
    r = client.post("/check", data={}, follow_redirects=True)
    assert b"Add a photo" in r.data
    r = client.post("/check", data={"front": (io.BytesIO(b"junk"), "x.jpg")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert b"use a JPEG or PNG" in r.data


def test_fast_sellers_and_presets(client):
    assert b"Nothing here yet" in client.get("/fast").data
    r = client.post("/searches/preset", data={"category": "57988", "keyword": "Carhartt"}, follow_redirects=True)
    assert "Carhartt · Men&#39;s Coats &amp; Jackets" in r.data.decode() or "Carhartt · Men's Coats" in r.data.decode()
    assert b"_sacat=57988" in r.data
    app_db = db.connect(client.application.config["DB_PATH"])
    db.save_market_check(app_db, "Carhartt Jacket", {"sold_min_count": 70, "active_min_count": 30, "str": 0.7, "median_price_min": 65})
    db.save_market_check(app_db, "Gap Jeans", {"sold_min_count": 5, "active_min_count": 95, "str": 0.05, "median_price_min": 32})
    html = client.get("/fast").data.decode()
    assert "Carhartt Jacket" in html and "70% · Fast" in html and "Gap Jeans" not in html and "1 slow seller hidden" in html
    assert "Gap Jeans" in client.get("/fast?all=1").data.decode()


# ---------------------------------------------------------------- phone storage backups

def test_backup_and_restore(tmp_path):
    live = str(tmp_path / "home" / "datasell.db")
    docs = str(tmp_path / "Documents" / "Datasell")
    import os
    os.makedirs(os.path.dirname(live))
    c = db.connect(live)
    db.save_many(c, [{"item_id": "1", "title": "Carhartt Jacket", "price": 80, "brand": "Carhartt"}])
    assert db.backup(c, docs).endswith("datasell.db")
    assert "Carhartt" in open(os.path.join(docs, "datasell_solds.csv")).read()
    assert db.last_backup(docs)["at"]
    c.close()
    os.remove(live)  # e.g. Termux was reinstalled
    assert db.restore_if_missing(live, docs)
    assert db.query(db.connect(live))[1] == 1
    assert not db.restore_if_missing(live, docs)  # never overwrites a live database


def test_daily_run_writes_backup(tmp_path, monkeypatch):
    docs = str(tmp_path / "Docs")
    monkeypatch.setattr(db, "BACKUP_DIR", docs)
    path = str(tmp_path / "d.db")
    db.connect(path).close()
    daily.run_saved_searches(path, delay_between=0, brand_checks=0)
    import os
    assert os.path.exists(os.path.join(docs, "datasell.db"))


def test_photo_prep_without_pillow(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_pil(name, *a, **kw):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no pillow")
        return real_import(name, *a, **kw)
    small = jpeg(size=(800, 600))
    monkeypatch.setattr(builtins, "__import__", no_pil)
    assert vision.prepare_image(small) == small
    with pytest.raises(vision.IdentifyError):
        vision.prepare_image(b"\x89PNG....")
    with pytest.raises(vision.IdentifyError):
        vision.prepare_image(b"\xff\xd8\xff" + b"0" * 4_000_000)
