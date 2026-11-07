# Datasell

Build a searchable database of **used clothing sold on eBay**. Paste eBay *sold* URLs and Datasell
scrapes each listing, pulls out clothing attributes (brand, category, size, color, material,
department, condition, vintage/era), stores them in SQLite, and shows what things actually sell for.

- **Check an item from your phone.** Snap the front and the tag; Claude reads the brand, size,
  material and era, then the app checks eBay solds and says **Buy it / Maybe / Pass** with the
  typical sale price, sell-through rate, what to list at and how much you can pay.
- **$30+ sales only.** Anything that sold for under $30 (or has no price) is ignored.
- **Sell-through rate.** For your saved searches and your best brands: how much of the $30+
  market actually sells within ~90 days. The **Fast sellers** page ranks them.
- **Updated daily.** Saved searches are re-pulled automatically every evening at 11:30 PM.

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # for photo checks (Windows: set ANTHROPIC_API_KEY=...)
python -m datasell serve --host 0.0.0.0   # open http://<this computer's IP>:5000 on your phone
```

`--host 0.0.0.0` lets your phone open the app when it's on the same Wi-Fi. Leave it out to use
the app only on the computer itself.

## Check an item (Buy / Maybe / Pass)

Open **Check item** on your phone, tap **Front of item** and **Tag** to take the two photos,
optionally enter what it costs, and tap **Check it** (about 20–40 seconds). You get:

- **Verdict** with plain reasons, e.g. "Fast: 62% sell-through at $30+ (118 sold in ~90 days vs
  72 listed)" or "Usually sells for $18, under your $30 minimum".
- **Typical sale** (median of recent eBay solds for that item, pre-owned), **list at** (upper
  quarter of solds), and **max to pay** (or estimated profit if you entered the cost).
- **What Claude sees**: brand, line, size, material, made in, RN number, era, condition notes,
  red flags (e.g. signs of a fake), and a suggested listing title you can copy into Nifty.
- **Sold comps** with links to eBay. If the search doesn't match the item, edit it and tap
  **Check again** (no new photo needed). The $30+ comps are saved to your database too.

No photo or no API key? Open **No photo? Type what it is**, or from the command line:
`python -m datasell check "Patagonia Retro X fleece" --cost 8`.

How the verdict is decided (all adjustable, see Settings):

| | Rule |
| --- | --- |
| **Buy it** | Typical sale is $30+, sell-through at $30+ is 50% or more, 5+ comps, confident ID, and (if you entered a cost) $15+ profit after ~15% fees |
| **Maybe** | Sell-through 25–50%, few comps, unsure ID, eBay couldn't be checked, or profit just under target |
| **Pass** | Typical sale under $30, sell-through under 25%, or profit well under target |

Each photo check costs a few cents of Claude API usage (model `claude-opus-5-5`; change it with
`DATASELL_MODEL`). Get an API key at console.anthropic.com.

## Sell-through rate and Fast sellers

Sell-through = items sold in the last ~90 days ÷ (sold + still listed), counting only $30+
listings, read from the result counts on eBay's sold and active searches. Each night the app
measures it for every saved search, plus up to 10 of the brand + item-type combos (e.g.
"Carhartt Jacket") that sell most often at $30+ in your data (each re-checked weekly). The
**Fast sellers** page ranks them and hides slow ones.

The **Daily updates** page has one-tap presets for eBay clothing categories (men's and women's
coats, sweaters, shirts, jeans, pants, dresses, shoes…), optionally narrowed to a brand.

1. On eBay, search for something (e.g. `vintage levis 501`), turn on **Sold items** in the filters,
   and copy the address bar URL.
2. In Datasell, go to **Add sold URLs**, paste one or more URLs (one per line), pick how many
   result pages to grab, and hit **Scrape & save**. Leave **Save search URLs for daily updates**
   ticked to keep pulling new sales for that search every day.
3. Use **Browse** to filter by brand / category / size / color / price / date, and **Price stats**
   to see median and average sold prices grouped by any attribute, plus a monthly trend.

### Supported URLs

| URL type | Example | What you get |
| --- | --- | --- |
| Sold search results | `https://www.ebay.com/sch/i.html?_nkw=carhartt+jacket&LH_Sold=1&LH_Complete=1` | Every listing on the page(s): title, sold price, shipping, sold date, image, condition. Attributes are parsed from the title. Sold filters are added automatically. |
| Single item | `https://www.ebay.com/itm/123456789012` | Everything above plus eBay's **item specifics** (exact brand, size, material, department…), which are more accurate than title parsing. |

Scraping the same item twice updates it instead of creating a duplicate, and a sparse search
result never overwrites richer item-page data.

### $30 minimum

Search URLs get eBay's own minimum-price filter (`_udlo=30`) added, so eBay only returns $30+
sales, and every listing is checked again before saving. Change the minimum with the
`DATASELL_MIN_PRICE` environment variable or `--min-price` (e.g. `--min-price 50`). If a search
URL already sets its own minimum price on eBay, that one is kept. To remove anything under the
minimum that's already in the database: `python -m datasell prune`.

### Daily updates

The **Daily updates** page lists your saved searches. Every day at the end of the day, each one is
re-scraped (newest sales first) and new $30+ sales are added. Already-known items are updated, not
duplicated, and paging stops early once it reaches sales that are already saved. The page also
shows when each search last ran, how many new sales it found, any errors, and a **Run update now**
button.

How it runs:

- **While the app is running** (`python -m datasell serve`), a background scheduler runs the update
  daily at 11:30 PM. If the computer or app was off at that time, it catches up as soon as the app
  starts again.
- **Without keeping the app open**, schedule the one-shot command instead (and start the web app
  with `--no-schedule` so it doesn't also run its own):

  ```bash
  # Linux/macOS cron: every day at 11:30 PM
  30 23 * * * cd /path/to/Datasell && python3 -m datasell daily >> daily.log 2>&1
  ```

  On Windows, create a daily Task Scheduler task that runs `python -m datasell daily` in the
  Datasell folder.

Settings (environment variables):

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATASELL_MIN_PRICE` | `30` | Ignore sales under this price |
| `DATASELL_DAILY_AT` | `23:30` | Time of the daily update (24-hour) |
| `DATASELL_TZ` | computer's time zone | Time zone for the daily update, e.g. `America/New_York` |
| `DATASELL_DB` | `datasell.db` | Database file |
| `ANTHROPIC_API_KEY` | — | Claude API key for photo checks |
| `DATASELL_MODEL` | `claude-opus-5-5` | Claude model for photo checks |
| `DATASELL_FAST_STR` / `DATASELL_OK_STR` | `0.5` / `0.25` | Sell-through for "fast" / "moderate" |
| `DATASELL_FEE_RATE` | `0.15` | Marketplace fees used for profit |
| `DATASELL_MIN_PROFIT` | `15` | Profit an item must clear to be a Buy |
| `DATASELL_BRAND_CHECKS` | `10` | Brand sell-through checks per night |

### If eBay blocks the request

eBay sometimes shows a bot check ("Pardon Our Interruption"). When that happens, open the URL in
your browser, choose **View page source**, copy everything, and paste it into the
**"Paste the page HTML instead"** box on the Add page (or save it as a file and use
`python -m datasell import page.html`).

## Command line

```bash
python -m datasell add "https://www.ebay.com/sch/i.html?_nkw=patagonia+fleece&LH_Sold=1" --pages 3 --watch
python -m datasell watch "https://www.ebay.com/sch/i.html?_nkw=vintage+carhartt" --name "Carhartt"
python -m datasell searches              # list saved searches and when they last ran
python -m datasell daily                 # run the daily update now
python -m datasell prune                 # delete stored sales under the minimum
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
| `POST /api/run-daily` | Start the daily update now in the background |
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
- `datasell/db.py` – SQLite schema, upserts, $30 minimum, saved searches, filtering, stats, CSV
- `datasell/daily.py` – daily update job, sell-through tracking and background scheduler
- `datasell/vision.py` – identifying an item from photos with Claude
- `datasell/market.py` – eBay comps and sell-through checks
- `datasell/appraise.py` – Buy / Maybe / Pass rules
- `datasell/app.py` – Flask web UI and JSON API
- `tests/` – tests with saved HTML fixtures

Please scrape responsibly: keep page counts modest (there is a delay between pages) and respect
eBay's terms of use.
