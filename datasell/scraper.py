"""Fetch and parse eBay sold listings.

Supports two kinds of URLs:
  * Sold/completed search results, e.g.
    https://www.ebay.com/sch/i.html?_nkw=levis+501&LH_Sold=1&LH_Complete=1
  * Single item pages, e.g. https://www.ebay.com/itm/123456789012

eBay has shipped two search-card layouts (the older `li.s-item` and the newer
`li.s-card`); both are handled. If eBay blocks the request (bot check), you can
save the page from your browser and pass the HTML to `parse_html` instead.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag

from .attributes import extract_attributes

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CURRENCY_SYMBOLS = [
    ("US $", "USD"), ("C $", "CAD"), ("AU $", "AUD"), ("CA $", "CAD"), ("A $", "AUD"),
    ("GBP", "GBP"), ("EUR", "EUR"), ("USD", "USD"), ("CAD", "CAD"), ("AUD", "AUD"),
    ("£", "GBP"), ("€", "EUR"), ("$", "USD"),
]

BLOCK_MARKERS = ("Pardon Our Interruption", "splashui/challenge", "captcha", "Checking your browser")


class ScrapeError(Exception):
    pass


class BlockedError(ScrapeError):
    """eBay served a bot check instead of the page."""


# ---------------------------------------------------------------- helpers

def _text(el: Tag | None) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def parse_price(text: str | None) -> tuple[float | None, str | None]:
    """'$24.99' -> (24.99, 'USD'); '$10.00 to $20.00' -> (10.0, 'USD')."""
    if not text:
        return None, None
    currency = None
    for sym, code in CURRENCY_SYMBOLS:
        if sym in text:
            currency = code
            break
    m = re.search(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?", text)
    if not m:
        return None, currency
    value = float(m.group(1).replace(",", "") + "." + (m.group(2) or "0"))
    return value, currency


def parse_shipping(text: str | None) -> float | None:
    if not text:
        return None
    if re.search(r"free", text, re.I):
        return 0.0
    value, _ = parse_price(text)
    return value


def parse_sold_date(text: str | None) -> str | None:
    """Find a date like 'Oct 12, 2024' or '12 Oct 2024' and return ISO format."""
    if not text:
        return None
    m = re.search(r"([A-Z][a-z]{2,8})\.? (\d{1,2}),? (\d{4})", text)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2}) ([A-Z][a-z]{2,8})\.? (\d{4})", text)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)[:3]} {m.group(3)}", "%d %b %Y").date().isoformat()
        except ValueError:
            pass
    return None


def item_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,15})", url)
    if m:
        return m.group(1)
    q = parse_qs(urlparse(url).query)
    for key in ("item", "itm", "hash"):
        for v in q.get(key, []):
            m = re.search(r"(\d{9,15})", v)
            if m:
                return m.group(1)
    return None


def clean_item_url(url: str) -> str:
    """Strip tracking query params from item links."""
    iid = item_id_from_url(url)
    if iid:
        host = urlparse(url).netloc or "www.ebay.com"
        return f"https://{host}/itm/{iid}"
    return url


def is_search_url(url: str) -> bool:
    p = urlparse(url)
    return "/sch/" in p.path or "_nkw" in p.query or "/b/" in p.path


def ensure_sold_filter(url: str, min_price: float | None = None) -> str:
    """Make sure a search URL only returns sold, completed listings.

    Also asks eBay for sales at or above `min_price` (`_udlo`) and for the
    most recently ended first (`_sop=13`), unless the URL already sets them.
    """
    p = urlparse(url)
    q = parse_qs(p.query, keep_blank_values=True)
    q["LH_Sold"] = ["1"]
    q["LH_Complete"] = ["1"]
    if min_price and not q.get("_udlo", [""])[0]:
        q["_udlo"] = [f"{min_price:g}"]
    q.setdefault("_sop", ["13"])
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))


def with_page(url: str, page: int) -> str:
    p = urlparse(url)
    q = parse_qs(p.query, keep_blank_values=True)
    q["_pgn"] = [str(page)]
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))


# ---------------------------------------------------------------- search pages

def _card_parts(card: Tag) -> dict:
    """Extract raw fields from either the s-item or s-card layout."""
    title_el = card.select_one(".s-item__title, .s-card__title")
    # Drop hidden "New Listing" / "Opens in a new window" spans.
    if title_el:
        for junk in title_el.select(".LIGHT_HIGHLIGHT, .clipped, .s-card__new-listing"):
            junk.decompose()
    title = _text(title_el)
    title = re.sub(r"^(New Listing|NEW LISTING)\s*", "", title)
    title = re.sub(r"\s*Opens in a new window or tab\s*$", "", title, flags=re.I)

    link_el = card.select_one("a.s-item__link, a.su-link, a.s-card__link, a[href*='/itm/']")
    href = link_el.get("href") if link_el else None

    price_el = card.select_one(".s-item__price, .s-card__price")
    img_el = card.select_one(".s-item__image-wrapper img, .s-item__image img, .s-card__image img, img")
    img = None
    if img_el:
        img = img_el.get("src") or img_el.get("data-src")
        if img and img.startswith("data:"):
            img = img_el.get("data-src")

    cond_el = card.select_one(".s-item__subtitle .SECONDARY_INFO, .s-card__subtitle")
    ship_el = card.select_one(".s-item__shipping, .s-item__logisticsCost")

    # Sold date lives in a caption / tag; fall back to scanning the whole card.
    sold_el = card.select_one(
        ".s-item__caption--signal, .s-item__title--tag, .s-item__caption, .s-card__caption"
    )
    card_text = _text(card)
    sold_text = _text(sold_el) if sold_el and "Sold" in _text(sold_el) else card_text

    if not ship_el:
        for row in card.select(".s-card__attribute-row, .su-styled-text"):
            t = _text(row)
            if "delivery" in t.lower() or "shipping" in t.lower():
                ship_el = row
                break

    return {
        "title": title,
        "href": href,
        "price_text": _text(price_el),
        "image_url": img,
        "condition": _text(cond_el) or None,
        "shipping_text": _text(ship_el) if ship_el else None,
        "sold_text": sold_text,
    }


def parse_search_page(html: str, source_url: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("li.s-item, li.s-card, div.s-item")
    results = []
    seen = set()
    for card in cards:
        raw = _card_parts(card)
        if not raw["title"] or raw["title"].lower().startswith("shop on ebay"):
            continue
        iid = item_id_from_url(raw["href"]) or card.get("data-listingid") or card.get("id", "").replace("item", "") or None
        if iid and iid in seen:
            continue
        price, currency = parse_price(raw["price_text"])
        listing = {
            "item_id": iid,
            "url": clean_item_url(raw["href"]) if raw["href"] else source_url,
            "title": raw["title"],
            "price": price,
            "currency": currency,
            "shipping": parse_shipping(raw["shipping_text"]),
            "sold_date": parse_sold_date(raw["sold_text"]) if "sold" in raw["sold_text"].lower() else None,
            "image_url": raw["image_url"],
            "specifics": {},
            "source_url": source_url,
        }
        listing.update(extract_attributes(raw["title"], condition=raw["condition"]))
        if iid:
            seen.add(iid)
        results.append(listing)
    return results


# ---------------------------------------------------------------- item pages

def _item_specifics(soup: BeautifulSoup) -> dict[str, str]:
    specifics: dict[str, str] = {}
    # Current layout: <dl class="ux-labels-values"><dt>…</dt><dd>…</dd></dl>
    # or divs with __labels / __values.
    for row in soup.select(".ux-labels-values"):
        label = row.select_one(".ux-labels-values__labels, dt")
        value = row.select_one(".ux-labels-values__values, dd")
        if label and value:
            l, v = _text(label).rstrip(":"), _text(value)
            # The condition value carries a long explanation; keep the first part.
            v = re.split(r"\s(?:See all condition definitions|Read more about the condition)", v)[0]
            if l and v:
                specifics.setdefault(l, v)
    # Older layout: table.itemAttr td.attrLabels
    for label in soup.select("td.attrLabels"):
        value = label.find_next_sibling("td")
        if value:
            specifics.setdefault(_text(label).rstrip(":"), _text(value))
    return specifics


def _json_ld_product(soup: BeautifulSoup) -> dict:
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("@type") == "Product":
                return obj
    return {}


def parse_item_page(html: str, url: str | None = None) -> dict:
    soup = BeautifulSoup(html, "lxml")
    ld = _json_ld_product(soup)

    title = _text(soup.select_one("h1.x-item-title__mainTitle, h1#itemTitle, h1"))
    title = re.sub(r"^Details about\s*", "", title)
    if not title:
        og = soup.select_one("meta[property='og:title']")
        title = (og.get("content") if og else None) or ld.get("name") or ""
    title = re.sub(r"\s*\|\s*eBay\s*$", "", title)

    price_text = _text(soup.select_one(".x-price-primary, #prcIsum, #mm-saleDscPrc, .vi-price .notranslate"))
    price, currency = parse_price(price_text)
    offers = ld.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if price is None and offers.get("price"):
        try:
            price = float(offers["price"])
        except (TypeError, ValueError):
            pass
    currency = offers.get("priceCurrency") or currency

    ship_text = _text(soup.select_one(".ux-labels-values--shipping .ux-labels-values__values, #fshippingCost, .d-shipping-minview"))

    page_text = _text(soup.body) if soup.body else ""
    sold_date = None
    prefix = r"(?:sold on|Ended:?|ended on|Sold)\s*(?:[A-Z][a-z]{2,8},?\s+)?"
    for pat in (prefix + r"([A-Z][a-z]{2,8}\.? \d{1,2},? \d{4})",
                prefix + r"(\d{1,2} [A-Z][a-z]{2,8}\.? \d{4})"):
        m = re.search(pat, page_text)
        if m:
            sold_date = parse_sold_date(m.group(1))
            break

    image = None
    for sel in (".ux-image-carousel-item img", "#icImg", "meta[property='og:image']"):
        img_el = soup.select_one(sel)
        if img_el:
            image = img_el.get("data-zoom-src") or img_el.get("src") or img_el.get("content")
            if image:
                break
    if not image and ld.get("image"):
        image = ld["image"][0] if isinstance(ld["image"], list) else ld["image"]

    specifics = _item_specifics(soup)
    condition = _text(soup.select_one(".x-item-condition-text .ux-textspans, .x-item-condition-value, #vi-itm-cond")) or None
    if not condition:
        condition = specifics.get("Condition")
    if ld.get("brand") and "Brand" not in specifics:
        b = ld["brand"]
        specifics["Brand"] = b.get("name") if isinstance(b, dict) else str(b)

    iid = item_id_from_url(url)
    if not iid:
        m = re.search(r"eBay item number:?\s*(\d{9,15})", page_text)
        iid = m.group(1) if m else None

    listing = {
        "item_id": iid,
        "url": clean_item_url(url) if url else None,
        "title": title,
        "price": price,
        "currency": currency,
        "shipping": parse_shipping(ship_text),
        "sold_date": sold_date,
        "image_url": image,
        "specifics": specifics,
        "source_url": url,
    }
    listing.update(extract_attributes(title, specifics, condition))
    return listing


# ---------------------------------------------------------------- dispatch

def parse_html(html: str, url: str | None = None) -> list[dict]:
    """Parse saved HTML. Detects search vs. item page from the content."""
    if any(marker in html for marker in BLOCK_MARKERS[:2]):
        raise BlockedError("This HTML is an eBay bot-check page, not a listing.")
    if (url and is_search_url(url)) or re.search(r'class="[^"]*\bs-(item|card)\b', html):
        results = parse_search_page(html, url)
        if results or (url and is_search_url(url)):
            return results
    return [parse_item_page(html, url)]


def fetch(url: str, session: requests.Session | None = None, timeout: int = 20) -> str:
    s = session or requests.Session()
    try:
        resp = s.get(url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as e:
        kind = "timed out" if isinstance(e, requests.Timeout) else "network error"
        raise ScrapeError(f"Could not reach eBay ({kind}). Check your internet connection.") from e
    if resp.status_code >= 400:
        raise ScrapeError(f"eBay returned HTTP {resp.status_code} for {url}")
    html = resp.text
    if any(m in html for m in BLOCK_MARKERS[:2]):
        raise BlockedError(
            "eBay showed a bot check. Open the URL in your browser, save the page "
            "(Ctrl+S / 'View source'), and use the 'Paste HTML' option instead."
        )
    return html


def scrape_url(url: str, pages: int = 1, delay: float = 1.5,
               session: requests.Session | None = None, min_price: float | None = None,
               known_ids: set[str] | None = None) -> list[dict]:
    """Scrape a sold search URL (optionally several pages) or an item URL.

    With `known_ids`, paging stops early once a page has nothing new, which
    keeps the daily update fast (results are sorted newest first).
    """
    url = url.strip()
    if not re.match(r"https?://", url):
        raise ScrapeError("URL must start with http:// or https://")
    if "ebay." not in urlparse(url).netloc:
        raise ScrapeError("Only eBay URLs are supported")
    session = session or requests.Session()

    if is_search_url(url):
        url = ensure_sold_filter(url, min_price)
        results: list[dict] = []
        for page in range(1, max(1, pages) + 1):
            page_url = url if page == 1 else with_page(url, page)
            page_results = parse_search_page(fetch(page_url, session), page_url)
            if not page_results:
                break
            results.extend(page_results)
            if known_ids is not None and all(r["item_id"] in known_ids for r in page_results):
                break
            if page < pages:
                time.sleep(delay)
        return results

    return [parse_item_page(fetch(url, session), url)]
