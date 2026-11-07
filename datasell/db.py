"""SQLite storage for sold clothing listings."""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median

DEFAULT_DB = os.environ.get("DATASELL_DB", "datasell.db")
# Optional folder for nightly copies of the database + a CSV of solds, e.g.
# /storage/emulated/0/Documents/Datasell on Android (visible in the Files app).
BACKUP_DIR = os.environ.get("DATASELL_BACKUP_DIR") or None
BACKUP_NAME = "datasell.db"
CSV_NAME = "datasell_solds.csv"

# Only keep sales at or above this price (sold price, not counting shipping).
MIN_PRICE = float(os.environ.get("DATASELL_MIN_PRICE", "30"))

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

-- Search URLs that get re-scraped by the daily job.
CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL UNIQUE,
    name        TEXT,
    pages       INTEGER NOT NULL DEFAULT 3,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    last_run_at TEXT,
    last_added  INTEGER,
    last_error  TEXT
);

-- Photo checks: what Claude saw, the market data and the verdict.
CREATE TABLE IF NOT EXISTS appraisals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    front_thumb TEXT,
    tag_thumb   TEXT,
    ident       TEXT,
    query       TEXT,
    cost        REAL,
    verdict     TEXT,
    reasons     TEXT,
    est_price   REAL,
    list_price  REAL,
    max_buy     REAL,
    profit      REAL,
    str         REAL,
    market      TEXT
);

-- Sell-through checks for brand + item-type combos (the Fast sellers page).
CREATE TABLE IF NOT EXISTS market_checks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    query            TEXT NOT NULL UNIQUE,
    sold_min_count   INTEGER,
    active_min_count INTEGER,
    str              REAL,
    median_price     REAL,
    checked_at       TEXT NOT NULL
);

-- One row per daily (or manual "run now") pull.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    trigger     TEXT,
    searches    INTEGER DEFAULT 0,
    added       INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    below_min   INTEGER DEFAULT 0,
    errors      TEXT
);
"""

FILTERABLE = ["brand", "category", "department", "size", "color", "material", "condition", "era"]
SORTS = {
    "newest": "sold_date DESC, id DESC",
    "oldest": "sold_date ASC, id ASC",
    "price_high": "price DESC",
    "price_low": "price ASC",
    "added": "id DESC",
}


# Columns added after the first release; added to older databases on connect.
MIGRATIONS = {
    "searches": {"sold_count": "INTEGER", "active_count": "INTEGER", "str": "REAL",
                 "median_price": "REAL", "str_checked_at": "TEXT"},
}


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DEFAULT_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert(conn: sqlite3.Connection, listing: dict, min_price: float | None = None) -> str:
    """Insert or update a listing.

    Returns 'added', 'updated', 'skipped' (no title) or 'below_min' (price
    missing or under `min_price`, which defaults to MIN_PRICE).
    """
    if not listing.get("title"):
        return "skipped"
    floor = MIN_PRICE if min_price is None else min_price
    if floor > 0 and (listing.get("price") is None or listing["price"] < floor):
        return "below_min"
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


def save_many(conn: sqlite3.Connection, listings: list[dict], min_price: float | None = None) -> dict:
    counts = {"added": 0, "updated": 0, "skipped": 0, "below_min": 0}
    for listing in listings:
        counts[upsert(conn, listing, min_price)] += 1
    return counts


def known_item_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT item_id FROM listings WHERE item_id IS NOT NULL")}


def prune_below(conn: sqlite3.Connection, min_price: float | None = None) -> int:
    """Delete stored listings under the minimum price. Returns how many."""
    floor = MIN_PRICE if min_price is None else min_price
    cur = conn.execute("DELETE FROM listings WHERE price IS NULL OR price < ?", (floor,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------- saved searches

def add_search(conn: sqlite3.Connection, url: str, pages: int = 3, name: str | None = None) -> bool:
    """Save a search URL for daily updates. Returns False if it was already saved."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO searches (url, name, pages, created_at) VALUES (?, ?, ?, ?)",
        (url, name, max(1, min(10, pages)), _now()))
    conn.commit()
    return cur.rowcount == 1


def list_searches(conn: sqlite3.Connection, active_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM searches" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
    return conn.execute(sql).fetchall()


def update_search(conn: sqlite3.Connection, search_id: int, **fields) -> None:
    allowed = {k: v for k, v in fields.items()
               if k in ("name", "pages", "active", "last_run_at", "last_added", "last_error",
                        "sold_count", "active_count", "str", "median_price", "str_checked_at")}
    if allowed:
        sets = ", ".join(f"{k} = ?" for k in allowed)
        conn.execute(f"UPDATE searches SET {sets} WHERE id = ?", list(allowed.values()) + [search_id])
        conn.commit()


def delete_search(conn: sqlite3.Connection, search_id: int) -> None:
    conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
    conn.commit()


def start_run(conn: sqlite3.Connection, trigger: str) -> int:
    cur = conn.execute("INSERT INTO runs (started_at, trigger) VALUES (?, ?)", (_now(), trigger))
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, searches: int, counts: dict, errors: list[str]) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, searches = ?, added = ?, updated = ?, below_min = ?, errors = ? WHERE id = ?",
        (_now(), searches, counts.get("added", 0), counts.get("updated", 0), counts.get("below_min", 0),
         "\n".join(errors) or None, run_id))
    conn.commit()


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def last_scheduled_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE trigger = 'schedule' AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


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


# ---------------------------------------------------------------- photo checks

def add_appraisal(conn: sqlite3.Connection, **fields) -> int:
    cols = ["front_thumb", "tag_thumb", "ident", "query", "cost", "verdict", "reasons",
            "est_price", "list_price", "max_buy", "profit", "str", "market"]
    row = {c: fields.get(c) for c in cols}
    for c in ("ident", "reasons", "market"):
        if row[c] is not None and not isinstance(row[c], str):
            row[c] = json.dumps(row[c])
    cur = conn.execute(
        f"INSERT INTO appraisals (created_at, {', '.join(cols)}) VALUES (?, {', '.join('?' for _ in cols)})",
        [_now()] + [row[c] for c in cols])
    conn.commit()
    return cur.lastrowid


def update_appraisal(conn: sqlite3.Connection, appraisal_id: int, **fields) -> None:
    for c in ("ident", "reasons", "market"):
        if c in fields and fields[c] is not None and not isinstance(fields[c], str):
            fields[c] = json.dumps(fields[c])
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE appraisals SET {sets} WHERE id = ?", list(fields.values()) + [appraisal_id])
    conn.commit()


def get_appraisal(conn: sqlite3.Connection, appraisal_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM appraisals WHERE id = ?", (appraisal_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for c, empty in (("ident", {}), ("reasons", []), ("market", {})):
        try:
            d[c] = json.loads(d[c]) if d[c] else empty
        except json.JSONDecodeError:
            d[c] = empty
    return d


def list_appraisals(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute("SELECT id FROM appraisals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [get_appraisal(conn, r["id"]) for r in rows]


def delete_appraisal(conn: sqlite3.Connection, appraisal_id: int) -> None:
    conn.execute("DELETE FROM appraisals WHERE id = ?", (appraisal_id,))
    conn.commit()


# ---------------------------------------------------------------- sell-through checks

def save_market_check(conn: sqlite3.Connection, query: str, m: dict) -> None:
    conn.execute(
        """INSERT INTO market_checks (query, sold_min_count, active_min_count, str, median_price, checked_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(query) DO UPDATE SET sold_min_count = excluded.sold_min_count,
             active_min_count = excluded.active_min_count, str = excluded.str,
             median_price = excluded.median_price, checked_at = excluded.checked_at""",
        (query, m.get("sold_min_count"), m.get("active_min_count"), m.get("str"),
         m.get("median_price_min") or m.get("median_price"), _now()))
    conn.commit()


def market_checks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM market_checks ORDER BY str DESC").fetchall()


def stale_market_queries(conn: sqlite3.Connection, queries: list[str], max_age_days: int = 7) -> list[str]:
    """Which of these queries have no check, or one older than max_age_days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    fresh = {r[0] for r in conn.execute("SELECT query FROM market_checks WHERE checked_at >= ?", (cutoff,))}
    return [q for q in queries if q not in fresh]


def top_brand_combos(conn: sqlite3.Connection, min_count: int = 3, limit: int = 10) -> list[str]:
    """Brand + category combos that most often sell for the minimum or more."""
    rows = conn.execute(
        """SELECT brand, category, COUNT(*) n FROM listings
           WHERE brand IS NOT NULL AND category IS NOT NULL AND price >= ?
           GROUP BY brand COLLATE NOCASE, category HAVING n >= ? ORDER BY n DESC LIMIT ?""",
        (MIN_PRICE, min_count, limit)).fetchall()
    return [f"{r['brand']} {r['category']}" for r in rows]


# ---------------------------------------------------------------- backups

def backup(conn: sqlite3.Connection, folder: str | None = None) -> str | None:
    """Copy the database (safely, while in use) and a CSV of solds to `folder`.

    Writes to temporary files first so a backup is never left half-written.
    Returns the backup path, or None if no folder is configured.
    """
    folder = folder or BACKUP_DIR
    if not folder:
        return None
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, BACKUP_NAME)
    tmp = target + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    dest = sqlite3.connect(tmp)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    os.replace(tmp, target)

    rows, _ = query(conn, {}, limit=None)
    csv_tmp = os.path.join(folder, CSV_NAME + ".tmp")
    with open(csv_tmp, "w", newline="", encoding="utf-8") as f:
        f.write(to_csv(rows))
    os.replace(csv_tmp, os.path.join(folder, CSV_NAME))
    return target


def last_backup(folder: str | None = None) -> dict | None:
    folder = folder or BACKUP_DIR
    if not folder:
        return None
    path = os.path.join(folder, BACKUP_NAME)
    if not os.path.exists(path):
        return {"folder": folder, "at": None}
    at = datetime.fromtimestamp(os.path.getmtime(path)).astimezone()
    return {"folder": folder, "at": at.strftime("%b %d, %I:%M %p")}


def restore_if_missing(path: str | None = None, folder: str | None = None) -> bool:
    """If the database file is gone (e.g. the app was reinstalled) but a backup
    exists, copy the backup back. Returns True if it restored."""
    path = path or DEFAULT_DB
    folder = folder or BACKUP_DIR
    if not folder or os.path.exists(path):
        return False
    src = os.path.join(folder, BACKUP_NAME)
    if not os.path.exists(src):
        return False
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    shutil.copyfile(src, path)
    return True
