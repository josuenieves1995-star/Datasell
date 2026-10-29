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
