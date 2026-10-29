# Datasell

Build a searchable database of **used clothing sold on eBay**. Paste eBay *sold* URLs and Datasell
scrapes each listing, pulls out clothing attributes (brand, category, size, color, material,
department, condition, vintage/era), stores them in SQLite, and shows what things actually sell for.

## Quick start

```bash
pip install -r requirements.txt
python -m datasell serve          # open http://127.0.0.1:5000
```

1. On eBay, search for something (e.g. `vintage levis 501`), turn on **Sold items** in the filters,
   and copy the address bar URL.
2. In Datasell, go to **Add sold URLs**, paste one or more URLs (one per line), pick how many
   result pages to grab, and hit **Scrape & save**.
3. Use **Browse** to filter by brand / category / size / color / price / date, and **Price stats**
   to see median and average sold prices grouped by any attribute, plus a monthly trend.

### Supported URLs

| URL type | Example | What you get |
| --- | --- | --- |
| Sold search results | `https://www.ebay.com/sch/i.html?_nkw=carhartt+jacket&LH_Sold=1&LH_Complete=1` | Every listing on the page(s): title, sold price, shipping, sold date, image, condition. Attributes are parsed from the title. Sold filters are added automatically. |
| Single item | `https://www.ebay.com/itm/123456789012` | Everything above plus eBay's **item specifics** (exact brand, size, material, department…), which are more accurate than title parsing. |

Scraping the same item twice updates it instead of creating a duplicate, and a sparse search
result never overwrites richer item-page data.

### If eBay blocks the request

eBay sometimes shows a bot check ("Pardon Our Interruption"). When that happens, open the URL in
your browser, choose **View page source**, copy everything, and paste it into the
**"Paste the page HTML instead"** box on the Add page (or save it as a file and use
`python -m datasell import page.html`).

## Command line

```bash
python -m datasell add "https://www.ebay.com/sch/i.html?_nkw=patagonia+fleece&LH_Sold=1" --pages 3
python -m datasell import saved_page.html --url https://www.ebay.com/itm/123456789012
python -m datasell stats --by brand      # or category, size, color, material, era, vintage...
python -m datasell export listings.csv
python -m datasell --db other.db serve --port 8000
```

The database file defaults to `datasell.db` in the current folder (override with `--db` or the
`DATASELL_DB` environment variable).

## JSON API

| Endpoint | Description |
| --- | --- |
| `GET /api/listings?brand=Nike&max_price=50&limit=100` | Filtered listings (same filters as Browse) |
| `GET /api/stats?group_by=category` | Price summary overall, by group, and by month |
| `POST /api/scrape` `{"urls": [...], "pages": 2}` or `{"html": "...", "url": "..."}` | Scrape and save |
| `GET /export.csv?…filters` | CSV download |

## Data fields

`item_id, url, title, price, currency, shipping, sold_date, brand, category, department, size,
color, material, style, condition, vintage, era, image_url, specifics (raw eBay item specifics as
JSON), source_url, scraped_at`

Every field can be corrected by hand on a listing's detail page.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

- `datasell/scraper.py` – fetching and parsing eBay search pages (old `s-item` and new `s-card`
  layouts) and item pages
- `datasell/attributes.py` – clothing attribute extraction from titles and item specifics
- `datasell/db.py` – SQLite schema, upserts, filtering, stats, CSV
- `datasell/app.py` – Flask web UI and JSON API
- `tests/` – tests with saved HTML fixtures

Please scrape responsibly: keep page counts modest (there is a delay between pages) and respect
eBay's terms of use.
