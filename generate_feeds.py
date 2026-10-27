#!/usr/bin/env python3
"""
Build Google Merchant Center (XML) and Meta catalogue (CSV) product feeds
from Deliverect web-ordering sites (e.g. order.sushiart.ae).

It reads the same public menu data the ordering website shows to customers,
merges every branch into one brand-wide catalogue, and writes:

    feeds/<brand-key>/google.xml   -> Google Merchant Center scheduled fetch
    feeds/<brand-key>/meta.csv     -> Meta Commerce Manager scheduled feed
    feeds/<brand-key>/report.json  -> counts, warnings, changes since last run
    feeds/<brand-key>/CHANGELOG.md -> human-readable history of menu changes

Standard library only (Python 3.9+). Usage:

    python generate_feeds.py                 # all enabled brands
    python generate_feeds.py --brand sushiart-ae
    python generate_feeds.py --fixture tests/fixture.json   # offline test
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.abspath(__file__))
USER_AGENT = "Mozilla/5.0 (compatible; EathosFeedBot/1.0; product feed generator)"

GOOGLE_FOOD = "422"       # Food, Beverages & Tobacco > Food Items
GOOGLE_BEVERAGES = "413"  # Food, Beverages & Tobacco > Beverages

META_COLUMNS = [
    "id", "title", "description", "availability", "condition", "price",
    "link", "image_link", "brand", "google_product_category", "product_type",
    "custom_label_0", "custom_label_1", "custom_label_2", "custom_label_3",
]


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------
class DeliverectClient:
    """Reads the public menu API that the Deliverect ordering site uses."""

    def __init__(self, brand, lang=None, timeout=30, retries=3):
        self.brand = brand
        self.lang = lang  # e.g. "ar": the menu API returns translated names/descriptions
        self.base = f"https://{brand['domain']}/api/dbe-proxy/"
        self.timeout = timeout
        self.retries = retries

    def _get(self, path, **params):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last = None
        for attempt in range(self.retries):
            try:
                headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
                if self.lang:
                    headers["Accept-Language"] = self.lang
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.load(resp)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"GET {url} failed after {self.retries} tries: {last}")

    def list_outlets(self):
        b = self.brand
        outlets, page = [], 1
        while True:
            res = self._get(f"businesses/{b['business_id']}/outlets",
                            limit=50, page=page, salesChannel=b["sales_channel"])
            data = res.get("data") or []
            outlets.extend(data)
            if len(data) < 50:
                return outlets
            page += 1

    def outlet(self, slug):
        b = self.brand
        return self._get("outlets", businessId=b["business_id"], slug=slug,
                         salesChannel=b["sales_channel"], orderingMode=b["ordering_mode"])

    def menus(self, outlet_id):
        b = self.brand
        res = self._get(f"outlets/{outlet_id}/menus", businessId=b["business_id"],
                        orderingMode=b["ordering_mode"], salesChannel=b["sales_channel"])
        return res.get("data") or []

    def menu(self, outlet_id, menu_slug):
        return self._get(f"outlets/{outlet_id}/menu", slug=menu_slug,
                         salesChannel=self.brand["sales_channel"])


class FixtureClient:
    """Offline stand-in that serves saved API responses (for testing)."""

    def __init__(self, fixture, lang=None):
        self.f = fixture
        self.lang = lang

    def list_outlets(self):
        return [{"slug": s, "name": v["outlet"]["name"]} for s, v in self.f.items()]

    def outlet(self, slug):
        return self.f[slug]["outlet"]

    def _by_id(self, outlet_id):
        return next(v for v in self.f.values() if v["outlet"]["id"] == outlet_id)

    def menus(self, outlet_id):
        return self._by_id(outlet_id)["menus"]["data"]

    def menu(self, outlet_id, menu_slug):
        v = self._by_id(outlet_id)
        return v.get(f"menu_{self.lang}") or v["menu"]


# --------------------------------------------------------------------------
# Collect + merge every branch into one catalogue
# --------------------------------------------------------------------------
def collect(brand, client, log):
    outlets = client.list_outlets()
    if not outlets:
        raise RuntimeError("No branches returned by the ordering site.")
    preferred = brand.get("link_outlet_slug")
    outlets.sort(key=lambda o: 0 if o.get("slug") == preferred else 1)

    products, warnings, branches_ok, menus_read = {}, [], 0, []
    for o in outlets:
        slug = o["slug"]
        try:
            full = client.outlet(slug)
            if (full.get("settings") or {}).get("isEnabled") is False:
                warnings.append(f"Branch skipped (disabled online): {full.get('name', slug)}")
                continue
            for m in client.menus(full["id"]):
                menu = client.menu(full["id"], m["slug"])
                _merge_menu(products, menu, slug, full.get("name", slug), warnings)
                menus_read.append((full["id"], m["slug"]))
            branches_ok += 1
        except Exception as e:  # one broken branch must not kill the feed
            warnings.append(f"Branch failed and was skipped: {o.get('name', slug)} ({e})")
            log(f"  ! {o.get('name', slug)}: {e}")

    if branches_ok == 0:
        raise RuntimeError("Could not read any branch menu. Feed NOT updated.")
    log(f"  read {branches_ok}/{len(outlets)} branches, {len(products)} unique products")
    return products, warnings, branches_ok, len(outlets), menus_read


def collect_texts(client, menus_read):
    """Translated name/description per item id, from the same menus in another language."""
    texts = {}
    for outlet_id, menu_slug in menus_read:
        menu = client.menu(outlet_id, menu_slug)
        for cat in menu.get("categories") or []:
            for it in cat.get("items") or []:
                if it["id"] not in texts:
                    texts[it["id"]] = {"name": (it.get("name") or "").strip(),
                                       "description": (it.get("description") or "").strip()}
    return texts


def _merge_menu(products, menu, outlet_slug, outlet_name, warnings):
    for cat in menu.get("categories") or []:
        for it in cat.get("items") or []:
            if it.get("type") not in (None, "product"):
                continue
            # Feed id = Deliverect menu item id. This is the id the ordering site pushes to
            # the dataLayer (view_item / add_to_cart), which the Meta pixel and Google tags
            # send as content_ids / item_id, so catalogue and ad events match.
            key = it["id"]
            price = (it.get("price") or {})
            p = products.get(key)
            if p is None:
                products[key] = {
                    "key": key,
                    "item_id": it["id"],
                    "name": (it.get("name") or "").strip(),
                    "description": (it.get("description") or "").strip(),
                    "price_minor": price.get("value"),
                    "currency": price.get("currency") or "AED",
                    "category": (cat.get("name") or "").strip(),
                    "photo": it.get("photo") or "",
                    "link_outlet": outlet_slug,
                    "menu_slug": menu.get("slug"),
                    "in_stock": bool(it.get("inStock")),
                    "branches": {outlet_name},
                    "vegetarian": any((t or {}).get("key") == "vegetarian"
                                      for t in it.get("consumableTags") or []),
                }
            else:
                p["in_stock"] = p["in_stock"] or bool(it.get("inStock"))
                p["branches"].add(outlet_name)
                if not p["photo"] and it.get("photo"):
                    p["photo"] = it["photo"]
                if price.get("value") is not None and price.get("value") != p["price_minor"]:
                    warnings.append(
                        f"Price differs by branch for '{p['name']}': "
                        f"{fmt_money(p['price_minor'], p['currency'])} vs "
                        f"{fmt_money(price['value'], p['currency'])} at {outlet_name} "
                        f"(feed uses the first)")


# --------------------------------------------------------------------------
# Build feed rows
# --------------------------------------------------------------------------
def fmt_money(minor, currency):
    return f"{(minor or 0) / 100:.2f} {currency}"


def clean_title(name):
    name = re.sub(r"\s+", " ", name).strip()
    if name.isupper() and len(name) > 3:  # "FRESH ORANGE JUICE" -> "Fresh Orange Juice"
        name = name.title()
    return name


def price_band(minor):
    aed = (minor or 0) / 100
    if aed < 30:
        return "under_30"
    if aed < 60:
        return "30_to_60"
    if aed < 100:
        return "60_to_100"
    return "100_plus"


def load_overrides(brand_key):
    path = os.path.join(ROOT, "overrides", f"{brand_key}.csv")
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            match = (row.get("match") or "").strip()
            if match and not match.startswith("#"):
                out[match.lower()] = {k: (v or "").strip() for k, v in row.items()}
    return out


def keyword_matcher(keywords):
    """Whole-word, case-insensitive match: 'pepsi' hits 'Diet Pepsi' but
    'mineral water' does not hit 'Fresh Water Melon Juice'."""
    words = [re.escape(k.strip()) for k in keywords if k and k.strip()]
    if not words:
        return lambda name: None
    rx = re.compile(r"(?<![a-z0-9])(" + "|".join(words) + r")(?![a-z0-9])", re.I)

    def match(name):
        m = rx.search(name)
        return m.group(1) if m else None
    return match


FALLBACK_DESC = {
    "en": "{title} from the {brand} {category} menu.",
    "ar": "{title} من قائمة {brand}.",
}


def build_rows(brand, products, overrides, utm, global_keywords=(), lang="en", texts=None):
    """Feed rows. Which products are in (exclusions, keywords) is always decided on the
    English data, so every language version holds exactly the same products and ids.
    `texts` (another language) only swaps the title and description."""
    rows, excluded = [], []
    excluded_keyword = keyword_matcher(
        list(global_keywords) + brand.get("exclude_name_keywords", []))
    exclude_cats = {c.lower() for c in brand.get("exclude_categories", [])}
    new_cats = {c.lower() for c in brand.get("new_categories", [])}
    bev_cats = {c.lower() for c in brand.get("beverage_categories", [])}
    primary = lang == "en" or not texts
    tagline = (brand.get("tagline", "") if primary
               else brand.get(f"tagline_{lang}", "")).strip()
    brand_name_local = brand["brand"] if primary else brand.get(f"brand_{lang}", brand["brand"])

    for p in sorted(products.values(), key=lambda x: (x["category"], x["name"])):
        title = clean_title(p["name"])
        ov = overrides.get(p["key"].lower()) or overrides.get(title.lower()) or {}
        if ov.get("exclude", "").lower() in ("yes", "y", "true", "1"):
            excluded.append(title)
            continue
        if p["category"].lower() in exclude_cats:
            excluded.append(title)
            continue
        hit = excluded_keyword(title)
        if hit and ov.get("exclude", "").lower() not in ("no", "n", "false", "0"):
            excluded.append(f"{title} (keyword: {hit})")
            continue
        if not p["price_minor"] or not p["photo"]:
            excluded.append(f"{title} (missing price or photo)")
            continue

        if primary:
            title = (ov.get("title") or title)[:150]
            desc = ov.get("description") or p["description"]
        else:
            t = texts.get(p["key"]) or {}
            title = (ov.get(f"title_{lang}") or clean_title(t.get("name") or "") or title)[:150]
            desc = ov.get(f"description_{lang}") or t.get("description") or ""
        if desc:
            sep = "، " if lang == "ar" and not primary else ", "
            desc = re.sub(r"\s*\n+\s*", sep, desc.strip())  # line-per-item lists -> commas
            desc = re.sub(r"\s+", " ", desc).strip().rstrip(".") + "."
        else:
            desc = FALLBACK_DESC.get(lang if not primary else "en", FALLBACK_DESC["en"]).format(
                title=title, brand=brand_name_local, category=p["category"])
        if tagline:
            desc = f"{desc} {tagline}"

        base_link = (f"https://{brand['domain']}/{brand['ordering_mode']}/"
                     f"{p['link_outlet']}/menus/{p['menu_slug']}?item-id={p['item_id']}")
        rows.append({
            "id": p["key"],
            "title": title,
            "description": desc[:5000],
            "in_stock": p["in_stock"],
            "price": fmt_money(p["price_minor"], p["currency"]),
            "link_google": _with_utm(base_link, utm.get("google")),
            "link_meta": _with_utm(base_link, utm.get("meta")),
            "image_link": ov.get("image_link") or p["photo"],
            "brand": brand["brand"],
            "google_product_category":
                GOOGLE_BEVERAGES if p["category"].lower() in bev_cats else GOOGLE_FOOD,
            "product_type": f"{brand['brand']} > {p['category']}",
            "custom_label_0": p["category"],
            "custom_label_1": price_band(p["price_minor"]),
            "custom_label_2": "new" if p["category"].lower() in new_cats else "core",
            "custom_label_3": ("vegetarian" if p["vegetarian"]
                               and brand.get("vegetarian_label", True) else ""),
        })
    return rows, excluded


def _with_utm(link, utm):
    return f"{link}&{utm}" if utm else link


def render_google_xml(brand, rows):
    def el(tag, val):
        return f"      <{tag}>{escape(str(val))}</{tag}>\n"

    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">\n  <channel>\n')
    out.write(f"    <title>{escape(brand['brand'])} product feed</title>\n")
    out.write(f"    <link>https://{escape(brand['domain'])}/</link>\n")
    out.write(f"    <description>{escape(brand['brand'])} menu items for Shopping ads</description>\n")
    for r in rows:
        out.write("    <item>\n")
        out.write(el("g:id", r["id"]))
        out.write(el("title", r["title"]))
        out.write(el("description", r["description"]))
        out.write(el("link", r["link_google"]))
        out.write(el("g:image_link", r["image_link"]))
        out.write(el("g:availability", "in_stock" if r["in_stock"] else "out_of_stock"))
        out.write(el("g:price", r["price"]))
        out.write(el("g:brand", r["brand"]))
        out.write(el("g:condition", "new"))
        out.write(el("g:identifier_exists", "no"))
        out.write(el("g:google_product_category", r["google_product_category"]))
        out.write(el("g:product_type", r["product_type"]))
        for i in range(4):
            if r[f"custom_label_{i}"]:
                out.write(el(f"g:custom_label_{i}", r[f"custom_label_{i}"]))
        out.write("    </item>\n")
    out.write("  </channel>\n</rss>\n")
    return out.getvalue()


def render_meta_csv(rows):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=META_COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({
            "id": r["id"], "title": r["title"], "description": r["description"],
            "availability": "in stock" if r["in_stock"] else "out of stock",
            "condition": "new", "price": r["price"], "link": r["link_meta"],
            "image_link": r["image_link"], "brand": r["brand"],
            "google_product_category": r["google_product_category"],
            "product_type": r["product_type"],
            "custom_label_0": r["custom_label_0"], "custom_label_1": r["custom_label_1"],
            "custom_label_2": r["custom_label_2"], "custom_label_3": r["custom_label_3"],
        })
    return out.getvalue()


def render_meta_language_csv(rows, override):
    """Meta language override feed: adds e.g. Arabic titles/descriptions to the SAME
    catalogue items (matched by id), so one catalogue serves both languages."""
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=["id", "override", "title", "description"],
                       lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({"id": r["id"], "override": override,
                    "title": r["title"], "description": r["description"]})
    return out.getvalue()


# --------------------------------------------------------------------------
# Change tracking + safety guard
# --------------------------------------------------------------------------
def read_previous(out_dir):
    path = os.path.join(out_dir, "meta.csv")
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def diff(prev, rows):
    if prev is None:
        return {"first_run": True}
    now = {r["id"]: r for r in rows}
    added = sorted(now[k]["title"] for k in now.keys() - prev.keys())
    removed = sorted(prev[k]["title"] for k in prev.keys() - now.keys())
    price = sorted(f"{now[k]['title']}: {prev[k]['price']} -> {now[k]['price']}"
                   for k in now.keys() & prev.keys() if now[k]["price"] != prev[k]["price"])
    renamed = sorted(f"{prev[k]['title']} -> {now[k]['title']}"
                     for k in now.keys() & prev.keys() if now[k]["title"] != prev[k]["title"])
    return {"added": added, "removed": removed, "price_changed": price, "renamed": renamed}


def write_changelog(out_dir, stamp, changes, total):
    path = os.path.join(out_dir, "CHANGELOG.md")
    lines = [f"## {stamp} - {total} products"]
    if changes.get("first_run"):
        lines.append("- First feed generated.")
    else:
        empty = True
        for label, key in (("Added", "added"), ("Removed", "removed"),
                           ("Price changed", "price_changed"), ("Renamed", "renamed")):
            for x in changes.get(key, []):
                lines.append(f"- {label}: {x}")
                empty = False
        if empty:
            lines.append("- No menu changes.")
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    header = "# Feed change log\n\n"
    old = old[len(header):] if old.startswith(header) else old
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n\n" + old)


# --------------------------------------------------------------------------
def run_brand(brand, cfg, make_client, out_root, log):
    log(f"[{brand['key']}] {brand['brand']} - {brand['domain']}")
    products, warnings, ok, total, menus_read = collect(brand, make_client(None), log)
    overrides = load_overrides(brand["key"])
    utm, keywords = cfg.get("utm", {}), cfg.get("exclude_name_keywords", [])
    rows, excluded = build_rows(brand, products, overrides, utm, keywords)
    if not rows:
        raise RuntimeError("No products to publish. Feed NOT updated.")

    out_dir = os.path.join(out_root, brand["key"])
    os.makedirs(out_dir, exist_ok=True)
    prev = read_previous(out_dir)
    ratio = cfg.get("min_product_ratio", 0.6)
    if prev and len(rows) < ratio * len(prev):
        raise RuntimeError(
            f"Only {len(rows)} products found vs {len(prev)} last time "
            f"(below the {int(ratio * 100)}% safety limit). Feed NOT updated - check the site.")

    changes = diff(prev, rows)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(os.path.join(out_dir, "google.xml"), "w", encoding="utf-8") as f:
        f.write(render_google_xml(brand, rows))
    with open(os.path.join(out_dir, "meta.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(render_meta_csv(rows))

    # Extra languages (e.g. Arabic for KSA): same products and ids, translated text.
    languages_written = ["en"]
    for lang in brand.get("extra_languages", []):
        try:
            texts = collect_texts(make_client(lang), menus_read)
            lrows, _ = build_rows(brand, products, overrides, utm, keywords, lang=lang, texts=texts)
            missing = sum(1 for r in lrows if r["id"] not in texts)
            if missing:
                warnings.append(f"{lang}: {missing} products had no translation (English used)")
            with open(os.path.join(out_dir, f"google_{lang}.xml"), "w", encoding="utf-8") as f:
                f.write(render_google_xml(brand, lrows))
            with open(os.path.join(out_dir, f"meta_{lang}.csv"), "w", encoding="utf-8",
                      newline="") as f:
                f.write(render_meta_language_csv(
                    lrows, brand.get(f"meta_override_{lang}", f"{lang}_AR" if lang == "ar" else f"{lang}_XX")))
            languages_written.append(lang)
            log(f"  wrote {lang} feeds ({len(lrows)} products)")
        except Exception as e:  # a failed translation keeps last month's files
            warnings.append(f"{lang} feeds not updated: {e}")
            log(f"  ! {lang} feeds not updated: {e}")

    report = {
        "languages": languages_written,
        "brand": brand["brand"], "generated_at": stamp,
        "branches_read": f"{ok}/{total}", "products": len(rows),
        "in_stock": sum(r["in_stock"] for r in rows),
        "out_of_stock": sum(not r["in_stock"] for r in rows),
        "excluded": excluded, "warnings": sorted(set(warnings)), "changes": changes,
    }
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    write_changelog(out_dir, stamp, changes, len(rows))
    log(f"  wrote {len(rows)} products ({report['out_of_stock']} out of stock, "
        f"{len(excluded)} excluded) -> {out_dir}")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--brand", help="Only this brand key (default: all enabled)")
    ap.add_argument("--out", default=os.path.join(ROOT, "feeds"))
    ap.add_argument("--fixture", help="Offline test: saved API responses (JSON)")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    brands = [b for b in cfg["brands"] if b.get("enabled", True)
              and (not args.brand or b["key"] == args.brand)]
    if not brands:
        sys.exit(f"No enabled brand matches '{args.brand}'.")

    fixture = None
    if args.fixture:
        with open(args.fixture, encoding="utf-8") as f:
            fixture = json.load(f)

    failed = []
    for b in brands:
        if fixture:
            make_client = lambda lang, b=b: FixtureClient(fixture[b["key"]], lang)
        else:
            make_client = lambda lang, b=b: DeliverectClient(b, lang)
        try:
            run_brand(b, cfg, make_client, args.out, print)
        except Exception as e:
            print(f"  ERROR [{b['key']}]: {e}", file=sys.stderr)
            failed.append(b["key"])
    if failed:
        sys.exit(f"Failed brands: {', '.join(failed)} (their previous feeds were left unchanged)")


if __name__ == "__main__":
    main()
