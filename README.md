# Eathos product feeds (Deliverect → Google Shopping + Meta catalogue)

Builds Google Merchant Center and Meta catalogue feeds automatically from the Deliverect ordering
sites. It runs on GitHub **on the 1st of every month**, and on demand whenever you click
**Run workflow** (use this after a menu change).

Nothing is uploaded manually after setup: Google and Meta fetch the files from fixed links.

## What it does
- Reads every branch's menu from the ordering site (the same public data customers see).
- Merges all branches into one brand-wide catalogue. An item is **in stock if any branch has it**.
- Links each product straight to its item page, e.g.
  `order.sushiart.ae/delivery/<branch>/menus/<menu>?item-id=<item>` (opens the product directly).
- Writes per brand, in `feeds/<brand-key>/`:
  - `google.xml`: Google Merchant Center feed
  - `meta.csv`: Meta Commerce Manager feed
  - `report.json`: product counts, exclusions, warnings
  - `CHANGELOG.md`: what changed each month (added / removed / price changes)
- **Safety stop:** if a run finds far fewer products than last time (below 60%), it does not
  overwrite the live feed, and GitHub emails you that the run failed.

### Fields included
| Field | Value |
|---|---|
| id | Deliverect menu item id: the same id the site's pixel sends as `content_ids`, so Meta/Google match events to products |
| title / description | From the ordering site + brand tagline. Missing descriptions get a generated one |
| price | e.g. `71.00 AED` |
| availability | in stock / out of stock |
| brand, condition | Brand name, `new` |
| identifier_exists | `no` (menu items have no barcodes) |
| google_product_category | 422 Food Items, 413 Beverages |
| product_type | `Sushi Art > Starters` |
| custom_label_0 | Menu category |
| custom_label_1 | Price band: `under_30`, `30_to_60`, `60_to_100`, `100_plus` |
| custom_label_2 | `new` (New On The Menu) or `core` |
| custom_label_3 | `vegetarian` when tagged |

Links carry UTM tags (`utm_source=google` / `utm_source=meta`), editable in `config.json`.

---

## One-time setup (about 15 minutes)

### 1. Put the files on GitHub
1. Create a free account at github.com (ideally a shared company account, not personal).
2. Click **New repository** → name it `product-feeds` → choose **Public** → Create.
   Public is needed so Google and Meta can read the feed links. Everything in it is already
   public on the ordering site; there are no passwords or keys.
3. Click **uploading an existing file** and drag in everything from this folder
   **except** the `.github` folder (macOS hides it). Commit.
4. Add the schedule: **Add file → Create new file**, type the name
   `.github/workflows/update-feeds.yml`, paste the contents of that file from this folder, Commit.
5. **Settings → Actions → General → Workflow permissions** → select **Read and write permissions** → Save.

### 2. Run it the first time
**Actions** tab → **Update product feeds** → **Run workflow**. After about a minute, a
`feeds/sushiart-ae/` folder appears with the files.

Your feed links (replace `YOUR-ACCOUNT`):
- Google: `https://raw.githubusercontent.com/YOUR-ACCOUNT/product-feeds/main/feeds/sushiart-ae/google.xml`
- Meta: `https://raw.githubusercontent.com/YOUR-ACCOUNT/product-feeds/main/feeds/sushiart-ae/meta.csv`

### 3. Google Merchant Center
1. Make sure the website `sushiart.ae` is verified and claimed (product links use `order.sushiart.ae`).
2. **Products → Add products → Add products from a file → Add a file link** → paste the Google link.
3. Country **United Arab Emirates**, language **English**, currency **AED**.
4. Set the fetch to **Daily**. The file only changes monthly, but Google expires products
   that are not re-fetched within 30 days, so a daily fetch keeps them live.
5. Set up delivery/shipping settings for UAE, or products will be flagged for missing shipping.

### 4. Meta Commerce Manager
1. **Catalogue → Data sources → Add items → Data feed → Use a URL** → paste the Meta link.
2. Currency **AED**, schedule **Daily**.

---

## Everyday use
- **Menu changed?** Actions → Update product feeds → Run workflow. Both platforms pick it up on
  their next daily fetch.
- **See what changed:** open `feeds/sushiart-ae/CHANGELOG.md`.
- **Fix a title/description, swap an image, or hide a product:** add a row to
  `overrides/sushiart-ae.csv` (edit it on GitHub with the pencil icon). Match by product id or
  exact name. Put `yes` in `exclude` to remove a product from both feeds.
- **Branded soft drinks and bottled water are excluded automatically** (Pepsi, Coke, 7Up,
  mineral/sparkling water, Masafi, Al Ain, etc.). The word list is `exclude_name_keywords` in
  `config.json` and applies to every brand; add a word there to exclude more. To keep one
  item that a keyword catches, add it to the overrides file with `no` in `exclude`.
- **Hide a whole category** (e.g. Drinks): add it to `exclude_categories` in `config.json`.
- **A run failed?** GitHub emails the account owner. The previous feed stays live. Usually the
  ordering site was down; run it again later.

## Arabic (or other language) feeds
Add `"extra_languages": ["ar"]` to a brand (plus `tagline_ar` and `brand_ar`). Each run then also writes:
- `google_ar.xml`: a full Arabic feed with the same product IDs. Add it in Merchant Center as a
  second data source, language **Arabic**, same country.
- `meta_ar.csv`: a Meta *language override* feed (id, override, title, description). Add it to
  the same catalogue as a language/localisation feed, so one catalogue serves both languages.

Which products appear is always decided on the English data, so both languages hold the same items.
Arabic text can be corrected in the overrides file with the `title_ar` / `description_ar` columns.

## Adding the other brands
Each brand is one block in `config.json`. Values like `business_id` and `link_outlet_slug`
come from the brand's ordering site; ask Claude to look them up from the ordering link.

## Run locally (optional)
```
python3 generate_feeds.py                     # all brands
python3 generate_feeds.py --brand sushiart-ae
python3 generate_feeds.py --fixture tests/fixture.json --out /tmp/test   # offline test
```
Python 3.9+, no extra packages.
