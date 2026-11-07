from pathlib import Path

import pytest

from datasell import db
from datasell.app import create_app
from datasell.attributes import extract_attributes, guess_category, guess_size
from datasell.scraper import (BlockedError, ensure_sold_filter, item_id_from_url, parse_html,
                              parse_price, parse_sold_date)

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return (FIX / name).read_text()


# ---------------------------------------------------------------- parsing helpers

@pytest.mark.parametrize("text,expected", [
    ("$24.99", (24.99, "USD")),
    ("US $1,045.00", (1045.0, "USD")),
    ("$10.00 to $20.00", (10.0, "USD")),
    ("£12.50", (12.5, "GBP")),
    ("C $30.00", (30.0, "CAD")),
    ("", (None, None)),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Sold  Oct 12, 2026", "2026-10-12"),
    ("Sold Sept 3, 2026", "2026-09-03"),
    ("Sold 3 Sep 2026", "2026-09-03"),
    ("nothing here", None),
])
def test_parse_sold_date(text, expected):
    assert parse_sold_date(text) == expected


def test_item_id_and_sold_filter():
    assert item_id_from_url("https://www.ebay.com/itm/Some-Title/298765432109?x=1") == "298765432109"
    assert item_id_from_url("https://www.ebay.com/itm/387654321012?hash=item5a") == "387654321012"
    url = ensure_sold_filter("https://www.ebay.com/sch/i.html?_nkw=levis")
    assert "LH_Sold=1" in url and "LH_Complete=1" in url and "_nkw=levis" in url


# ---------------------------------------------------------------- attributes

@pytest.mark.parametrize("title,size", [
    ("Levi's 501 W32 L30", "32x30"),
    ("Wrangler Jeans 34 x 32", "34x32"),
    ("Nike Hoodie Size XL Black", "XL"),
    ("Women's Dress Size 8", "8"),
    ("Mens Large Flannel", "L"),
    ("L.L. Bean Flannel Shirt XXL", "XXL"),
])
def test_guess_size(title, size):
    assert guess_size(title) == size


@pytest.mark.parametrize("title,cat", [
    ("Vintage Levi's Denim Trucker Jacket", "Jacket"),
    ("Tie Dye Grateful Dead T-Shirt", "T-Shirt"),
    ("Brooks Brothers Button Down Oxford Shirt", "Shirt"),
    ("Cargo Shorts", "Shorts"),
    ("Short Sleeve Tee", "T-Shirt"),
])
def test_guess_category(title, cat):
    assert guess_category(title) == cat


def test_specifics_override_title():
    attrs = extract_attributes(
        "Mystery Brand Red Shirt Size S",
        {"Brand": "Gap", "Size": "M", "Color": "Blue", "Vintage": "No"},
        "Pre-owned: worn")
    assert attrs["brand"] == "Gap" and attrs["size"] == "M" and attrs["color"] == "Blue"
    assert attrs["condition"] == "Pre-owned" and attrs["vintage"] is False


def test_unbranded_falls_back_to_title():
    assert extract_attributes("Carhartt Jacket", {"Brand": "Unbranded"})["brand"] == "Carhartt"


# ---------------------------------------------------------------- page parsing

def test_parse_old_search_layout():
    items = parse_html(load("search_s_item.html"))
    assert len(items) == 2  # "Shop on eBay" placeholder skipped
    levis, pata = items
    assert levis["item_id"] == "387654321012"
    assert levis["url"] == "https://www.ebay.com/itm/387654321012"
    assert levis["title"].startswith("Vintage 90s Levi's")  # "New Listing" stripped
    assert (levis["price"], levis["shipping"], levis["sold_date"]) == (64.99, 8.5, "2026-10-12")
    assert (levis["brand"], levis["category"], levis["size"]) == ("Levi's", "Jeans", "32x30")
    assert levis["vintage"] and levis["era"] == "1990s"
    assert pata["price"] == 1045.0 and pata["shipping"] == 0.0 and pata["department"] == "Women"


def test_parse_new_search_layout():
    items = parse_html(load("search_s_card.html"))
    assert [i["item_id"] for i in items] == ["306111222333", "306444555666"]
    carhartt, nike = items
    assert not carhartt["title"].endswith("tab")
    assert (carhartt["price"], carhartt["shipping"], carhartt["sold_date"]) == (145.0, 15.2, "2026-10-01")
    assert (carhartt["brand"], carhartt["category"], carhartt["size"]) == ("Carhartt", "Jacket", "XL")
    assert nike["price"] == 38.0 and nike["era"] == "2000s" and nike["category"] == "Hoodie"


def test_parse_item_page():
    [item] = parse_html(load("item_page.html"), "https://www.ebay.com/itm/405512345678?trk=1")
    assert item["item_id"] == "405512345678"
    assert item["price"] == 72.0 and item["shipping"] == 6.95
    assert item["sold_date"] == "2026-10-15"
    assert item["brand"] == "Polo Ralph Lauren" and item["size"] == "L"
    assert item["color"] == "Blue" and item["material"] == "100% Cotton"
    assert item["category"] == "Sweater" and item["department"] == "Men"
    assert item["condition"] == "Pre-owned" and item["vintage"] is False
    assert item["image_url"].endswith("s-l1600.jpg")
    assert item["specifics"]["Style"] == "Crewneck"


def test_blocked_page_raises():
    with pytest.raises(BlockedError):
        parse_html(load("blocked.html"))


# ---------------------------------------------------------------- database

@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def test_upsert_dedupes_and_keeps_rich_data(conn):
    [item] = parse_html(load("item_page.html"), "https://www.ebay.com/itm/405512345678")
    assert db.upsert(conn, item) == "added"
    sparse = {"item_id": "405512345678", "title": item["title"], "price": 72.0, "material": None, "specifics": {}}
    assert db.upsert(conn, sparse) == "updated"
    rows, total = db.query(conn)
    assert total == 1 and rows[0]["material"] == "100% Cotton"
    assert db.row_to_dict(rows[0])["specifics"]["Brand"] == "Polo Ralph Lauren"


def test_query_filters_and_stats(conn):
    db.save_many(conn, parse_html(load("search_s_item.html")) + parse_html(load("search_s_card.html")))
    assert db.query(conn)[1] == 4
    assert db.query(conn, {"category": "Jacket"})[1] == 2
    assert db.query(conn, {"vintage": "1"})[1] == 2
    assert db.query(conn, {"max_price": "100"})[1] == 2
    assert db.query(conn, {"q": "carhartt detroit"})[1] == 1
    assert db.query(conn, {"date_from": "2026-10-01"})[1] == 2
    rows, _ = db.query(conn, {"sort": "price_high"})
    assert rows[0]["price"] == 1045.0
    s = db.stats(conn, group_by="category")
    jackets = next(g for g in s["groups"] if g["name"] == "Jacket")
    assert jackets["count"] == 2 and jackets["median"] == 595.0
    assert [t["month"] for t in s["trend"]] == ["2026-08", "2026-09", "2026-10"]
    assert "Carhartt" in db.to_csv(db.query(conn, limit=None)[0])


# ---------------------------------------------------------------- web app

@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "web.db"))
    app.testing = True
    return app.test_client()


def test_web_flow(client):
    assert b"No listings yet" in client.get("/").data
    r = client.post("/add", data={"urls": "", "html": load("search_s_card.html")}, follow_redirects=True)
    assert b"2 added" in r.data
    page = client.get("/?brand=Carhartt").data.decode()
    assert "Carhartt Detroit Jacket" in page and "Center Swoosh" not in page
    assert b"Jacket" in client.get("/stats?group_by=category").data
    api = client.get("/api/listings").get_json()
    assert api["total"] == 2
    lid = api["listings"][0]["id"]
    assert client.get(f"/listing/{lid}").status_code == 200
    client.post(f"/listing/{lid}", data={"title": "Edited", "price": "99", "size": "M"})
    edited = {l["id"]: l for l in client.get("/api/listings").get_json()["listings"]}[lid]
    assert (edited["title"], edited["price"], edited["size"]) == ("Edited", 99.0, "M")
    assert b"must be a number" in client.post(f"/listing/{lid}", data={"title": "x", "price": "abc"}, follow_redirects=True).data
    assert client.get("/export.csv").data.startswith(b"id,item_id")
    client.post(f"/listing/{lid}/delete")
    assert client.get("/api/listings").get_json()["total"] == 1


def test_api_scrape_validation(client):
    assert client.post("/api/scrape", json={}).status_code == 400
    r = client.post("/api/scrape", json={"urls": ["https://example.com/x"]}).get_json()
    assert r["errors"] and "eBay" in r["errors"][0]


# ---------------------------------------------------------------- $30 minimum + daily updates

from datetime import datetime, timezone  # noqa: E402

from datasell import daily, scraper  # noqa: E402


def test_min_price_filter(conn):
    cheap = {"item_id": "1", "title": "Cheap tee", "price": 29.99}
    no_price = {"item_id": "2", "title": "Unknown price"}
    ok = {"item_id": "3", "title": "Good jacket", "price": 30.0}
    counts = db.save_many(conn, [cheap, no_price, ok])
    assert counts["added"] == 1 and counts["below_min"] == 2
    assert db.save_many(conn, [cheap], min_price=0)["added"] == 1
    assert db.prune_below(conn) == 1
    assert [r["item_id"] for r in db.query(conn)[0]] == ["3"]


def test_search_url_gets_min_price_and_newest_sort():
    url = ensure_sold_filter("https://www.ebay.com/sch/i.html?_nkw=levis", 30)
    assert "_udlo=30" in url and "_sop=13" in url
    # A price the user already set in the URL is kept.
    assert "_udlo=50" in ensure_sold_filter("https://www.ebay.com/sch/i.html?_nkw=x&_udlo=50", 30)


def test_scrape_stops_paging_when_nothing_new(monkeypatch):
    calls = []

    def fake_fetch(url, session=None, timeout=20):
        calls.append(url)
        return load("search_s_card.html")

    monkeypatch.setattr(scraper, "fetch", fake_fetch)
    res = scraper.scrape_url("https://www.ebay.com/sch/i.html?_nkw=x", pages=5, delay=0,
                             known_ids={"306111222333", "306444555666"})
    assert len(calls) == 1 and len(res) == 2
    calls.clear()
    scraper.scrape_url("https://www.ebay.com/sch/i.html?_nkw=x", pages=3, delay=0, known_ids=set())
    assert len(calls) == 3


def test_is_due():
    tz = timezone.utc
    at = "23:30"
    before = datetime(2026, 10, 29, 22, 0, tzinfo=tz)
    after = datetime(2026, 10, 29, 23, 45, tzinfo=tz)
    next_morning = datetime(2026, 10, 30, 9, 0, tzinfo=tz)
    assert daily.is_due(before, None, at)                               # never ran: run now
    assert not daily.is_due(before, "2026-10-28T23:30:05+00:00", at)   # last night's ran, wait
    assert daily.is_due(after, "2026-10-28T23:30:05+00:00", at)        # tonight's is due
    assert not daily.is_due(after, "2026-10-29T23:30:05+00:00", at)    # already ran tonight
    # App was off last night (last run two nights ago): catch up in the morning.
    assert daily.is_due(next_morning, "2026-10-28T23:30:05+00:00", at)
    assert not daily.is_due(next_morning, "2026-10-29T23:31:00+00:00", at)
    assert daily.next_run_time(before, at) == datetime(2026, 10, 29, 23, 30, tzinfo=tz)
    assert daily.next_run_time(after, at) == datetime(2026, 10, 30, 23, 30, tzinfo=tz)


def test_run_saved_searches(tmp_path):
    path = str(tmp_path / "d.db")
    c = db.connect(path)
    db.add_search(c, "https://www.ebay.com/sch/i.html?_nkw=a", 2, "A")
    db.add_search(c, "https://www.ebay.com/sch/i.html?_nkw=b", 2, "B")
    assert not db.add_search(c, "https://www.ebay.com/sch/i.html?_nkw=a")  # no duplicates
    c.close()
    seen = []

    def fake_scrape(url, pages, min_price, known_ids, stats=None):
        seen.append((url, pages, min_price))
        if url.endswith("=b"):
            raise scraper.BlockedError("bot check")
        return parse_html(load("search_s_item.html")) + [{"item_id": "9", "title": "Cheap", "price": 5.0}]

    r = daily.run_saved_searches(path, "schedule", scrape=fake_scrape, delay_between=0,
                                 brand_checks=0)
    assert seen[0] == ("https://www.ebay.com/sch/i.html?_nkw=a", 2, 30.0)
    assert (r["searches"], r["added"], r["below_min"], len(r["errors"])) == (2, 2, 1, 1)
    c = db.connect(path)
    a, b = db.list_searches(c)
    assert a["last_added"] == 2 and a["last_error"] is None and "bot check" in b["last_error"]
    assert db.last_scheduled_run(c)["added"] == 2
    c.close()


def test_searches_page(client):
    assert b"No saved searches" in client.get("/searches").data
    r = client.post("/searches", data={"url": "https://www.ebay.com/sch/i.html?_nkw=nike", "pages": "2"},
                    follow_redirects=True)
    assert b"Search saved" in r.data and b"_nkw=nike" in r.data
    assert b"doesn" in client.post("/searches", data={"url": "https://example.com"}, follow_redirects=True).data
    # Pasting HTML on the Add page applies the $30 minimum.
    r = client.post("/add", data={"html": load("search_s_card.html")}, follow_redirects=True)
    assert b"2 added" in r.data
