"""
Albert Heijn API Scraper
========================
Uses the official api.ah.nl mobile API instead of scraping the website.
Way faster, no Cloudflare blocks, no Playwright needed.

API discovered & documented by: https://gist.github.com/jabbink/8bfa44bdfc535d696b340c46d228fdd1

Setup:
    pip install httpx pandas tqdm

Usage:
    python ah_api_scraper.py
    python ah_api_scraper.py --concurrency 20
    python ah_api_scraper.py --out ah_full.csv --concurrency 30
    python ah_api_scraper.py --query melk           # just search for "melk"
    python ah_api_scraper.py --taxonomy 6401        # manually test one API taxonomy
"""

import argparse
import asyncio
import csv
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    async_playwright = None
    PlaywrightTimeout = Exception

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_URL   = "https://api.ah.nl"
USER_AGENT = "Appie/8.22.3"
PAGE_SIZE  = 200         # Larger values can make AH return HTTP 500 on page 0.
MAX_SEARCH_PAGES = 30    # AH search returns HTTP 400 for broad searches beyond this.
WEB_PRODUCTS_URL = "https://www.ah.nl/producten"

# Top-level AH category (taxonomy) IDs - these are the same IDs that appear in
# the website URLs like /producten/6401/groente-aardappelen
TAXONOMIES: dict[int, str] = {
    6401:  "groente-aardappelen",
    19255: "fruit-verse-sappen",
    1301:  "maaltijden-salades",
    4847:  "vlees",
    4850:  "vis",
    19053: "vegetarisch-vegan-en-plantaardig",
    4852:  "vleeswaren",
    4853:  "kaas",
    4854:  "zuivel-eieren",
    4855:  "bakkerij",
    19562: "glutenvrij",
    18509: "borrel-chips-snacks",
    6405:  "pasta-rijst-wereldkeuken",
    6406:  "soepen-sauzen-kruiden-olie",
    4858:  "koek-snoep-chocolade",
    4859:  "ontbijtgranen-beleg",
    6409:  "tussendoortjes",
    4861:  "diepvries",
    4862:  "koffie-thee",
    4863:  "frisdrank-sappen-water",
    4864:  "bier-wijn-aperitieven",
    4865:  "drogisterij",
    16797: "gezondheid-sport",
    4867:  "huishouden",
    4868:  "baby-kind",
    4869:  "huisdier",
    4870:  "koken-tafelen-vrije-tijd",
}

CATEGORY_SEARCHES: list[dict[str, Any]] = [
    {"slug": "groente-aardappelen", "taxonomy": 6401},
    {"slug": "fruit-verse-sappen", "main_category": "Fruit, verse sappen", "queries": ["fruit", "sap", "sappen"]},
    {"slug": "maaltijden-salades", "taxonomy": 1301},
    {"slug": "vlees", "main_category": "Vlees", "queries": ["vlees", "kip", "gehakt", "biefstuk"]},
    {"slug": "vis", "main_category": "Vis", "queries": ["vis", "zalm", "tonijn", "garnalen"]},
    {"slug": "vegetarisch-vegan-en-plantaardig", "main_category": "Vegetarisch, vegan en plantaardig", "queries": ["vegetarisch", "vegan", "plantaardig", "terra"]},
    {"slug": "vleeswaren", "main_category": "Vleeswaren", "queries": ["vleeswaren", "ham", "salami", "kipfilet"]},
    {
        "slug": "kaas",
        "main_category": "Kaas",
        "queries": ["kaas", "parmigiano", "parmezaanse kaas", "grana padano", "pecorino", "zanetti"],
    },
    {"slug": "zuivel-eieren", "main_category": "Zuivel, eieren", "queries": ["zuivel", "eieren", "melk", "yoghurt"]},
    {"slug": "bakkerij", "main_category": "Bakkerij", "queries": ["bakkerij", "brood", "bolletjes"]},
    {
        "slug": "glutenvrij",
        "main_category": "Glutenvrij",
        "queries": [
            "glutenvrij",
            "glutenvrije",
            "glutenvrije koekjes",
            "glutenvrije oaties",
            "glutenvrij brood",
            "glutenvrije crackers",
            "glutenvrije pasta",
            "glutenvrije bakkerij",
            "verkade glutenvrij",
        ],
    },
    {"slug": "borrel-chips-snacks", "main_category": "Borrel, chips, snacks", "queries": ["chips", "borrel", "nootjes", "snacks"]},
    {
        "slug": "pasta-rijst-wereldkeuken",
        "taxonomy": 6405,
        "main_category": "Pasta, rijst, wereldkeuken",
        "queries": ["pasta", "spaghetti", "linguine", "macaroni", "rijst", "honig spaghetti", "spaghetti vlugkokend"],
    },
    {"slug": "soepen-sauzen-kruiden-olie", "taxonomy": 6406},
    {"slug": "koek-snoep-chocolade", "main_category": "Koek, snoep, chocolade", "queries": ["koek", "snoep", "chocolade"]},
    {"slug": "ontbijtgranen-beleg", "main_category": "Ontbijtgranen, beleg", "queries": ["ontbijtgranen", "beleg", "hagelslag", "pindakaas"]},
    {"slug": "tussendoortjes", "taxonomy": 6409},
    {"slug": "diepvries", "main_category": "Diepvries", "queries": ["diepvries", "ijs", "pizza"]},
    {"slug": "koffie-thee", "taxonomy": 4862},
    {"slug": "frisdrank-sappen-water", "main_category": "Frisdrank, sappen, water", "queries": ["frisdrank", "sappen", "water", "cola"]},
    {"slug": "bier-wijn-aperitieven", "main_category": "Bier, wijn, aperitieven", "queries": ["bier", "wijn", "aperitieven"]},
    {"slug": "drogisterij", "main_category": "Drogisterij", "queries": ["drogisterij", "shampoo", "tandpasta", "deodorant"]},
    {"slug": "gezondheid-sport", "main_category": "Gezondheid en sport", "queries": ["gezondheid", "sport", "vitamine", "proteine"]},
    {"slug": "huishouden", "main_category": "Huishouden", "queries": ["huishouden", "wasmiddel", "schoonmaak", "toiletpapier"]},
    {"slug": "baby-kind", "main_category": "Baby en kind", "queries": ["baby", "kind", "luiers", "nutrilon"]},
    {"slug": "huisdier", "main_category": "Huisdier", "queries": ["huisdier", "kat", "hond", "kattenvoer"]},
    {"slug": "koken-tafelen-vrije-tijd", "main_category": "Koken, tafelen, vrije tijd", "queries": ["koken", "tafelen", "keuken", "servies"]},
]

CSV_COLUMNS = [
    "webshopId", "hqId", "gtin", "title", "brand",
    "currentPrice", "priceBeforeBonus", "unitPriceDescription", "salesUnitSize",
    "isBonus", "bonusMechanism", "bonusStartDate", "bonusEndDate",
    "mainCategory", "subCategory", "taxonomyId", "taxonomySlug",
    "nutriscore", "propertyIcons", "kenmerken",
    "glutenvrij", "lactosevrij",
    "allergens_contains", "allergens_may_contain", "allergens_free_from",
    "availableOnline", "isOrderable", "shopType",
    "imageUrl", "imageUrl800", "imageUrl400", "imageUrl200", "imageUrl48",
    "descriptionHighlights", "descriptionFull",
]

EXTRA_WEBSHOP_IDS = {
    169813,  # AH Bosui
    395080,  # Verkade Glutenvrije oaties naturel
    419519,  # Honig Spaghetti vlugkokend
    478224,  # Zanetti Parmigiano reggiano 30+
}

_RE_WEB_PRODUCT_ID = re.compile(r"/producten/product/wi(\d+)/")
_RE_WEB_CATEGORY = re.compile(r"/producten/(\d+)/([^/?#]+)$")


# ─── Auth ─────────────────────────────────────────────────────────────────────

async def get_anonymous_token(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{BASE_URL}/mobile-auth/v1/auth/token/anonymous",
        json={"clientId": "appie"},
        headers={
            "User-Agent": USER_AGENT,
            "X-Application": "AHWEBSHOP",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"]


# ─── Search ───────────────────────────────────────────────────────────────────

async def fetch_page(
    client: httpx.AsyncClient,
    token: str,
    sem: asyncio.Semaphore,
    *,
    query: str | None = None,
    taxonomy: int | None = None,
    page: int = 0,
    size: int = PAGE_SIZE,
    sort_by: str = "RELEVANCE",
    retries: int = 4,
) -> dict[str, Any] | None:
    """Fetch one search result page. Auto-retries on transient errors."""
    params: dict[str, Any] = {
        "sortOn": sort_by,
        "page":   page,
        "size":   size,
    }
    if query is not None:
        params["query"] = query
    if taxonomy is not None:
        params["taxonomyId"] = taxonomy
        params["adType"] = "TAXONOMY"

    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
        "X-Application": "AHWEBSHOP",
        "Content-Type": "application/json",
    }
    url = f"{BASE_URL}/mobile-services/product/search/v2"

    backoff = 2.0
    async with sem:
        for attempt in range(1, retries + 1):
            try:
                r = await client.get(url, params=params, headers=headers, timeout=30.0)
                if r.status_code == 401:
                    raise RuntimeError("Token expired or unauthorized")
                if r.status_code == 400:
                    print(f"  HTTP 400 on {taxonomy or query} page {page}; skipping this page")
                    return None
                if r.status_code in {429, 500, 502, 503, 504}:
                    if r.status_code == 500 and size > 200:
                        print(
                            f"  HTTP 500 on {taxonomy or query} page {page} with size={size}. "
                            "Try --page-size 200 or lower."
                        )
                    print(f"  HTTP {r.status_code} on {taxonomy or query} page {page}, "
                          f"retry {attempt}/{retries} in {backoff:.1f}s")
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                try:
                    r.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    print(
                        f"  HTTP {r.status_code} on {taxonomy or query} page {page}; "
                        f"skipping. {str(exc)}"
                    )
                    return None
                return r.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                print(f"  network error {exc!r} on {taxonomy or query} page {page}, "
                      f"retry {attempt}/{retries}")
                await asyncio.sleep(backoff)
                backoff *= 2
        print(f"  GIVING UP on {taxonomy or query} page {page} after {retries} retries")
        return None


async def fetch_product_detail(
    client: httpx.AsyncClient,
    token: str,
    sem: asyncio.Semaphore,
    webshop_id: str | int,
    retries: int = 3,
) -> dict[str, Any] | None:
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
        "X-Application": "AHWEBSHOP",
        "Content-Type": "application/json",
    }
    url = f"{BASE_URL}/mobile-services/product/detail/v4/fir/{webshop_id}"
    backoff = 1.0
    async with sem:
        for attempt in range(1, retries + 1):
            try:
                r = await client.get(url, headers=headers, timeout=30.0)
                if r.status_code in {403, 429, 500, 502, 503, 504}:
                    print(
                        f"  HTTP {r.status_code} on detail {webshop_id}, "
                        f"retry {attempt}/{retries} in {backoff:.1f}s"
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                if r.status_code in {400, 404}:
                    print(f"  HTTP {r.status_code} on detail {webshop_id}; skipping GTIN")
                    return None
                try:
                    r.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    print(f"  HTTP {r.status_code} on detail {webshop_id}; skipping GTIN. {exc}")
                    return None
                return r.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                print(f"  network error {exc!r} on detail {webshop_id}, retry {attempt}/{retries}")
                await asyncio.sleep(backoff)
                backoff *= 2
            except Exception as exc:
                print(f"  detail {webshop_id} failed: {exc}")
                return None
        return None


def first_list_value(value: Any) -> Any:
    """AH properties are often lists with one useful value."""
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if item not in (None, ""):
                return item
        return None
    return value


def extract_nutriscore(detail: dict, card: dict) -> str | None:
    card_props = card.get("properties") or {}
    detail_props = detail.get("properties") or {}
    for value in (
        card_props.get("nutriscore"),
        detail_props.get("nutriscore"),
        card.get("nutriscore"),
    ):
        score = first_list_value(value)
        if score:
            return str(score)
    return None


def extract_image_urls(card: dict) -> dict[str, str | None]:
    images = card.get("images") or []
    urls_by_width: dict[int, str] = {}
    for image in images:
        width = image.get("width")
        url = image.get("url")
        if width and url:
            urls_by_width[int(width)] = url

    preferred = urls_by_width.get(400)
    if not preferred and images:
        preferred = images[-1].get("url")

    return {
        "imageUrl": preferred,
        "imageUrl800": urls_by_width.get(800),
        "imageUrl400": urls_by_width.get(400),
        "imageUrl200": urls_by_width.get(200),
        "imageUrl48": urls_by_width.get(48),
    }


def extract_diet_flags(card: dict, detail: dict) -> dict[str, Any]:
    card_props = card.get("properties") or {}
    detail_props = detail.get("properties") or {}
    merged_props = {**detail_props, **card_props}

    gluten_value = first_list_value(merged_props.get("sp_include_intolerance_geen_gluten"))
    lactose_value = first_list_value(merged_props.get("sp_include_intolerance_geen_lactose"))
    property_icons = card.get("propertyIcons") or []

    kenmerken: list[str] = [str(icon) for icon in property_icons if icon]
    for value in (gluten_value, lactose_value):
        if value and str(value) not in kenmerken:
            kenmerken.append(str(value))

    return {
        "kenmerken": ";".join(kenmerken) if kenmerken else None,
        "glutenvrij": bool(gluten_value),
        "lactosevrij": bool(lactose_value),
    }


def extract_allergens(detail: dict) -> dict[str, str | None]:
    buckets = {
        "CONTAINS": [],
        "MAY_CONTAIN": [],
        "FREE_FROM": [],
    }
    trade_item = detail.get("tradeItem") or {}
    for group in trade_item.get("allergenInformation") or []:
        for item in group.get("items") or []:
            label = ((item.get("typeCode") or {}).get("label"))
            level = ((item.get("levelOfContainmentCode") or {}).get("value"))
            if label and level in buckets and label not in buckets[level]:
                buckets[level].append(label)

    return {
        "allergens_contains": ";".join(buckets["CONTAINS"]) if buckets["CONTAINS"] else None,
        "allergens_may_contain": ";".join(buckets["MAY_CONTAIN"]) if buckets["MAY_CONTAIN"] else None,
        "allergens_free_from": ";".join(buckets["FREE_FROM"]) if buckets["FREE_FROM"] else None,
    }


def normalize_detail_product(data: dict, taxonomy_slug: str | None = None) -> dict:
    card = data.get("productCard") or {}
    row = normalize_product(card, None, taxonomy_slug)
    trade_item = data.get("tradeItem") or {}
    if trade_item.get("gtin"):
        row["gtin"] = trade_item["gtin"]
    row["nutriscore"] = extract_nutriscore(data, card)
    row.update(extract_diet_flags(card, data))
    row.update(extract_allergens(data))
    row.update(extract_image_urls(card))
    return row


async def enrich_gtins(
    client: httpx.AsyncClient,
    token: str,
    products: list[dict],
    concurrency: int,
    output_csv: str,
    batch_size: int = 500,
) -> list[dict]:
    missing = [p for p in products if p.get("webshopId") and not p.get("gtin")]
    if not missing:
        return products

    print(f"\nFetching GTIN details for {len(missing)} products (concurrency {concurrency})")
    sem = asyncio.Semaphore(concurrency)

    async def enrich_one(product: dict) -> None:
        data = await fetch_product_detail(client, token, sem, product["webshopId"])
        if not data:
            return
        trade_item = data.get("tradeItem") or {}
        gtin = trade_item.get("gtin")
        if gtin:
            product["gtin"] = gtin

    for start in range(0, len(missing), batch_size):
        batch = missing[start:start + batch_size]
        results = await asyncio.gather(
            *(enrich_one(product) for product in batch),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                print(f"  GTIN task failed: {result}")
        enriched_count = sum(1 for p in products if p.get("gtin"))
        df = pd.DataFrame(products).drop_duplicates(subset="webshopId").reindex(columns=CSV_COLUMNS)
        df.to_csv(Path(output_csv), index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
        print(
            f"  GTIN batch {start + len(batch)}/{len(missing)} done; "
            f"{enriched_count} products now have GTIN"
        )

    return products


def normalize_product(prod: dict, taxonomy_id: int | None, taxonomy_slug: str | None) -> dict:
    """Flatten one API product dict into a CSV row."""
    image_urls = extract_image_urls(prod)
    property_icons = prod.get("propertyIcons") or []
    props = prod.get("properties") or {}

    return {
        "webshopId":             prod.get("webshopId"),
        "hqId":                  prod.get("hqId"),
        "gtin":                  prod.get("gtin"),
        "title":                 prod.get("title"),
        "brand":                 prod.get("brand"),
        "currentPrice":          prod.get("currentPrice"),
        "priceBeforeBonus":      prod.get("priceBeforeBonus"),
        "unitPriceDescription":  prod.get("unitPriceDescription"),
        "salesUnitSize":         prod.get("salesUnitSize"),
        "isBonus":               prod.get("isBonus"),
        "bonusMechanism":        prod.get("bonusMechanism"),
        "bonusStartDate":        prod.get("bonusStartDate"),
        "bonusEndDate":          prod.get("bonusEndDate"),
        "mainCategory":          prod.get("mainCategory"),
        "subCategory":           prod.get("subCategory"),
        "taxonomyId":            taxonomy_id,
        "taxonomySlug":          taxonomy_slug,
        "nutriscore":            first_list_value(props.get("nutriscore")) or prod.get("nutriscore"),
        "propertyIcons":         ";".join(property_icons) if property_icons else None,
        "kenmerken":             None,
        "glutenvrij":            None,
        "lactosevrij":           None,
        "allergens_contains":    None,
        "allergens_may_contain": None,
        "allergens_free_from":   None,
        "availableOnline":       prod.get("availableOnline"),
        "isOrderable":           prod.get("isOrderable"),
        "shopType":              prod.get("shopType"),
        "imageUrl":              image_urls["imageUrl"],
        "imageUrl800":           image_urls["imageUrl800"],
        "imageUrl400":           image_urls["imageUrl400"],
        "imageUrl200":           image_urls["imageUrl200"],
        "imageUrl48":            image_urls["imageUrl48"],
        "descriptionHighlights": prod.get("descriptionHighlights"),
        "descriptionFull":       prod.get("descriptionFull"),
    }


async def scrape_taxonomy(
    client: httpx.AsyncClient,
    token: str,
    sem: asyncio.Semaphore,
    taxonomy_id: int,
    taxonomy_slug: str,
    page_size: int = PAGE_SIZE,
) -> list[dict]:
    """Scrape every page of one taxonomy in parallel."""
    # First, get page 0 to find out totalPages
    first = await fetch_page(client, token, sem,
                             taxonomy=taxonomy_id, page=0, size=page_size)
    if not first:
        return []

    page_info = first.get("page", {})
    total_pages = page_info.get("totalPages", 1)
    total = page_info.get("totalElements", 0)
    print(f"  [{taxonomy_slug}] {total} products across {total_pages} pages")

    products = [normalize_product(p, taxonomy_id, taxonomy_slug)
                for p in (first.get("products") or [])]

    if total_pages > 1:
        # Fetch remaining pages in parallel
        tasks = [
            fetch_page(client, token, sem,
                       taxonomy=taxonomy_id, page=p, size=page_size)
            for p in range(1, total_pages)
        ]
        results = await asyncio.gather(*tasks)
        for r in results:
            if not r:
                continue
            products.extend(
                normalize_product(p, taxonomy_id, taxonomy_slug)
                for p in (r.get("products") or [])
            )

    print(f"  [{taxonomy_slug}] collected {len(products)} products")
    return products


async def scrape_category_search(
    client: httpx.AsyncClient,
    token: str,
    sem: asyncio.Semaphore,
    category: dict[str, str],
    page_size: int = PAGE_SIZE,
) -> list[dict]:
    """Scrape a category via broad search and exact mainCategory filtering."""
    slug = category["slug"]
    products_by_id = {}
    if category.get("taxonomy"):
        taxonomy_products = await scrape_taxonomy(
            client,
            token,
            sem,
            category["taxonomy"],
            slug,
            page_size,
        )
        for product in taxonomy_products:
            products_by_id[product.get("webshopId")] = product
        if not category.get("queries"):
            return list(products_by_id.values())

    main_category = category.get("main_category")
    raw_by_id = {}
    for query in category["queries"]:
        first = await fetch_page(client, token, sem, query=query, page=0, size=page_size)
        if not first:
            continue

        page_info = first.get("page", {})
        total_pages = page_info.get("totalPages", 1)
        total = page_info.get("totalElements", 0)
        print(f"  [{slug}] query {query!r}: {total} hits across {total_pages} pages")
        total_pages = min(total_pages, MAX_SEARCH_PAGES)

        for product in first.get("products") or []:
            raw_by_id[product.get("webshopId")] = product
        if total_pages > 1:
            tasks = [
                fetch_page(client, token, sem, query=query, page=p, size=page_size)
                for p in range(1, total_pages)
            ]
            for r in await asyncio.gather(*tasks):
                if r:
                    for product in r.get("products") or []:
                        raw_by_id[product.get("webshopId")] = product

    matching = [
        normalize_product(p, None, slug)
        for p in raw_by_id.values()
        if not main_category or p.get("mainCategory") == main_category
    ]
    for product in matching:
        products_by_id[product.get("webshopId")] = product
    print(
        f"  [{slug}] kept {len(products_by_id)} products total "
        f"after {len(raw_by_id)} supplemental search hits"
    )
    return list(products_by_id.values())


async def fetch_extra_products(
    client: httpx.AsyncClient,
    token: str,
    sem: asyncio.Semaphore,
) -> list[dict]:
    extras = []
    for webshop_id in sorted(EXTRA_WEBSHOP_IDS):
        data = await fetch_product_detail(client, token, sem, webshop_id)
        if data:
            extras.append(normalize_detail_product(data, "extra-seed"))
    if extras:
        print(f"  [extra-seed] added {len(extras)} product(s)")
    return extras


# ─── Website inventory mode ───────────────────────────────────────────────────

def build_web_category_url(category_url: str, page: int) -> str:
    base = category_url.split("?")[0].rstrip("/")
    return base if page <= 1 else f"{base}?page={page}"


async def discover_web_categories(context) -> list[dict[str, Any]]:
    page = await context.new_page()
    categories = []
    seen = set()
    try:
        await page.goto(WEB_PRODUCTS_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(1_000)
        try:
            await page.wait_for_selector('a[href*="/producten/"]', timeout=15_000)
        except PlaywrightTimeout:
            pass
        title = await page.title()
        html = await page.content()
        if "Access Denied" in title or "Access Denied" in html[:1000]:
            raise RuntimeError(f"AH blocked category discovery page: {WEB_PRODUCTS_URL}")

        hrefs = await page.eval_on_selector_all(
            'a[href*="/producten/"]',
            """links => links.map(a => ({href: a.href, text: a.innerText || a.textContent || ""}))""",
        )
        for item in hrefs:
            href = (item.get("href") or "").split("?")[0].split("#")[0].rstrip("/")
            match = _RE_WEB_CATEGORY.search(href)
            if not match or href in seen:
                continue
            taxonomy_id = int(match.group(1))
            slug = match.group(2)
            seen.add(href)
            categories.append({
                "category_url": href,
                "taxonomy_id": taxonomy_id,
                "slug": slug,
                "name": " ".join((item.get("text") or "").split()) or slug,
            })
    finally:
        try:
            await page.close()
        except Exception:
            pass
    print(f"  discovered {len(categories)} live website categories")
    return categories


async def open_web_context(headed: bool, user_data_dir: str | None):
    if async_playwright is None:
        raise RuntimeError("Playwright is not installed. Run: pip install playwright && playwright install chromium")

    pw = await async_playwright().start()
    launch = {
        "headless": not headed,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    }
    ctx_opts = {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "locale": "nl-NL",
        "timezone_id": "Europe/Amsterdam",
        "viewport": {"width": 1280, "height": 900},
        "extra_http_headers": {"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"},
    }

    if user_data_dir:
        context = await pw.chromium.launch_persistent_context(user_data_dir, **launch, **ctx_opts)
        browser = None
    else:
        browser = await pw.chromium.launch(**launch)
        context = await browser.new_context(**ctx_opts)
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return pw, browser, context


async def scrape_web_category_ids(
    context,
    category_url: str,
    slug: str,
    max_pages: int | None = None,
) -> set[int]:
    page = await context.new_page()
    ids: set[int] = set()
    page_num = 1
    try:
        while True:
            if max_pages and page_num > max_pages:
                break
            url = build_web_category_url(category_url, page_num)
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(500)
            try:
                await page.wait_for_selector('a[href*="/producten/product/wi"]', timeout=10_000)
            except PlaywrightTimeout:
                pass

            title = await page.title()
            html = await page.content()
            if "Access Denied" in title or "Access Denied" in html[:1000]:
                raise RuntimeError(f"AH blocked website inventory page: {url}")

            page_ids = {int(value) for value in _RE_WEB_PRODUCT_ID.findall(html)}
            new_ids = page_ids - ids
            if not page_ids:
                print(f"  [{slug}] page {page_num}: no product IDs; stopping")
                break
            if page_num > 1 and not new_ids:
                print(f"  [{slug}] page {page_num}: no new product IDs; stopping")
                break

            ids.update(page_ids)
            print(f"  [{slug}] page {page_num}: {len(page_ids)} ids ({len(ids)} total)")
            page_num += 1
    finally:
        try:
            await page.close()
        except Exception:
            pass
    return ids


async def collect_web_inventory(
    headed: bool = False,
    user_data_dir: str | None = None,
    max_pages: int | None = None,
    max_categories: int | None = None,
) -> dict[int, set[str]]:
    print("\nCollecting product IDs from AH website category pages")
    pw, browser, context = await open_web_context(headed, user_data_dir)
    id_to_slugs: dict[int, set[str]] = {}
    try:
        categories = await discover_web_categories(context)
        if max_categories:
            categories = categories[:max_categories]
        for category in categories:
            slug = category["slug"]
            ids = await scrape_web_category_ids(
                context,
                category["category_url"],
                slug,
                max_pages=max_pages,
            )
            for webshop_id in ids:
                id_to_slugs.setdefault(webshop_id, set()).add(slug)
            print(f"  [{slug}] inventory total so far: {len(id_to_slugs)} unique ids")
    finally:
        try:
            await context.close()
        except Exception:
            pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        await pw.stop()
    return id_to_slugs


async def hydrate_web_inventory(
    client: httpx.AsyncClient,
    token: str,
    id_to_slugs: dict[int, set[str]],
    concurrency: int,
    output_csv: str,
    batch_size: int = 500,
) -> list[dict]:
    ids = sorted(id_to_slugs)
    sem = asyncio.Semaphore(concurrency)
    rows: list[dict] = []
    print(f"\nHydrating {len(ids)} website product IDs through API detail endpoint")

    async def hydrate_one(webshop_id: int) -> dict | None:
        data = await fetch_product_detail(client, token, sem, webshop_id)
        if not data:
            return None
        return normalize_detail_product(data, ";".join(sorted(id_to_slugs.get(webshop_id, []))))

    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]
        results = await asyncio.gather(*(hydrate_one(webshop_id) for webshop_id in batch))
        rows.extend(row for row in results if row)
        df = pd.DataFrame(rows).drop_duplicates(subset="webshopId").reindex(columns=CSV_COLUMNS)
        df.to_csv(Path(output_csv), index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
        print(f"  Hydrated {start + len(batch)}/{len(ids)} ids; saved {len(df)} rows")
    return rows


# ─── Orchestrator ─────────────────────────────────────────────────────────────

async def scrape_all(
    output_csv: str,
    concurrency: int = 6,
    gtin_concurrency: int | None = None,
    query: str | None = None,
    only_taxonomy: int | None = None,
    page_size: int = PAGE_SIZE,
    include_gtin_details: bool = True,
    use_web_inventory: bool = False,
    web_headed: bool = False,
    web_user_data_dir: str | None = None,
    web_max_pages: int | None = None,
    web_max_categories: int | None = None,
) -> pd.DataFrame:
    sem = asyncio.Semaphore(concurrency)
    if gtin_concurrency is None:
        gtin_concurrency = max(1, min(2, concurrency))
    out_path = Path(output_csv)

    def save_snapshot(products: list[dict], label: str) -> pd.DataFrame:
        if not products:
            return pd.DataFrame(columns=CSV_COLUMNS)
        df = pd.DataFrame(products)
        before = len(df)
        df = df.drop_duplicates(subset="webshopId").reset_index(drop=True)
        df = df.reindex(columns=CSV_COLUMNS)
        df.to_csv(out_path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
        print(f"  Saved {len(df)} unique products after {label} ({before - len(df)} dupes removed)")
        return df

    async with httpx.AsyncClient(http2=True) as client:
        print("Getting anonymous token...")
        token = await get_anonymous_token(client)
        print(f"Token OK (len {len(token)})")

        all_products: list[dict] = []

        if use_web_inventory:
            id_to_slugs = await collect_web_inventory(
                headed=web_headed,
                user_data_dir=web_user_data_dir,
                max_pages=web_max_pages,
                max_categories=web_max_categories,
            )
            all_products = await hydrate_web_inventory(
                client,
                token,
                id_to_slugs,
                concurrency=gtin_concurrency,
                output_csv=output_csv,
            )
        elif query:
            print(f"\nSearching for query: {query!r}")
            first = await fetch_page(client, token, sem,
                                     query=query, page=0, size=page_size)
            if first:
                page_info = first.get("page", {})
                total_pages = page_info.get("totalPages", 1)
                total = page_info.get("totalElements", 0)
                print(f"  {total} products across {total_pages} pages")
                total_pages = min(total_pages, MAX_SEARCH_PAGES)
                all_products.extend(
                    normalize_product(p, None, None)
                    for p in (first.get("products") or [])
                )
                if total_pages > 1:
                    tasks = [
                        fetch_page(client, token, sem,
                                   query=query, page=p, size=page_size)
                        for p in range(1, total_pages)
                    ]
                    for r in await asyncio.gather(*tasks):
                        if r:
                            all_products.extend(
                                normalize_product(p, None, None)
                                for p in (r.get("products") or [])
                            )
        elif only_taxonomy is not None:
            slug = TAXONOMIES.get(only_taxonomy, str(only_taxonomy))
            products = await scrape_taxonomy(client, token, sem,
                                             only_taxonomy, slug, page_size)
            all_products.extend(products)
            save_snapshot(all_products, slug)
        else:
            print(f"\nScraping all {len(CATEGORY_SEARCHES)} category searches "
                  f"(concurrency {concurrency})")
            for category in CATEGORY_SEARCHES:
                products = await scrape_category_search(client, token, sem, category, page_size)
                all_products.extend(products)
                save_snapshot(all_products, category["slug"])
            extra_products = await fetch_extra_products(client, token, sem)
            if extra_products:
                all_products.extend(extra_products)
                save_snapshot(all_products, "extra-seed")

        # Deduplicate by webshopId - a product can appear under multiple taxonomies
        if not all_products:
            print("No products collected.")
            return pd.DataFrame(columns=CSV_COLUMNS)

        if include_gtin_details and not use_web_inventory:
            all_products = await enrich_gtins(
                client,
                token,
                all_products,
                concurrency=gtin_concurrency,
                output_csv=output_csv,
            )

        df = save_snapshot(all_products, "final")
        print(f"\nSaved final CSV -> {out_path.resolve()}")
        return df


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Albert Heijn API scraper")
    ap.add_argument("--out", default="ah_products.csv",
                    help="Output CSV path")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="Max simultaneous API requests within a category (default 6)")
    ap.add_argument("--gtin-concurrency", type=int, default=None,
                    help="Max simultaneous product-detail GTIN requests (default min(concurrency, 2))")
    ap.add_argument("--query", default=None,
                    help="Search query instead of scraping all categories")
    ap.add_argument("--taxonomy", type=int, default=None,
                    help="Scrape only one taxonomy (category) by ID")
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE,
                    help=f"Products per API page (default {PAGE_SIZE})")
    ap.add_argument("--no-gtin-details", action="store_true",
                    help="Skip product-detail calls used to fill GTIN.")
    ap.add_argument("--web-inventory", action="store_true",
                    help="Scrape product IDs from AH website categories, then hydrate via API details.")
    ap.add_argument("--web-headed", action="store_true",
                    help="Show browser for --web-inventory, useful if AH asks for verification.")
    ap.add_argument("--web-user-data-dir", default=".ah-profile",
                    help="Browser profile folder for --web-inventory cookies/session.")
    ap.add_argument("--web-max-pages", type=int, default=None,
                    help="Limit website listing pages per category for testing --web-inventory.")
    ap.add_argument("--web-max-categories", type=int, default=None,
                    help="Limit website categories for testing --web-inventory.")
    args = ap.parse_args()

    t0 = time.perf_counter()
    df = asyncio.run(scrape_all(
        output_csv=args.out,
        concurrency=args.concurrency,
        gtin_concurrency=args.gtin_concurrency,
        query=args.query,
        only_taxonomy=args.taxonomy,
        page_size=args.page_size,
        include_gtin_details=not args.no_gtin_details,
        use_web_inventory=args.web_inventory,
        web_headed=args.web_headed,
        web_user_data_dir=args.web_user_data_dir,
        web_max_pages=args.web_max_pages,
        web_max_categories=args.web_max_categories,
    ))
    elapsed = time.perf_counter() - t0
    print(f"\nDone in {elapsed:.1f}s - {len(df)} rows")
    if not df.empty:
        print("\nSample:")
        print(df[["webshopId", "title", "currentPrice", "isBonus",
                  "taxonomySlug"]].head(10).to_string())


if __name__ == "__main__":
    main()
