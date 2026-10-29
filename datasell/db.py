"""SQLite storage for sold clothing listings."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from statistics import median

DEFAULT_DB = os.environ.get("DATASELL_DB", "datasell.db")

COLUMNS = [
    "item_id", "url", "title", "price", "currency", "shipping", "sold_date",
    "brand", "category", "department", "size", "color", "material", "style",
    "condition", "vintage", "era", "image_url", "specifics", "source_url",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT UNIQUE,
    url         TEXT,
    title       TEXT NOT NULL,
    price       REAL,
    currency    TEXT,
    shipping    REAL,
    sold_date   TEXT,
    brand       TEXT,
    category    TEXT,
    department  TEXT,
    size        TEXT,
    color       TEXT,
    material    TEXT,
    style       TEXT,
    condition   TEXT,
    vintage     INTEGER DEFAULT 0,
    era         TEXT,
    image_url   TEXT,
    specifics   TEXT,
    source_url  TEXT,
    scraped_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brand ON listings(brand);
CREATE INDEX IF NOT EXISTS idx_category ON listings(category);
CREATE INDEX IF NOT EXISTS idx_sold_date ON listings(sold_date);
"""

FILTERABLE = ["brand", "category", "department", "size", "color", "material", "condition", "era"]
SORTS = {
    "newest": "sold_date DESC, id DESC",
    "oldest": "sold_date ASC, id ASC",
    "price_high": "price DESC",
    "price_low": "price ASC",
    "added": "id DESC",
}


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert(conn: sqlite3.Connection, listing: dict) -> str:
    """Insert or update a listing. Returns 'added', 'updated' or 'skipped'."""
    if not listing.get("title"):
        return "skipped"
    row = {c: listing.get(c) for c in COLUMNS}
    row["vintage"] = 1 if row.get("vintage") else 0
    row["specifics"] = json.dumps(listing.get("specifics") or {})

    existing = None
    if row["item_id"]:
        existing = conn.execute("SELECT * FROM listings WHERE item_id = ?", (row["item_id"],)).fetchone()
    elif row["url"]:
        existing = conn.execute("SELECT * FROM listings WHERE url = ? AND item_id IS NULL", (row["url"],)).fetchone()

    now = _now()
    if existing:
        # Don't let a sparse search-card scrape wipe out richer item-page data.
        merged = {c: (row[c] if row[c] not in (None, "", "{}") else existing[c]) for c in COLUMNS}
        if row["specifics"] == "{}":
            merged["specifics"] = existing["specifics"]
        sets = ", ".join(f"{c} = ?" for c in COLUMNS)
        conn.execute(f"UPDATE listings SET {sets}, updated_at = ? WHERE id = ?",
                     [merged[c] for c in COLUMNS] + [now, existing["id"]])
        conn.commit()
        return "updated"

    cols = ", ".join(COLUMNS + ["scraped_at", "updated_at"])
    marks = ", ".join("?" for _ in range(len(COLUMNS) + 2))
    conn.execute(f"INSERT INTO listings ({cols}) VALUES ({marks})",
                 [row[c] for c in COLUMNS] + [now, now])
    conn.commit()
    return "added"


def save_many(conn: sqlite3.Connection, listings: list[dict]) -> dict:
    counts = {"added": 0, "updated": 0, "skipped": 0}
    for listing in listings:
        counts[upsert(conn, listing)] += 1
    return counts


def _where(filters: dict) -> tuple[str, list]:
    clauses, params = [], []
    q = (filters.get("q") or "").strip()
    if q:
        for word in q.split():
            clauses.append("(title LIKE ? OR brand LIKE ?)")
            params += [f"%{word}%", f"%{word}%"]
    for f in FILTERABLE:
        v = filters.get(f)
        if v:
            clauses.append(f"{f} = ?")
            params.append(v)
    if filters.get("vintage") in ("1", 1, True, "true", "on"):
        clauses.append("vintage = 1")
    for key, op in (("min_price", ">="), ("max_price", "<=")):
        v = filters.get(key)
        if v not in (None, ""):
            try:
                params.append(float(v))
                clauses.append(f"price {op} ?")
            except ValueError:
                pass
    for key, op in (("date_from", ">="), ("date_to", "<=")):
        if filters.get(key):
            clauses.append(f"sold_date {op} ?")
            params.append(filters[key])
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def query(conn: sqlite3.Connection, filters: dict | None = None, limit: int | None = 50,
          offset: int = 0) -> tuple[list[sqlite3.Row], int]:
    filters = filters or {}
    where, params = _where(filters)
    order = SORTS.get(filters.get("sort") or "newest", SORTS["newest"])
    total = conn.execute(f"SELECT COUNT(*) FROM listings {where}", params).fetchone()[0]
    sql = f"SELECT * FROM listings {where} ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    return conn.execute(sql, params).fetchall(), total


def get(conn: sqlite3.Connection, listing_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()


def delete(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    conn.commit()


def facet_values(conn: sqlite3.Connection) -> dict[str, list[str]]:
    out = {}
    for f in FILTERABLE:
        rows = conn.execute(
            f"SELECT {f}, COUNT(*) n FROM listings WHERE {f} IS NOT NULL AND {f} != '' "
            f"GROUP BY {f} ORDER BY n DESC, {f} LIMIT 200").fetchall()
        out[f] = [r[0] for r in rows]
    return out


def _price_summary(prices: list[float]) -> dict:
    if not prices:
        return {"count": 0, "avg": None, "median": None, "min": None, "max": None}
    return {
        "count": len(prices),
        "avg": round(sum(prices) / len(prices), 2),
        "median": round(median(prices), 2),
        "min": min(prices),
        "max": max(prices),
    }


def stats(conn: sqlite3.Connection, filters: dict | None = None, group_by: str = "brand",
          min_count: int = 1) -> dict:
    """Price stats overall and grouped by a field (brand, category, size...)."""
    if group_by not in FILTERABLE + ["vintage"]:
        group_by = "brand"
    where, params = _where(filters or {})
    rows = conn.execute(f"SELECT price, {group_by} AS g, sold_date FROM listings {where}", params).fetchall()

    overall = _price_summary([r["price"] for r in rows if r["price"] is not None])
    overall["listings"] = len(rows)

    groups: dict[str, list[float]] = {}
    for r in rows:
        if r["price"] is None:
            continue
        key = r["g"]
        if group_by == "vintage":
            key = "Vintage" if key else "Not vintage"
        groups.setdefault(key or "(unknown)", []).append(r["price"])
    grouped = [dict(name=k, **_price_summary(v)) for k, v in groups.items() if len(v) >= min_count]
    grouped.sort(key=lambda g: (-g["count"], -(g["median"] or 0)))

    # Monthly median price trend.
    months: dict[str, list[float]] = {}
    for r in rows:
        if r["price"] is not None and r["sold_date"]:
            months.setdefault(r["sold_date"][:7], []).append(r["price"])
    trend = [{"month": m, **_price_summary(v)} for m, v in sorted(months.items())]

    return {"overall": overall, "groups": grouped, "group_by": group_by, "trend": trend}


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["vintage"] = bool(d.get("vintage"))
    try:
        d["specifics"] = json.loads(d.get("specifics") or "{}")
    except json.JSONDecodeError:
        d["specifics"] = {}
    return d


def to_csv(rows: list[sqlite3.Row]) -> str:
    buf = io.StringIO()
    fields = ["id"] + [c for c in COLUMNS if c != "specifics"] + ["scraped_at"]
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(dict(r))
    return buf.getvalue()
