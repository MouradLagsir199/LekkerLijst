"""
Jumbo GraphQL catalog scraper
=============================

Uses Jumbo's first-party GraphQL endpoint instead of scraping rendered HTML.

Setup:
    pip install httpx

Usage:
    python Jumbo/jumbo_scraper.py --probe
    python Jumbo/jumbo_scraper.py --all
    python Jumbo/jumbo_scraper.py --all --limit 250
    python Jumbo/jumbo_scraper.py --all --out Output/jumbo_products.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from curl_cffi import requests as curl_requests


BASE_URL = "https://www.jumbo.com"
GRAPHQL_URL = f"{BASE_URL}/api/graphql"
PRODUCTS_URL = f"{BASE_URL}/producten/"
CLIENT_NAME = "JUMBO_WEB"
CLIENT_VERSION_FALLBACK = "master-v32.14.0-web"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

PAGE_SIZE = 24
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "Output" / "jumbo_products.csv"

CSV_COLUMNS = [
    "catalog_rank",
    "product_id",
    "sku",
    "gtin",
    "product_name",
    "brand",
    "subtitle",
    "price",
    "promo_price",
    "effective_price",
    "base_unit_price",
    "base_unit_price_value",
    "base_unit_price_unit",
    "base_unit_quantity",
    "allergen_contains",
    "allergen_may_contain",
    "nutriscore",
    "nutrients_json",
    "nutrients_table_json",
    "ingredients",
    "root_category",
    "categories",
    "category_ids",
    "url",
    "image_url",
    "is_available",
    "availability_label",
    "availability_reason",
    "in_assortment",
    "retail_set",
    "regulated_product_name",
    "description",
    "storage",
    "preparation_and_usage",
    "badges",
    "characteristics_json",
    "promotions_json",
    "scraped_at",
]


SEARCH_PRODUCTS_QUERY = """
query SearchMobileProducts($input: ProductSearchInput!) {
  searchProducts(input: $input) {
    id
    start
    count
    pageHeader {
      headerText
      count
    }
    products {
      id
      brand
      rootCategory
      subtitle: packSizeDisplay
      title
      image
      inAssortment
      link
      retailSet
      price {
        price
        promoPrice
        pricePerUnit {
          price
          unit
        }
      }
      availability {
        availability
        isAvailable
        label
        reason
        availabilityNote
      }
      primaryProductBadges {
        alt
        image
      }
      secondaryProductBadges {
        alt
        image
      }
      characteristics {
        freshness {
          name
          value
          url
        }
        logo {
          name
          value
          url
        }
        tags {
          name
          value
          url
        }
      }
      promotions {
        id
        group
        isKiesAndMix
        image
        url
        tags {
          text
          inverse
        }
        durationTexts {
          shortTitle
        }
        volumeDiscounts {
          discount
          volume
        }
      }
    }
  }
}
"""


PRODUCTS_BATCH_QUERY = """
query ProductsBatch($skus: [String!]!) {
  products(skus: $skus) {
    id
    sku
    brand
    brandURL
    ean
    rootCategory
    categories {
      name
      path
      id
    }
    subtitle
    title
    image
    canonicalUrl
    description
    storage
    recycling
    ingredients
    retailSet
    isMedicine
    preparationAndUsage
    isExcludedForCustomer
    productAllergens {
      mayContain
      contains
    }
    nutritionsTable {
      columns
      rows
    }
    nutriScore {
      value
      url
    }
    availability {
      availabilityNote
      label
      isAvailable
      availability
      stockLimit
      reason
      delistDate {
        iso
      }
    }
    link
    price {
      price
      promoPrice
      pricePerUnit {
        price
        unit
        quantity
      }
    }
    quantityDetails {
      maxAmount
      minAmount
      stepAmount
      defaultAmount
    }
    primaryProductBadges {
      alt
      image
    }
    secondaryProductBadges {
      alt
      image
    }
    promotions {
      id
      isKiesAndMix
      tags {
        text
        inverse
      }
      group
      image
      url
      durationTexts {
        title
        description
        shortTitle
      }
      primaryBadges {
        alt
        image
      }
      start {
        date
        dayShort
        monthShort
      }
      end {
        date
        dayShort
        monthShort
      }
      volumeDiscounts {
        discount
        volume
      }
      maxPromotionQuantity
    }
    manufacturer {
      description
      address
      phone
      website
    }
    alcoholByVolume
    nutritionHealthClaims
    additives
    mandatoryInformation
    regulatedProductName
    safety
    safetyWarning
    origin
    fishCatchArea
    fishOriginFreeText
    fishPlaceOfProvenance
    placeOfRearing
    placeOfSlaughter
    placeOfBirth
    customerAllergies {
      long
      short
    }
    characteristics {
      freshness {
        name
        value
        url
      }
      logo {
        name
        value
        url
      }
      tags {
        name
        value
        url
      }
      thirdPartyLogos {
        identifier
        imageUrl
        targetUrl
      }
    }
  }
}
"""


def graphql_headers(client_version: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": PRODUCTS_URL,
        "x-source": CLIENT_NAME,
        "apollographql-client-name": CLIENT_NAME,
        "apollographql-client-version": client_version,
    }


async def discover_client_version(client: Any) -> str:
    """Read the current Nuxt app version so GraphQL client headers stay fresh."""
    try:
        resp = await client.get(PRODUCTS_URL, timeout=30)
        resp.raise_for_status()
    except curl_requests.errors.RequestsError as exc:
        print(f"Could not refresh Jumbo client version, using fallback: {exc}")
        return CLIENT_VERSION_FALLBACK

    match = re.search(r'applicationVersion:"([^"]+)"', resp.text)
    if not match:
        match = re.search(r'"applicationVersion"\s*:\s*"([^"]+)"', resp.text)
    return match.group(1) if match else CLIENT_VERSION_FALLBACK


async def graphql_request(
    client: Any,
    *,
    operation_name: str,
    query: str,
    variables: dict[str, Any],
    retries: int = 4,
) -> dict[str, Any]:
    payload = {
        "operationName": operation_name,
        "variables": variables,
        "query": query,
    }
    backoff = 1.5
    for attempt in range(1, retries + 1):
        try:
            resp = await client.post(GRAPHQL_URL, json=payload, timeout=60)
            if resp.status_code in {429, 500, 502, 503, 504}:
                if attempt == retries:
                    resp.raise_for_status()
                print(
                    f"  HTTP {resp.status_code} on {operation_name}; "
                    f"retry {attempt}/{retries} in {backoff:.1f}s"
                )
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 401 and "No client headers set" in resp.text:
                raise RuntimeError(
                    "Jumbo rejected the GraphQL client headers. "
                    "Try rerunning; the scraper refreshes the current Nuxt client version at startup."
                )

            resp.raise_for_status()
            data = resp.json()
            errors = data.get("errors") or []
            if errors and not data.get("data"):
                messages = "; ".join(str(error.get("message", error)) for error in errors)
                raise RuntimeError(f"GraphQL {operation_name} failed: {messages}")
            if errors:
                messages = "; ".join(str(error.get("message", error)) for error in errors)
                print(f"  GraphQL warnings on {operation_name}: {messages}")
            return data
        except curl_requests.errors.RequestsError as exc:
            if attempt == retries:
                raise
            print(
                f"  network error on {operation_name}: {exc!r}; "
                f"retry {attempt}/{retries} in {backoff:.1f}s"
            )
            await asyncio.sleep(backoff)
            backoff *= 2
    raise RuntimeError(f"GraphQL {operation_name} failed after {retries} retries")


def build_search_input(
    *,
    offset: int,
    query: str | None = None,
    friendly_url: str = "",
) -> dict[str, Any]:
    if query:
        encoded = quote(query)
        current_url = f"/producten/?searchType=keyword&searchTerms={encoded}"
        if offset:
            current_url = f"{current_url}&offSet={offset}"
        return {
            "searchType": "keyword",
            "searchTerms": query,
            "friendlyUrl": "",
            "sort": None,
            "offSet": offset,
            "currentUrl": current_url,
            "previousUrl": "",
            "bloomreachCookieId": None,
        }

    current_url = "/producten/" if not offset else f"/producten/?offSet={offset}"
    return {
        "searchType": "category",
        "searchTerms": "producten",
        "friendlyUrl": friendly_url,
        "sort": None,
        "offSet": offset,
        "currentUrl": current_url,
        "previousUrl": "",
        "bloomreachCookieId": None,
    }


async def fetch_search_page(
    client: Any,
    *,
    offset: int,
    query: str | None = None,
    friendly_url: str = "",
) -> dict[str, Any]:
    variables = {"input": build_search_input(offset=offset, query=query, friendly_url=friendly_url)}
    data = await graphql_request(
        client,
        operation_name="SearchMobileProducts",
        query=SEARCH_PRODUCTS_QUERY,
        variables=variables,
    )
    return data["data"]["searchProducts"]


async def collect_listing(
    client: Any,
    *,
    concurrency: int,
    limit: int | None = None,
    query: str | None = None,
    friendly_url: str = "",
) -> list[dict[str, Any]]:
    first = await fetch_search_page(client, offset=0, query=query, friendly_url=friendly_url)
    total_count = int(first.get("count") or 0)
    products = list(first.get("products") or [])
    if limit is not None:
        total_count = min(total_count, limit)

    print(f"Listing has {first.get('count', 0)} products; collecting {total_count}.")
    if total_count <= len(products):
        return products[:total_count]

    offsets = list(range(PAGE_SIZE, total_count, PAGE_SIZE))
    sem = asyncio.Semaphore(concurrency)
    collected_pages = 1

    async def fetch_one(offset: int) -> tuple[int, list[dict[str, Any]]]:
        async with sem:
            page = await fetch_search_page(
                client,
                offset=offset,
                query=query,
                friendly_url=friendly_url,
            )
            return offset, list(page.get("products") or [])

    tasks = [asyncio.create_task(fetch_one(offset)) for offset in offsets]
    pages_by_offset: dict[int, list[dict[str, Any]]] = {0: products}
    for task in asyncio.as_completed(tasks):
        offset, page_products = await task
        pages_by_offset[offset] = page_products
        collected_pages += 1
        if collected_pages % 25 == 0 or collected_pages == len(offsets) + 1:
            print(f"  listing pages: {collected_pages}/{len(offsets) + 1}")

    ordered: list[dict[str, Any]] = []
    for offset in sorted(pages_by_offset):
        ordered.extend(pages_by_offset[offset])
    return ordered[:total_count]


async def fetch_products_batch(
    client: Any,
    skus: list[str],
) -> list[dict[str, Any]]:
    data = await graphql_request(
        client,
        operation_name="ProductsBatch",
        query=PRODUCTS_BATCH_QUERY,
        variables={"skus": skus},
    )
    products = data.get("data", {}).get("products") or []
    return [product for product in products if product]


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


async def hydrate_details(
    client: httpx.AsyncClient,
    *,
    listing: list[dict[str, Any]],
    concurrency: int,
    batch_size: int,
    checkpoint_every: int,
    output_csv: Path,
) -> list[dict[str, Any]]:
    listing_by_sku = {str(item.get("id")): item for item in listing if item.get("id")}
    skus = list(listing_by_sku)
    batches = chunks(skus, batch_size)
    sem = asyncio.Semaphore(concurrency)
    hydrated: list[dict[str, Any]] = []
    seen: set[str] = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    async def fetch_one(batch_number: int, batch_skus: list[str]) -> tuple[int, list[dict[str, Any]]]:
        async with sem:
            return batch_number, await fetch_products_batch(client, batch_skus)

    print(
        f"Hydrating {len(skus)} SKUs in {len(batches)} batches "
        f"(batch size {batch_size}, concurrency {concurrency})."
    )

    tasks = [
        asyncio.create_task(fetch_one(batch_number, batch))
        for batch_number, batch in enumerate(batches, start=1)
    ]
    completed = 0
    for task in asyncio.as_completed(tasks):
        batch_number, detail_products = await task
        for product in detail_products:
            sku = str(product.get("sku") or product.get("id") or "")
            if sku:
                seen.add(sku)
            hydrated.append(
                normalize_product(
                    product,
                    listing_by_sku.get(sku, {}),
                    scraped_at=scraped_at,
                )
            )

        completed += 1
        if completed % checkpoint_every == 0 or completed == len(batches):
            missing_rows = [
                normalize_product({}, listing_by_sku[sku], scraped_at=scraped_at)
                for sku in skus
                if sku not in seen
            ]
            write_csv(hydrated + missing_rows, output_csv)
            print(
                f"  detail batches: {completed}/{len(batches)} "
                f"(last batch {batch_number}); saved {len(hydrated)} hydrated rows"
            )

    for sku in skus:
        if sku not in seen:
            hydrated.append(normalize_product({}, listing_by_sku[sku], scraped_at=scraped_at))

    return hydrated


def cents_to_euro(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return f"{int(value) / 100:.2f}"
    except (TypeError, ValueError):
        return None


def normalize_gtin(value: Any) -> str | None:
    gtin = re.sub(r"\D", "", str(value or ""))
    return gtin or None


def as_json(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def join_list(value: Any, sep: str = "; ") -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [str(item) for item in value if item not in (None, "")]
        return sep.join(parts) if parts else None
    return str(value)


def absolute_url(value: str | None) -> str | None:
    if not value:
        return None
    return urljoin(BASE_URL, value)


def base_unit_price_description(price_per_unit: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    value = cents_to_euro(price_per_unit.get("price"))
    unit = price_per_unit.get("unit")
    quantity = price_per_unit.get("quantity") or "1"
    if not value or not unit:
        return None, value, unit, str(quantity) if quantity else None
    return f"{value} per {quantity} {unit}", value, unit, str(quantity)


def nutrition_records(table: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not table:
        return []
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    records: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list) or not row:
            continue
        if len(row) == 1:
            records.append({"note": row[0]})
            continue
        name = row[0] or ""
        values: dict[str, Any] = {}
        for index, cell in enumerate(row[1:], start=1):
            column_name = columns[index] if index < len(columns) else f"value_{index}"
            values[column_name] = cell
        records.append({"name": name, "values": values})
    return records


def badge_text(product: dict[str, Any]) -> str | None:
    badges = []
    for key in ("primaryProductBadges", "secondaryProductBadges"):
        for badge in product.get(key) or []:
            label = badge.get("alt") or badge.get("image")
            if label:
                badges.append(str(label))
    return "; ".join(dict.fromkeys(badges)) if badges else None


def categories_text(product: dict[str, Any], listing: dict[str, Any]) -> tuple[str | None, str | None]:
    categories = product.get("categories") or []
    names = [item.get("name") for item in categories if item.get("name")]
    ids = [item.get("id") for item in categories if item.get("id")]
    if not names and (product.get("rootCategory") or listing.get("rootCategory")):
        names = [product.get("rootCategory") or listing.get("rootCategory")]
    return (
        " > ".join(names) if names else None,
        ";".join(ids) if ids else None,
    )


def normalize_product(
    product: dict[str, Any],
    listing: dict[str, Any],
    *,
    scraped_at: str,
) -> dict[str, Any]:
    data = {**listing, **product}
    sku = data.get("sku") or data.get("id")
    price = data.get("price") or {}
    price_regular = cents_to_euro(price.get("price"))
    price_promo = cents_to_euro(price.get("promoPrice"))
    effective_price = price_promo or price_regular
    base_price, base_value, base_unit, base_quantity = base_unit_price_description(
        price.get("pricePerUnit") or {}
    )
    allergens = product.get("productAllergens") or {}
    nutri_score = product.get("nutriScore") or {}
    nutrition_table = product.get("nutritionsTable")
    categories, category_ids = categories_text(product, listing)
    availability = data.get("availability") or {}

    return {
        "catalog_rank": listing.get("_catalog_rank"),
        "product_id": data.get("id"),
        "sku": sku,
        "gtin": normalize_gtin(product.get("ean")),
        "product_name": data.get("title"),
        "brand": data.get("brand"),
        "subtitle": data.get("subtitle"),
        "price": price_regular,
        "promo_price": price_promo,
        "effective_price": effective_price,
        "base_unit_price": base_price,
        "base_unit_price_value": base_value,
        "base_unit_price_unit": base_unit,
        "base_unit_quantity": base_quantity,
        "allergen_contains": join_list(allergens.get("contains")),
        "allergen_may_contain": join_list(allergens.get("mayContain")),
        "nutriscore": nutri_score.get("value") if isinstance(nutri_score, dict) else None,
        "nutrients_json": as_json(nutrition_records(nutrition_table)),
        "nutrients_table_json": as_json(nutrition_table),
        "ingredients": join_list(product.get("ingredients")),
        "root_category": product.get("rootCategory") or listing.get("rootCategory"),
        "categories": categories,
        "category_ids": category_ids,
        "url": absolute_url(product.get("canonicalUrl") or data.get("link")),
        "image_url": data.get("image"),
        "is_available": availability.get("isAvailable"),
        "availability_label": availability.get("label") or availability.get("availabilityNote"),
        "availability_reason": availability.get("reason"),
        "in_assortment": data.get("inAssortment"),
        "retail_set": data.get("retailSet"),
        "regulated_product_name": product.get("regulatedProductName"),
        "description": product.get("description"),
        "storage": product.get("storage"),
        "preparation_and_usage": product.get("preparationAndUsage"),
        "badges": badge_text(data),
        "characteristics_json": as_json(data.get("characteristics")),
        "promotions_json": as_json(data.get("promotions")),
        "scraped_at": scraped_at,
    }


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("sku") or row.get("product_id") or len(deduped))
        deduped[key] = row
    ordered = sorted(
        deduped.values(),
        key=lambda row: (
            int(row["catalog_rank"]) if str(row.get("catalog_rank") or "").isdigit() else 10**12,
            str(row.get("sku") or ""),
        ),
    )
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ordered)


async def scrape(
    *,
    output_csv: Path,
    concurrency: int,
    detail_batch_size: int,
    checkpoint_every: int,
    limit: int | None,
    query: str | None,
    no_details: bool,
    client_version: str | None,
) -> list[dict[str, Any]]:
    bootstrap_headers = graphql_headers(client_version or CLIENT_VERSION_FALLBACK)
    # Jumbo's GraphQL edge rejects the stock HTTP fingerprint used by hosted CI.
    # curl_cffi keeps the async flow while impersonating the Chrome TLS/browser stack.
    async with curl_requests.AsyncSession(headers=bootstrap_headers, impersonate="chrome124") as client:
        if client_version is None:
            version = await discover_client_version(client)
            client.headers.update(graphql_headers(version))
            print(f"Using Jumbo client version: {version}")
        else:
            print(f"Using supplied Jumbo client version: {client_version}")

        listing = await collect_listing(
            client,
            concurrency=concurrency,
            limit=limit,
            query=query,
        )
        for index, product in enumerate(listing, start=1):
            product["_catalog_rank"] = index

        if no_details:
            scraped_at = datetime.now(timezone.utc).isoformat()
            rows = [normalize_product({}, item, scraped_at=scraped_at) for item in listing]
        else:
            rows = await hydrate_details(
                client,
                listing=listing,
                concurrency=concurrency,
                batch_size=detail_batch_size,
                checkpoint_every=checkpoint_every,
                output_csv=output_csv,
            )

        write_csv(rows, output_csv)
        return rows


async def probe(args: argparse.Namespace) -> None:
    rows = await scrape(
        output_csv=Path(args.out),
        concurrency=args.concurrency,
        detail_batch_size=args.detail_batch_size,
        checkpoint_every=args.checkpoint_every,
        limit=args.limit or 5,
        query=args.query,
        no_details=False,
        client_version=args.client_version,
    )
    print("\nProbe sample:")
    for row in rows[:5]:
        print(
            f"  {row.get('sku')} | {row.get('gtin')} | "
            f"{row.get('product_name')} | {row.get('effective_price')}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Jumbo GraphQL catalog scraper")
    parser.add_argument("--all", action="store_true", help="Scrape the full Jumbo catalog")
    parser.add_argument("--probe", action="store_true", help="Fetch a small sample and write it to CSV")
    parser.add_argument("--query", default=None, help="Keyword search instead of the root catalog")
    parser.add_argument("--limit", type=int, default=None, help="Limit SKUs for testing")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output CSV path")
    parser.add_argument("--concurrency", type=int, default=6, help="Simultaneous GraphQL requests")
    parser.add_argument("--detail-batch-size", type=int, default=50, help="SKUs per details request")
    parser.add_argument("--checkpoint-every", type=int, default=10, help="Detail batches between CSV saves")
    parser.add_argument("--no-details", action="store_true", help="Only save product-list fields")
    parser.add_argument(
        "--client-version",
        default=None,
        help="Override apollographql-client-version if Jumbo changes header checks",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.all and not args.probe and not args.query:
        raise SystemExit("Choose --all, --probe, or --query. Try --probe for a smoke test.")

    t0 = time.perf_counter()
    if args.probe:
        asyncio.run(probe(args))
    else:
        rows = asyncio.run(
            scrape(
                output_csv=Path(args.out),
                concurrency=args.concurrency,
                detail_batch_size=args.detail_batch_size,
                checkpoint_every=args.checkpoint_every,
                limit=args.limit,
                query=args.query,
                no_details=args.no_details,
                client_version=args.client_version,
            )
        )
        print(f"\nSaved {len(rows)} rows -> {Path(args.out).resolve()}")

    print(f"Elapsed: {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
