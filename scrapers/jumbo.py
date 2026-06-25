"""Jumbo catalog scraper.

Reverse-engineered Jumbo web GraphQL edge. Unlike Albert Heijn, Jumbo's edge
sits behind anti-bot that rejects ordinary HTTP clients on TLS fingerprint, so
we drive it with ``curl_cffi`` impersonating Chrome.

  - Endpoint : POST https://www.jumbo.com/api/graphql  (single GraphQL edge)
  - Version  : GET  https://www.jumbo.com/producten/   then regex the embedded
               ``applicationVersion`` -> apollographql-client-version header.
  - Listing  : operationName ``SearchMobileProducts`` / field ``searchProducts``
               category search over the root ``producten`` collection, paginated
               by ``offSet`` (capital S) in PRODUCT units, server page size 24.
  - Detail   : operationName ``ProductsBatch`` / field ``products(skus: [...])``
               chunked 50 SKUs at a time; carries EAN, nutrition, allergens,
               nutriScore, ingredients, categories.

We store the merged listing card + detail object under ``raw`` (no
normalization). Same envelope as every other store scraper:
  {"store", "scraped_at", "external_id", "raw": {...}}

Usage:
    python -m scrapers.jumbo                # full catalog -> Output/jumbo_bronze.jsonl
    python -m scrapers.jumbo --limit 30     # smoke test (first ~30 products)
    python -m scrapers.jumbo --no-detail    # fast: skip the ProductsBatch hydrate
"""

from __future__ import annotations

import argparse
import asyncio
import re
from typing import Any

from curl_cffi import requests as curl_requests

from .common import JsonlWriter, backoff_sleep, default_output_path, now_iso, should_retry

STORE = "jumbo"

PRODUCTEN_URL = "https://www.jumbo.com/producten/"
GRAPHQL_URL = "https://www.jumbo.com/api/graphql"

# Fallback client-version if the embedded one cannot be discovered.
FALLBACK_CLIENT_VERSION = "master-v32.14.0-web"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PAGE_SIZE = 24  # Jumbo's listing page size is server-fixed.
DETAIL_CHUNK = 50  # ProductsBatch SKUs per request.
CONCURRENCY = 4
TIMEOUT = 60
IMPERSONATE = "chrome124"

# ---- Working GraphQL query strings (verified against the live edge). ---------

SEARCH_QUERY = """query SearchMobileProducts($input: ProductSearchInput!) {
  searchProducts(input: $input) {
    count
    start
    products {
      id
      title
      brand
      image
      link
      price { price promoPrice pricePerUnit { price unit } }
      availability { isAvailable }
      packSizeDisplay
    }
  }
}"""

DETAIL_QUERY = """query ProductsBatch($skus: [String!]!) {
  products(skus: $skus) {
    sku
    ean
    title
    brand
    description
    ingredients
    nutriScore { value }
    productAllergens { contains mayContain }
    nutritionsTable { columns rows }
    categories { name path id }
    image
    price { price promoPrice pricePerUnit { price unit quantity } }
    availability { isAvailable label }
  }
}"""


def graphql_headers(client_version: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": "https://www.jumbo.com",
        "Referer": PRODUCTEN_URL,
        "x-source": "JUMBO_WEB",
        "apollographql-client-name": "JUMBO_WEB",
        "apollographql-client-version": client_version,
    }


class NoClientHeaders(Exception):
    """Raised on the 401 'No client headers set' -> client-version must be refreshed."""


async def discover_client_version(client: curl_requests.AsyncSession) -> str:
    """GET the products page and pull the embedded applicationVersion."""
    resp = await client.get(
        PRODUCTEN_URL,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"},
        timeout=TIMEOUT,
    )
    html = resp.text or ""
    match = re.search(r'applicationVersion:"([^"]+)"', html)
    if not match:
        match = re.search(r'"applicationVersion"\s*:\s*"([^"]+)"', html)
    if match:
        version = match.group(1)
        print(f"Discovered Jumbo client-version {version}")
        return version
    print(f"Could not discover client-version; using fallback {FALLBACK_CLIENT_VERSION}")
    return FALLBACK_CLIENT_VERSION


async def graphql(
    client: curl_requests.AsyncSession,
    sem: asyncio.Semaphore,
    client_version: str,
    operation: str,
    variables: dict,
    query: str,
    *,
    retries: int = 5,
) -> Any | None:
    """POST a GraphQL op; retry transient errors, signal version-staleness via NoClientHeaders."""
    payload = {"operationName": operation, "variables": variables, "query": query}
    for attempt in range(1, retries + 1):
        try:
            async with sem:
                resp = await client.post(
                    GRAPHQL_URL,
                    json=payload,
                    headers=graphql_headers(client_version),
                    timeout=TIMEOUT,
                )
        except Exception:  # curl_cffi network/timeout errors
            if attempt == retries:
                raise
            await backoff_sleep(attempt, base=2.0)
            continue

        status = resp.status_code
        text = resp.text or ""

        # Stale/missing client headers: never retry, force a version rediscovery.
        if status == 401 and "No client headers set" in text:
            raise NoClientHeaders(text[:200])

        if should_retry(status) and attempt < retries:
            await backoff_sleep(attempt, base=2.0)
            continue
        if status >= 400:
            raise RuntimeError(f"Jumbo GraphQL {operation} HTTP {status}: {text[:300]}")

        body = resp.json()
        errors = body.get("errors")
        if errors:
            # Retry only on 5xx/429-flavoured GraphQL errors; surface the rest.
            blob = str(errors).lower()
            transient = any(code in blob for code in ("429", "500", "502", "503", "504", "timeout", "unavailable"))
            if transient and attempt < retries:
                await backoff_sleep(attempt, base=2.0)
                continue
            raise RuntimeError(f"Jumbo GraphQL {operation} errors: {errors}")
        return body.get("data")
    return None


async def run_search(
    client: curl_requests.AsyncSession,
    sem: asyncio.Semaphore,
    version_holder: list[str],
    *,
    limit: int | None,
) -> list[dict]:
    """Paginate the root category listing, returning all product cards."""

    async def fetch_offset(off: int) -> dict | None:
        variables = {
            "input": {
                "searchType": "category",
                "searchTerms": "producten",
                "friendlyUrl": "",
                "sort": None,
                "offSet": off,
                "currentUrl": f"/producten/?offSet={off}" if off else "/producten/",
                "previousUrl": "",
                "bloomreachCookieId": None,
            }
        }
        try:
            data = await graphql(client, sem, version_holder[0], "SearchMobileProducts", variables, SEARCH_QUERY)
        except NoClientHeaders:
            version_holder[0] = await discover_client_version(client)
            data = await graphql(client, sem, version_holder[0], "SearchMobileProducts", variables, SEARCH_QUERY)
        return (data or {}).get("searchProducts")

    first = await fetch_offset(0)
    if not first:
        return []
    total = int(first.get("count") or 0)
    cards: list[dict] = list(first.get("products") or [])
    target = min(total, limit) if limit else total
    print(f"Listing reports {total} products; collecting up to {target}")

    offsets = list(range(PAGE_SIZE, total, PAGE_SIZE))

    async def fetch_page(off: int) -> list[dict]:
        page = await fetch_offset(off)
        return list((page or {}).get("products") or [])

    for start in range(0, len(offsets), CONCURRENCY * 4):
        if limit and len(cards) >= limit:
            break
        batch = offsets[start : start + CONCURRENCY * 4]
        results = await asyncio.gather(*(fetch_page(o) for o in batch))
        for page_products in results:
            cards.extend(page_products)
        print(f"  listing {min(len(cards), total)}/{total}")

    return cards


async def hydrate_details(
    client: curl_requests.AsyncSession,
    sem: asyncio.Semaphore,
    version_holder: list[str],
    skus: list[str],
) -> dict[str, dict]:
    """Batch-fetch product detail by SKU; return a sku -> detail map."""
    details: dict[str, dict] = {}
    chunks = [skus[i : i + DETAIL_CHUNK] for i in range(0, len(skus), DETAIL_CHUNK)]

    async def fetch_chunk(chunk: list[str]) -> list[dict]:
        variables = {"skus": chunk}
        try:
            data = await graphql(client, sem, version_holder[0], "ProductsBatch", variables, DETAIL_QUERY)
        except NoClientHeaders:
            version_holder[0] = await discover_client_version(client)
            data = await graphql(client, sem, version_holder[0], "ProductsBatch", variables, DETAIL_QUERY)
        return list((data or {}).get("products") or [])

    for start in range(0, len(chunks), CONCURRENCY):
        batch = chunks[start : start + CONCURRENCY]
        results = await asyncio.gather(*(fetch_chunk(c) for c in batch))
        for products in results:
            for prod in products:
                sku = prod.get("sku")
                if sku:
                    details[str(sku)] = prod
        print(f"  detail {min(len(details), len(skus))}/{len(skus)}")

    return details


def envelope(card: dict, detail: dict | None) -> dict:
    return {
        "store": STORE,
        "scraped_at": now_iso(),
        "external_id": str(card.get("id")),
        "raw": {"listing": card, "detail": detail},
    }


async def run(*, limit: int | None, no_detail: bool) -> None:
    out_path = default_output_path(STORE)
    sem = asyncio.Semaphore(CONCURRENCY)

    async with curl_requests.AsyncSession(
        headers={"User-Agent": USER_AGENT}, impersonate=IMPERSONATE
    ) as client:
        version_holder = [await discover_client_version(client)]

        cards = await run_search(client, sem, version_holder, limit=limit)

        # De-dup by listing id, preserving order.
        cards_by_id: dict[str, dict] = {}
        for card in cards:
            cid = str(card.get("id"))
            if cid and cid != "None":
                cards_by_id.setdefault(cid, card)
        ids = list(cards_by_id.keys())
        if limit:
            ids = ids[:limit]
        print(f"Collected {len(ids)} unique products")

        details: dict[str, dict] = {}
        if not no_detail and ids:
            details = await hydrate_details(client, sem, version_holder, ids)

        with JsonlWriter(out_path) as writer:
            for cid in ids:
                writer.write(envelope(cards_by_id[cid], details.get(cid)))
        print(f"Wrote {len(ids)} products to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape the Jumbo catalog into a bronze JSONL artifact.")
    parser.add_argument("--limit", type=int, default=None, help="Cap total products (smoke test)")
    parser.add_argument("--no-detail", action="store_true", help="Skip the ProductsBatch detail hydrate")
    args = parser.parse_args()
    asyncio.run(run(limit=args.limit, no_detail=args.no_detail))


if __name__ == "__main__":
    main()
