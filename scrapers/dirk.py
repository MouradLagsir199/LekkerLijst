"""Dirk supermarket catalog scraper.

Reverse-engineered Dirk web GraphQL gateway (no Cloudflare, no browser needed):
  - Endpoint: POST https://web-gateway.dirk.nl/graphql   (every query, with api_key header)
  - Departments: ``ListDepartments`` -> 17 departments, 146 webGroups (no variables)
  - List       : ``ListWebGroupProducts(webGroupId)`` -> productAssortment(storeId) rows
                 (with live normal/offer price). Rows not in the store assortment come
                 back as ``null`` entries in the array, so we drop them.
  - Detail     : ``ProductDetail(productId, storeId)`` -> barcode/EAN, images, brand,
                 declarations (nutrition, allergens, ingredients), logos (nutri-score).

Completeness: we flatten ALL webGroups across ALL departments, list every product in
the store-66 assortment for each webGroup, dedupe productIds (keep first-seen), then
hydrate each unique product with the detail query and merge list-row + detail under
``raw``. No normalization happens here (that is the later silver step).

This mirrors the reference scraper ``scrapers.ah``: every store module emits the same
envelope ``{"store", "scraped_at", "external_id", "raw": {...}}`` to a JSONL artifact,
which ``scrapers.bronze_ingest`` loads into ``catalog.bronze_products``.

Usage:
    python -m scrapers.dirk                          # full catalog -> Output/dirk_bronze.jsonl
    python -m scrapers.dirk --limit 30 --max-groups 3  # smoke test
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any
from urllib.parse import quote

import httpx

from .common import JsonlWriter, backoff_sleep, default_output_path, now_iso, should_retry

GRAPHQL_URL = "https://web-gateway.dirk.nl/graphql"
FILESERVER_BASE = "https://web-fileserver.dirk.nl/"

STORE = "dirk"
STORE_ID = 66

API_KEY = "6d3a42a3-6d93-4f98-838d-bcc0ab2307fd"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

LIST_CONCURRENCY = 8
DETAIL_CONCURRENCY = 8

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "api_key": API_KEY,
}

LIST_DEPARTMENTS_QUERY = """
query ListDepartments {
  listDepartments {
    departments {
      id
      description
      webGroups { webGroupId description }
    }
  }
}
"""

LIST_WEBGROUP_PRODUCTS_QUERY = """
query ListWebGroupProducts($webGroupId: Int!, $storeId: Int!) {
  listWebGroupProducts(webGroupId: $webGroupId) {
    productAssortment(storeId: $storeId) {
      productId productNumber normalPrice offerPrice
      startDate endDate priceDate
      productInformation {
        productId headerText subText packaging image department webgroup brand
        logos { description position link image }
      }
    }
  }
}
"""

PRODUCT_DETAIL_QUERY = """
query ProductDetail($productId: Int!, $storeId: Int!) {
  product(productId: $productId) {
    productId articleNumber barcode brand department headerText packaging
    description additionalDescription mainDescription subDescription webgroup
    isWeightProduct maxAmount
    images { image rankNumber mainImage }
    logos { description position link image }
    declarations {
      storageInstructions cookingInstructions instructionsForUse ingredients
      contactInformation { contactName contactAdress }
      nutritionalInformation {
        standardPackagingUnit soldOrPrepared
        nutritionalValues { text value nutritionalSubValues { text value } }
      }
      allergiesInformation { text }
    }
    productAssortment(storeId: $storeId) {
      productId productNumber normalPrice offerPrice startDate endDate priceDate
      productInformation {
        productId headerText subText packaging image department webgroup brand
      }
    }
  }
}
"""


def build_image_url(relative: str | None, *, width: int | None = None) -> str | None:
    """Join a relative Dirk image path onto the fileserver base.

    Paths come back like ``artikelen/198647_1_..png`` (or with backslashes); we
    normalize separators, url-quote each segment, and optionally request a width.
    """
    if not relative:
        return None
    rel = str(relative).replace("\\", "/").lstrip("/")
    quoted = quote(rel, safe="/")
    url = FILESERVER_BASE + quoted
    if width is not None:
        url = f"{url}?width={width}"
    return url


async def graphql(
    client: httpx.AsyncClient,
    query: str,
    variables: dict | None = None,
    *,
    retries: int = 5,
) -> dict | None:
    """POST a GraphQL query, returning the ``data`` payload.

    Retries on retryable HTTP statuses (429/5xx) and on GraphQL ``errors`` in the
    body with exponential backoff. After exhausting retries, surfaces the error.
    """
    payload: dict[str, Any] = {"query": query}
    if variables is not None:
        payload["variables"] = variables

    for attempt in range(1, retries + 1):
        try:
            resp = await client.post(GRAPHQL_URL, json=payload, headers=HEADERS, timeout=60.0)
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == retries:
                raise
            await backoff_sleep(attempt, base=2.0)
            continue

        if should_retry(resp.status_code) and attempt < retries:
            await backoff_sleep(attempt, base=2.0)
            continue
        resp.raise_for_status()

        body = resp.json()
        if body.get("errors"):
            if attempt < retries:
                await backoff_sleep(attempt, base=2.0)
                continue
            raise RuntimeError(f"Dirk GraphQL errors: {body['errors']}")
        return body.get("data")
    return None


async def list_webgroups(client: httpx.AsyncClient) -> list[dict]:
    """Flatten every webGroup across every department."""
    data = await graphql(client, LIST_DEPARTMENTS_QUERY)
    departments = ((data or {}).get("listDepartments") or {}).get("departments") or []
    groups: list[dict] = []
    for dep in departments:
        for wg in dep.get("webGroups") or []:
            groups.append(
                {
                    "webGroupId": wg.get("webGroupId"),
                    "description": wg.get("description"),
                    "departmentId": dep.get("id"),
                    "departmentDescription": dep.get("description"),
                }
            )
    return groups


async def list_webgroup_products(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, web_group_id: int
) -> list[dict]:
    """Return the non-null assortment rows for one webGroup at store 66."""
    async with sem:
        data = await graphql(
            client,
            LIST_WEBGROUP_PRODUCTS_QUERY,
            {"webGroupId": web_group_id, "storeId": STORE_ID},
        )
    assortment = ((data or {}).get("listWebGroupProducts") or {}).get("productAssortment") or []
    # Products not in this store's assortment come back as null entries.
    return [row for row in assortment if row and row.get("productId") is not None]


async def fetch_detail(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, product_id: int
) -> dict | None:
    async with sem:
        data = await graphql(
            client,
            PRODUCT_DETAIL_QUERY,
            {"productId": product_id, "storeId": STORE_ID},
        )
    return (data or {}).get("product")


def with_image_urls(product: dict | None) -> dict | None:
    """Add convenience absolute ``image_url`` fields without dropping the raw paths."""
    if not product:
        return product
    images = product.get("images") or []
    built: list[dict] = []
    for img in images:
        if not img:
            continue
        entry = dict(img)
        entry["image_url"] = build_image_url(
            img.get("image"), width=500 if img.get("mainImage") else None
        )
        built.append(entry)
    if built:
        product = dict(product)
        product["images"] = built
    return product


def envelope(list_row: dict, detail: dict | None) -> dict:
    """Build the bronze envelope: list row + detail merged under ``raw``."""
    return {
        "store": STORE,
        "scraped_at": now_iso(),
        "external_id": str(list_row.get("productId")),
        "raw": {"list": list_row, "detail": with_image_urls(detail)},
    }


async def run(*, limit: int | None, max_groups: int | None) -> None:
    out_path = default_output_path(STORE)
    async with httpx.AsyncClient(http2=False) as client:
        groups = await list_webgroups(client)
        print(f"Found {len(groups)} webGroups")
        if max_groups:
            groups = groups[:max_groups]
            print(f"Capped to {len(groups)} webGroups (smoke test)")

        list_sem = asyncio.Semaphore(LIST_CONCURRENCY)
        rows_by_id: dict[str, dict] = {}
        for index, wg in enumerate(groups, start=1):
            rows = await list_webgroup_products(client, list_sem, wg["webGroupId"])
            for row in rows:
                pid = str(row.get("productId"))
                if pid and pid != "None":
                    # carry which webgroup first surfaced the product
                    row = {**row, "_webGroupId": wg["webGroupId"]}
                    rows_by_id.setdefault(pid, row)
            print(
                f"  [{index}/{len(groups)}] webGroup {wg['webGroupId']} "
                f"({wg.get('description')}): +{len(rows)} (total unique {len(rows_by_id)})"
            )
            if limit and len(rows_by_id) >= limit:
                break

        ids = list(rows_by_id.keys())
        if limit:
            ids = ids[:limit]
        print(f"Collected {len(ids)} unique products")

        details: dict[str, dict | None] = {}
        detail_sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def hydrate(pid: str) -> None:
            details[pid] = await fetch_detail(client, detail_sem, int(pid))

        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            await asyncio.gather(*(hydrate(pid) for pid in chunk))
            print(f"  detail {min(start + 500, len(ids))}/{len(ids)}")

        with JsonlWriter(out_path) as writer:
            for pid in ids:
                writer.write(envelope(rows_by_id[pid], details.get(pid)))
        print(f"Wrote {len(ids)} products to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape the Dirk catalog into a bronze JSONL artifact."
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap total products (smoke test)")
    parser.add_argument(
        "--max-groups", type=int, default=None, help="Cap number of webGroups walked (smoke test)"
    )
    args = parser.parse_args()
    asyncio.run(run(limit=args.limit, max_groups=args.max_groups))


if __name__ == "__main__":
    main()
