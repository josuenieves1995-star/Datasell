"""Market checks: how much something sells for, and how fast.

Sell-through rate (STR) = sales in the last ~90 days / (those sales + live
listings). eBay's sold search covers about 90 days, so for a search we read
the "N results" count on the sold page and on the active page.

STR is measured on the $30+ market (both searches use the minimum price),
because that's the only part of the market we buy for:
  * 60% means for every 10 listings, 6 sold in 90 days: moves fast
  * 20% means most listings sit
"""

from __future__ import annotations

import os
from statistics import median
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

from . import db
from .scraper import fetch, parse_result_count, parse_search_page

# eBay category "Clothing, Shoes & Accessories".
CLOTHING_CATEGORY = os.environ.get("DATASELL_CATEGORY", "11450")
USED_CONDITION = "3000"  # eBay's "Pre-owned" condition filter

# eBay clothing categories for one-tap saved searches.
PRESET_CATEGORIES = [
    ("Men's Coats & Jackets", "57988"),
    ("Men's Sweaters", "11484"),
    ("Men's Hoodies & Sweatshirts", "155183"),
    ("Men's Casual Button-Down Shirts", "57990"),
    ("Men's T-Shirts", "15687"),
    ("Men's Jeans", "11483"),
    ("Men's Pants", "57989"),
    ("Men's Shoes", "93427"),
    ("Women's Coats, Jackets & Vests", "63862"),
    ("Women's Sweaters", "63866"),
    ("Women's Tops", "53159"),
    ("Women's Dresses", "63861"),
    ("Women's Jeans", "11554"),
    ("Women's Pants", "63863"),
    ("Women's Shoes", "3034"),
]

FAST_STR = float(os.environ.get("DATASELL_FAST_STR", "0.5"))
OK_STR = float(os.environ.get("DATASELL_OK_STR", "0.25"))


def search_url(query: str, sold: bool, min_price: float | None = None,
               category: str | None = CLOTHING_CATEGORY, used: bool = True) -> str:
    params = {"_nkw": query, "_ipg": "120"}
    if category:
        params["_sacat"] = category
    if used:
        params["LH_ItemCondition"] = USED_CONDITION
    if sold:
        params.update({"LH_Sold": "1", "LH_Complete": "1", "_sop": "13"})
    if min_price:
        params["_udlo"] = f"{min_price:g}"
    return "https://www.ebay.com/sch/i.html?" + urlencode(params)


def active_version(url: str, min_price: float | None = None) -> str:
    """Turn a sold-search URL into the same search over live listings."""
    p = urlparse(url)
    q = parse_qs(p.query, keep_blank_values=True)
    for k in ("LH_Sold", "LH_Complete", "_sop", "_pgn"):
        q.pop(k, None)
    if min_price and not q.get("_udlo", [""])[0]:
        q["_udlo"] = [f"{min_price:g}"]
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))


def sell_through(sold: int | None, active: int | None) -> float | None:
    if sold is None or active is None or sold + active == 0:
        return None
    return round(sold / (sold + active), 3)


def speed_label(str_: float | None) -> str:
    if str_ is None:
        return "Unknown"
    if str_ >= FAST_STR:
        return "Fast"
    if str_ >= OK_STR:
        return "Moderate"
    return "Slow"


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 2)


def check(query: str, min_price: float | None = None, category: str | None = CLOTHING_CATEGORY,
          used: bool = True, all_prices: bool = True, session: requests.Session | None = None,
          fetch_page=fetch) -> dict:
    """Look up sold comps and $30+ sell-through for a search query.

    With all_prices=True (used for photo checks) it also looks at every sale
    regardless of price, to tell whether this item usually sells for $30+.
    That costs one extra eBay request (3 in total, otherwise 2).
    """
    floor = db.MIN_PRICE if min_price is None else min_price
    session = session or requests.Session()

    sold_url = search_url(query, True, floor, category, used)
    html = fetch_page(sold_url, session)
    sold_min_count = parse_result_count(html)
    comps_min = parse_search_page(html, sold_url)

    active_url = search_url(query, False, floor, category, used)
    active_min_count = parse_result_count(fetch_page(active_url, session))

    comps_all, sold_all_count = comps_min, None
    if all_prices:
        all_url = search_url(query, True, None, category, used)
        html_all = fetch_page(all_url, session)
        sold_all_count = parse_result_count(html_all)
        comps_all = parse_search_page(html_all, all_url)

    prices_all = [c["price"] for c in comps_all if c.get("price")]
    prices_min = [c["price"] for c in comps_min if c.get("price")]
    share = (sum(p >= floor for p in prices_all) / len(prices_all)) if prices_all and all_prices else None
    str_ = sell_through(sold_min_count, active_min_count)

    return {
        "query": query,
        "category": category,
        "min_price": floor,
        "sold_url": sold_url,
        "active_url": active_url,
        "sold_count": sold_all_count,          # all prices, ~90 days (None if not checked)
        "sold_min_count": sold_min_count,      # $30+ sales, ~90 days
        "active_min_count": active_min_count,  # $30+ live listings
        "str": str_,
        "speed": speed_label(str_),
        "median_price": round(median(prices_all), 2) if prices_all else None,
        "median_price_min": round(median(prices_min), 2) if prices_min else None,
        "p25_price": _percentile(prices_all, 0.25),
        "p75_price": _percentile(prices_all, 0.75),
        "share_over_min": round(share, 3) if share is not None else None,
        "comps": comps_all[:24],
        "comps_min": comps_min,
    }


def local_comps(conn, brand: str | None, category: str | None, limit: int = 50) -> dict:
    """Fallback when eBay can't be reached: comps already in the database."""
    if not brand and not category:
        return {"count": 0, "median_price": None, "comps": []}
    clauses, params = [], []
    if brand:
        clauses.append("brand = ? COLLATE NOCASE")
        params.append(brand)
    if category:
        clauses.append("category = ?")
        params.append(category)
    rows = conn.execute(
        f"SELECT * FROM listings WHERE {' AND '.join(clauses)} AND price IS NOT NULL "
        f"ORDER BY sold_date DESC LIMIT ?", params + [limit]).fetchall()
    prices = [r["price"] for r in rows]
    return {
        "count": len(rows),
        "median_price": round(median(prices), 2) if prices else None,
        "p75_price": _percentile(prices, 0.75),
        "comps": [dict(r) for r in rows[:24]],
    }
