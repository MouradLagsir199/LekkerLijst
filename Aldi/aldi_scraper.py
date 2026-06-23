"""
Aldi.nl catalog scraper
=======================

Uses Aldi's public Algolia search index, discovered from the live Next.js
frontend, instead of scraping rendered product cards. The product sitemap is
used as a sanity check and the scraper can optionally hydrate product pages for
their server-side PRODUCT_DETAIL_GET payload.

Setup:
    pip install httpx

Usage:
    python Aldi/aldi_scraper.py --probe
    python Aldi/aldi_scraper.py --all
    python Aldi/aldi_scraper.py --all --hydrate-details
    python Aldi/aldi_scraper.py --query melk --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin
from xml.etree import ElementTree as ET

import httpx


BASE_URL = "https://www.aldi.nl"
PRODUCTS_URL = f"{BASE_URL}/producten.html"
DISCOVERY_CATEGORY_URL = (
    f"{BASE_URL}/producten/huishouden/afwassen-en-vaatwasmiddelen.html"
)
ROBOTS_URL = f"{BASE_URL}/robots.txt"
SITEMAP_INDEX_URL = f"{BASE_URL}/.aldi-nord-sitemap.xml"
PRODUCT_SITEMAP_URL = f"{BASE_URL}/sitemaps/.aldi-nord-sitemap-products.xml"

ALGOLIA_APP_ID_FALLBACK = "2HU29PF6BH"
ALGOLIA_API_KEY_FALLBACK = "686cf0c8ddcf740223d420d1115c94c1"
ALGOLIA_INDEX_FALLBACK = "an_prd_nl_nl_products2"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "Output" / "aldi_products.csv"
DEFAULT_DISCOVERY_OUT = (
    Path(__file__).resolve().parents[1] / "Output" / "aldi_discovery.json"
)

CSV_COLUMNS = [
    "catalog_rank",
    "product_id",
    "product_slug",
    "url",
    "product_name",
    "brand",
    "sales_unit",
    "price",
    "base_unit_price",
    "base_unit_price_value",
    "base_unit_price_unit",
    "price_valid_from",
    "price_valid_until",
    "price_valid_from_local_date",
    "price_valid_until_local_date",
    "price_promo_label",
    "price_status",
    "promotion_prices_json",
    "is_available",
    "main_category_id",
    "category_ids_json",
    "root_categories_json",
    "category_paths_json",
    "primary_image_url",
    "images_json",
    "short_description",
    "long_description",
    "gtin",
    "ean",
    "ingredients",
    "allergen_info",
    "nutriscore",
    "nutrients_json",
    "source",
    "scraped_at",
]

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
PRODUCT_ID_FROM_URL_RE = re.compile(r"-(\d+)\.html(?:[?#].*)?$")
PRODUCT_OVERVIEW_SCRIPT_RE = re.compile(
    r'<script[^>]+src="([^"]*pages/product-overview/[^"]+\.js)"'
)
ALGOLIA_CREDENTIAL_RE = re.compile(
    r'\(["\']([A-Z0-9]{10})["\'],["\']([0-9a-f]{32})["\']\)'
)


@dataclass
class AlgoliaConfig:
    app_id: str
    api_key: str
    index_name: str
    discovered_from: str
    used_fallback: bool = False


def default_headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    }


def as_json(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def clean_text(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    text = str(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def epoch_to_iso(value: Any) -> str | None:
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def product_url(product: dict[str, Any]) -> str | None:
    slug = product.get("productSlug")
    if not slug:
        return None
    return f"{BASE_URL}/product/{slug}.html"


def product_id_from_url(url: str) -> str | None:
    match = PRODUCT_ID_FROM_URL_RE.search(url)
    return match.group(1) if match else None


def primary_image(assets: list[dict[str, Any]] | None) -> str | None:
    if not assets:
        return None
    for asset in assets:
        if asset.get("type") == "primary" and asset.get("url"):
            return asset["url"]
    for asset in assets:
        if asset.get("type") != "seal" and asset.get("url"):
            return asset["url"]
    for asset in assets:
        if asset.get("url"):
            return asset["url"]
    return None


def first_matching_value(value: Any, key_names: set[str]) -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in key_names and child not in (None, "", [], {}):
                return child
        for child in value.values():
            found = first_matching_value(child, key_names)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, list):
        for child in value:
            found = first_matching_value(child, key_names)
            if found not in (None, "", [], {}):
                return found
    return None


def scalar_or_first(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, list):
        for item in value:
            scalar = scalar_or_first(item)
            if scalar:
                return scalar
        return None
    if isinstance(value, dict):
        for key in ("value", "code", "number", "id"):
            scalar = scalar_or_first(value.get(key))
            if scalar:
                return scalar
        return None
    return str(value).strip() or None


def text_or_json(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, (dict, list)):
        return as_json(value)
    return clean_text(value)


def extract_nutriscore(product: dict[str, Any]) -> str | None:
    value = first_matching_value(
        product,
        {"nutriscore", "nutri_score", "nutri-score", "nutriscorevalue"},
    )
    scalar = scalar_or_first(value)
    if scalar:
        match = re.search(r"\b([A-E])\b", scalar, re.IGNORECASE)
        return match.group(1).upper() if match else scalar

    assets = product.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        url = str(asset.get("url") or "")
        match = re.search(r"nutri[-_ ]?score[_-]?([a-e])\b", url, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def price_label(price: dict[str, Any]) -> str | None:
    labels = price.get("priceTagLabels")
    if not isinstance(labels, dict):
        return None
    parts = [str(v).strip() for v in labels.values() if str(v).strip()]
    return " | ".join(parts) or None


def base_price_parts(price: dict[str, Any]) -> tuple[str | None, Any, str | None]:
    base_prices = price.get("basePrice")
    if not isinstance(base_prices, list) or not base_prices:
        return None, None, None

    display_parts: list[str] = []
    first_value: Any = None
    first_unit: str | None = None
    for base in base_prices:
        if not isinstance(base, dict):
            continue
        value = base.get("basePriceValue")
        unit = base.get("basePriceScale")
        if first_value is None:
            first_value = value
            first_unit = unit
        if value is not None and unit:
            display_parts.append(f"{value}/{unit}")
        elif value is not None:
            display_parts.append(str(value))
    return " | ".join(display_parts) or None, first_value, first_unit


def hierarchical_categories(product: dict[str, Any]) -> tuple[list[str], list[str]]:
    categories = product.get("hierarchicalCategories")
    roots: list[str] = []
    paths: list[str] = []
    if not isinstance(categories, dict):
        return roots, paths

    for key in sorted(categories):
        values = categories.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if not value:
                continue
            text = str(value)
            if key == "lvl0":
                roots.append(text)
            else:
                paths.append(text)
    return roots, paths


def price_status(product: dict[str, Any]) -> str:
    price = product.get("currentPrice")
    if isinstance(price, dict) and isinstance(price.get("priceValue"), (int, float)):
        return "available"
    return "missing_from_aldi_catalog_payload"


def merge_product(base: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in detail.items():
        if key.startswith("_"):
            merged[key] = value
        elif value not in (None, "", [], {}):
            merged[key] = value
    return merged


async def fetch_text(client: httpx.AsyncClient, url: str, retries: int = 4) -> str:
    backoff = 1.5
    for attempt in range(1, retries + 1):
        try:
            resp = await client.get(url, timeout=60)
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                print(f"  HTTP {resp.status_code} for {url}; retry in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff *= 1.8
                continue
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError:
            if attempt == retries:
                raise
            await asyncio.sleep(backoff)
            backoff *= 1.8
    raise RuntimeError(f"Failed to fetch {url}")


def extract_next_data(html: str) -> dict[str, Any]:
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise ValueError("Could not find __NEXT_DATA__")
    return json.loads(match.group(1))


async def discover_algolia_config(client: httpx.AsyncClient) -> AlgoliaConfig:
    try:
        html = await fetch_text(client, DISCOVERY_CATEGORY_URL)
        next_data = extract_next_data(html)
        page_props = next_data.get("props", {}).get("pageProps", {})
        index_name = (
            page_props.get("algoliaConfig", {}).get("indexName")
            or ALGOLIA_INDEX_FALLBACK
        )

        script_match = PRODUCT_OVERVIEW_SCRIPT_RE.search(html)
        if not script_match:
            raise ValueError("Could not find product overview chunk")
        script_url = urljoin(BASE_URL, script_match.group(1))
        script = await fetch_text(client, script_url)
        credential_match = ALGOLIA_CREDENTIAL_RE.search(script)
        if not credential_match:
            raise ValueError("Could not find Algolia credentials in chunk")

        return AlgoliaConfig(
            app_id=credential_match.group(1),
            api_key=credential_match.group(2),
            index_name=index_name,
            discovered_from=script_url,
        )
    except Exception as exc:
        print(f"Algolia config discovery failed ({exc}); using checked fallback.")
        return AlgoliaConfig(
            app_id=ALGOLIA_APP_ID_FALLBACK,
            api_key=ALGOLIA_API_KEY_FALLBACK,
            index_name=ALGOLIA_INDEX_FALLBACK,
            discovered_from="fallback_constants",
            used_fallback=True,
        )


async def fetch_algolia_page(
    client: httpx.AsyncClient,
    config: AlgoliaConfig,
    *,
    page: int,
    hits_per_page: int,
    query: str,
    filters: str | None,
    retries: int = 4,
) -> dict[str, Any]:
    url = (
        f"https://{config.app_id}-dsn.algolia.net/1/indexes/"
        f"{quote(config.index_name, safe='')}/query"
    )
    headers = {
        "x-algolia-application-id": config.app_id,
        "x-algolia-api-key": config.api_key,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    body: dict[str, Any] = {
        "query": query,
        "hitsPerPage": hits_per_page,
        "page": page,
        "facets": [],
    }
    if filters:
        body["filters"] = filters

    backoff = 1.5
    for attempt in range(1, retries + 1):
        try:
            resp = await client.post(url, headers=headers, json=body, timeout=60)
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                print(
                    f"  Algolia HTTP {resp.status_code} page {page}; "
                    f"retry in {backoff:.1f}s"
                )
                await asyncio.sleep(backoff)
                backoff *= 1.8
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError:
            if attempt == retries:
                raise
            await asyncio.sleep(backoff)
            backoff *= 1.8
    raise RuntimeError(f"Failed to fetch Algolia page {page}")


async def fetch_algolia_products(
    client: httpx.AsyncClient,
    config: AlgoliaConfig,
    *,
    query: str,
    filters: str | None,
    limit: int | None,
    hits_per_page: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    first = await fetch_algolia_page(
        client,
        config,
        page=0,
        hits_per_page=hits_per_page,
        query=query,
        filters=filters,
    )
    nb_hits = int(first.get("nbHits") or 0)
    nb_pages = int(first.get("nbPages") or 0)
    print(
        f"Algolia index {config.index_name}: {nb_hits} hits across "
        f"{nb_pages} pages"
    )

    products: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_hits(hits: list[dict[str, Any]]) -> None:
        for hit in hits:
            object_id = str(hit.get("objectID") or "")
            if not object_id or object_id in seen:
                continue
            seen.add(object_id)
            hit["_source"] = "algolia"
            products.append(hit)
            if limit and len(products) >= limit:
                return

    add_hits(first.get("hits") or [])
    for page in range(1, nb_pages):
        if limit and len(products) >= limit:
            break
        data = await fetch_algolia_page(
            client,
            config,
            page=page,
            hits_per_page=hits_per_page,
            query=query,
            filters=filters,
        )
        add_hits(data.get("hits") or [])
        print(f"  fetched Algolia page {page + 1}/{nb_pages}: {len(products)} products")

    return products, {
        "nb_hits": nb_hits,
        "nb_pages": nb_pages,
        "hits_per_page": hits_per_page,
        "query": query,
        "filters": filters,
    }


async def fetch_product_sitemap_urls(client: httpx.AsyncClient) -> list[str]:
    xml_text = await fetch_text(client, PRODUCT_SITEMAP_URL)
    root = ET.fromstring(xml_text)
    urls: list[str] = []
    for loc in root.findall(".//{*}loc"):
        if loc.text:
            urls.append(loc.text.strip())
    return urls


async def fetch_detail_product(
    client: httpx.AsyncClient,
    url: str,
    sem: asyncio.Semaphore,
) -> dict[str, Any] | None:
    async with sem:
        try:
            html = await fetch_text(client, url)
            next_data = extract_next_data(html)
            page_props = next_data.get("props", {}).get("pageProps", {})
            api_data_raw = page_props.get("apiData")
            if not api_data_raw:
                return None
            api_entries = json.loads(api_data_raw)
            for entry in api_entries:
                if not isinstance(entry, list) or len(entry) != 2:
                    continue
                if entry[0] != "PRODUCT_DETAIL_GET":
                    continue
                response = entry[1].get("res", {})
                products = response.get("products") or []
                if not products:
                    return None
                product = products[0]
                product["_source"] = "product_detail"
                product["_parentCategory"] = response.get("parentCategory")
                return product
        except Exception as exc:
            print(f"  detail failed for {url}: {exc}")
            return None
    return None


async def hydrate_details(
    client: httpx.AsyncClient,
    products: list[dict[str, Any]],
    *,
    concurrency: int,
    checkpoint_every: int,
    out_path: Path | None,
) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)
    tasks: list[asyncio.Task[dict[str, Any] | None]] = []
    positions: list[int] = []

    for idx, product in enumerate(products):
        url = product_url(product)
        if not url:
            continue
        tasks.append(asyncio.create_task(fetch_detail_product(client, url, sem)))
        positions.append(idx)

    print(f"Hydrating {len(tasks)} product detail pages")
    completed = 0
    for task, idx in zip(tasks, positions):
        detail = await task
        completed += 1
        if detail:
            products[idx] = merge_product(products[idx], detail)
        if completed % checkpoint_every == 0:
            print(f"  hydrated {completed}/{len(tasks)}")
            if out_path:
                write_csv(products, out_path)
    return products


def product_to_row(product: dict[str, Any], rank: int, scraped_at: str) -> dict[str, Any]:
    price = product.get("currentPrice")
    if not isinstance(price, dict):
        price = {}
    base_display, base_value, base_unit = base_price_parts(price)
    roots, paths = hierarchical_categories(product)
    assets = product.get("assets") if isinstance(product.get("assets"), list) else []
    category_ids = (
        product.get("categoryIDs") if isinstance(product.get("categoryIDs"), list) else []
    )
    gtin = scalar_or_first(
        first_matching_value(
            product,
            {"gtin", "gtins", "globaltradeitemnumber", "globaltradeitemnumbers"},
        )
    )
    ean = scalar_or_first(
        first_matching_value(product, {"ean", "eans", "barcode", "barcodes"})
    )
    ingredients = first_matching_value(
        product,
        {"ingredients", "ingredientstatement", "ingredient_statement"},
    )
    allergen_info = first_matching_value(
        product,
        {"allergens", "allergeninfo", "allergen_info", "allergeninformation"},
    )
    nutrients = first_matching_value(
        product,
        {
            "nutrients",
            "nutrition",
            "nutritionalinformation",
            "nutritional_information",
            "nutritionalvalues",
            "nutritional_values",
        },
    )
    source = product.get("_source") or "algolia"
    parent_category = product.get("_parentCategory")
    if parent_category:
        source = f"{source}+detail_parent_category"

    return {
        "catalog_rank": rank,
        "product_id": product.get("objectID"),
        "product_slug": product.get("productSlug"),
        "url": product_url(product),
        "product_name": clean_text(product.get("name")),
        "brand": clean_text(product.get("brandName")),
        "sales_unit": clean_text(product.get("salesUnit")),
        "price": price.get("priceValue"),
        "base_unit_price": base_display,
        "base_unit_price_value": base_value,
        "base_unit_price_unit": base_unit,
        "price_valid_from": epoch_to_iso(price.get("validFrom")),
        "price_valid_until": epoch_to_iso(price.get("validUntil")),
        "price_valid_from_local_date": price.get("validFromLocalDate"),
        "price_valid_until_local_date": price.get("validUntilLocalDate"),
        "price_promo_label": price_label(price),
        "price_status": price_status(product),
        "promotion_prices_json": as_json(product.get("promotionPrices")),
        "is_available": product.get("isAvailable"),
        "main_category_id": product.get("mainCategoryID"),
        "category_ids_json": as_json(category_ids),
        "root_categories_json": as_json(roots),
        "category_paths_json": as_json(paths),
        "primary_image_url": primary_image(assets),
        "images_json": as_json(assets),
        "short_description": clean_text(product.get("shortDescription")),
        "long_description": clean_text(product.get("longDescription")),
        "gtin": gtin,
        "ean": ean,
        "ingredients": text_or_json(ingredients),
        "allergen_info": text_or_json(allergen_info),
        "nutriscore": extract_nutriscore(product),
        "nutrients_json": as_json(nutrients),
        "source": source,
        "scraped_at": scraped_at,
    }


def write_csv(products: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    scraped_at = datetime.now(timezone.utc).isoformat()
    with out_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for rank, product in enumerate(products, start=1):
            writer.writerow(product_to_row(product, rank, scraped_at))


def write_discovery(
    out_path: Path,
    *,
    config: AlgoliaConfig,
    algolia_summary: dict[str, Any],
    product_count: int,
    sitemap_urls: list[str] | None,
    started_at: str,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sitemap_count = len(sitemap_urls) if sitemap_urls is not None else None
    product_ids = {
        product_id_from_url(url)
        for url in sitemap_urls or []
        if product_id_from_url(url)
    }
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at,
        "strategy": "public_algolia_index",
        "base_url": BASE_URL,
        "source_urls": {
            "products": PRODUCTS_URL,
            "discovery_category": DISCOVERY_CATEGORY_URL,
            "robots": ROBOTS_URL,
            "sitemap_index": SITEMAP_INDEX_URL,
            "product_sitemap": PRODUCT_SITEMAP_URL,
        },
        "algolia": {
            "app_id": config.app_id,
            "index_name": config.index_name,
            "discovered_from": config.discovered_from,
            "used_fallback": config.used_fallback,
            "nb_hits": algolia_summary.get("nb_hits"),
            "nb_pages": algolia_summary.get("nb_pages"),
            "hits_per_page": algolia_summary.get("hits_per_page"),
            "query": algolia_summary.get("query"),
            "filters": algolia_summary.get("filters"),
        },
        "product_count_written": product_count,
        "product_sitemap_url_count": sitemap_count,
        "product_sitemap_unique_id_count": len(product_ids) if sitemap_urls is not None else None,
        "note": (
            "GTIN/EAN, allergen, ingredients, and nutrient columns are present "
            "but left blank when Aldi's public product payload does not expose "
            "those fields. Nutri-Score is also extracted from public seal assets "
            "when available."
        ),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def run(args: argparse.Namespace) -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    timeout = httpx.Timeout(60.0, connect=30.0)
    limits = httpx.Limits(max_connections=max(20, args.concurrency * 2))
    async with httpx.AsyncClient(
        headers=default_headers(),
        follow_redirects=True,
        timeout=timeout,
        limits=limits,
    ) as client:
        config = await discover_algolia_config(client)
        print(
            f"Using Algolia app {config.app_id}, index {config.index_name} "
            f"from {config.discovered_from}"
        )

        sitemap_urls: list[str] | None = None
        if not args.skip_sitemap_check:
            sitemap_urls = await fetch_product_sitemap_urls(client)
            print(f"Product sitemap lists {len(sitemap_urls)} product URLs")

        products, algolia_summary = await fetch_algolia_products(
            client,
            config,
            query=args.query or "",
            filters=args.filters,
            limit=args.limit,
            hits_per_page=args.hits_per_page,
        )
        print(f"Collected {len(products)} unique products from Algolia")

        if sitemap_urls is not None and not args.query and not args.filters and not args.limit:
            sitemap_ids = {
                product_id_from_url(url)
                for url in sitemap_urls
                if product_id_from_url(url)
            }
            algolia_ids = {str(product.get("objectID")) for product in products}
            missing_ids = sorted(sitemap_ids - algolia_ids)
            if missing_ids:
                print(
                    f"Warning: {len(missing_ids)} sitemap product IDs were not in "
                    "the Algolia empty-query result"
                )

        if args.hydrate_details:
            products = await hydrate_details(
                client,
                products,
                concurrency=args.concurrency,
                checkpoint_every=args.checkpoint_every,
                out_path=args.out,
            )

    write_csv(products, args.out)
    write_discovery(
        args.discovery_out,
        config=config,
        algolia_summary=algolia_summary,
        product_count=len(products),
        sitemap_urls=sitemap_urls,
        started_at=started_at,
    )
    print(f"Wrote {len(products)} products to {args.out}")
    print(f"Wrote discovery diagnostics to {args.discovery_out}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Aldi.nl product catalog")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrape the full current Aldi search catalog. This is also the default.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Small smoke run; equivalent to --limit 20 unless --limit is supplied.",
    )
    parser.add_argument("--query", default="", help="Optional Algolia search query")
    parser.add_argument(
        "--filters",
        default=None,
        help="Optional raw Algolia filters string for targeted research",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum products to write",
    )
    parser.add_argument(
        "--hits-per-page",
        type=int,
        default=1000,
        help="Algolia hits per page; Aldi's frontend also uses 1000",
    )
    parser.add_argument(
        "--hydrate-details",
        action="store_true",
        help="Fetch product pages and merge the server-side PRODUCT_DETAIL_GET payload",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Concurrency for optional detail hydration",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=100,
        help="Write CSV checkpoints while hydrating details",
    )
    parser.add_argument(
        "--skip-sitemap-check",
        action="store_true",
        help="Skip product sitemap count/check",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"CSV output path (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--discovery-out",
        type=Path,
        default=DEFAULT_DISCOVERY_OUT,
        help=f"Discovery JSON output path (default: {DEFAULT_DISCOVERY_OUT})",
    )
    args = parser.parse_args()
    if args.probe and args.limit is None:
        args.limit = 20
    if args.hits_per_page < 1 or args.hits_per_page > 1000:
        parser.error("--hits-per-page must be between 1 and 1000")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.checkpoint_every < 1:
        parser.error("--checkpoint-every must be at least 1")
    return args


def main() -> None:
    args = parse_args()
    start = time.time()
    asyncio.run(run(args))
    print(f"Done in {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()
