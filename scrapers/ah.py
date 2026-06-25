"""Albert Heijn catalog scraper.

Reverse-engineered "Appie" mobile API (no Cloudflare, no browser needed):
  - Auth   : POST  /mobile-auth/v1/auth/token/anonymous   body {"clientId":"appie"}
  - Cats   : GET   /mobile-services/v1/product-shelves/categories
             GET   /mobile-services/v1/product-shelves/categories/{id}/sub-categories
  - Search : GET   /mobile-services/product/search/v2?taxonomyId=&adType=TAXONOMY&page=&size=
  - Detail : GET   /mobile-services/product/detail/v4/fir/{webshopId}

Completeness: AH 400s a single search past page 30 (~6000 products), so we walk
the taxonomy tree down to leaf sub-categories and paginate each leaf. The detail
endpoint carries the trade-item block (gtin/EAN, allergens, ingredients,
nutrition); the search card carries price + images + nutriscore. We store both.

This is the reference scraper: every store module emits the same envelope
  {"store", "scraped_at", "external_id", "raw": {...}}
to a JSONL artifact, which scrapers.bronze_ingest loads into catalog.bronze_products.

Usage:
    python -m scrapers.ah                         # full catalog -> Output/ah_bronze.jsonl
    python -m scrapers.ah --limit 50              # smoke test (first ~50 products)
    python -m scrapers.ah --no-detail             # fast: skip the per-product detail call
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import httpx

from .common import JsonlWriter, backoff_sleep, default_output_path, now_iso, should_retry

BASE = "https://api.ah.nl"
AUTH_URL = f"{BASE}/mobile-auth/v1/auth/token/anonymous"
CATEGORIES_URL = f"{BASE}/mobile-services/v1/product-shelves/categories"
SUBCATEGORIES_URL = f"{BASE}/mobile-services/v1/product-shelves/categories/{{cid}}/sub-categories"
SEARCH_URL = f"{BASE}/mobile-services/product/search/v2"
DETAIL_URL = f"{BASE}/mobile-services/product/detail/v4/fir/{{wid}}"

USER_AGENT = "Appie/8.22.3"
PAGE_SIZE = 200
MAX_PAGES = 30  # AH returns HTTP 400 beyond this for a single taxonomy query
SEARCH_CONCURRENCY = 6
DETAIL_CONCURRENCY = 8

STORE = "ah"


def base_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "X-Application": "AHWEBSHOP",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json: Any | None = None,
    headers: dict | None = None,
    retries: int = 4,
) -> Any | None:
    """Return parsed JSON, None for 400/404 (skip), raising after exhausting retries."""
    for attempt in range(1, retries + 1):
        try:
            resp = await client.request(method, url, params=params, json=json, headers=headers, timeout=60.0)
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == retries:
                raise
            await backoff_sleep(attempt, base=2.0)
            continue
        if resp.status_code in (400, 404):
            return None
        if should_retry(resp.status_code) and attempt < retries:
            await backoff_sleep(attempt, base=2.0)
            continue
        resp.raise_for_status()
        return resp.json()
    return None


async def get_token(client: httpx.AsyncClient) -> str:
    data = await request_json(client, "POST", AUTH_URL, json={"clientId": "appie"}, headers=base_headers())
    if not data or "access_token" not in data:
        raise RuntimeError("Failed to obtain AH anonymous token")
    return data["access_token"]


async def list_leaf_taxonomies(client: httpx.AsyncClient, token: str) -> list[dict]:
    """Walk top categories -> sub-categories; the sub-categories are the leaves we paginate."""
    headers = base_headers(token)
    top = await request_json(client, "GET", CATEGORIES_URL, headers=headers) or []
    leaves: list[dict] = []
    sem = asyncio.Semaphore(SEARCH_CONCURRENCY)

    async def fetch_subs(cat: dict) -> None:
        async with sem:
            data = await request_json(
                client, "GET", SUBCATEGORIES_URL.format(cid=cat["id"]), headers=headers
            )
        children = (data or {}).get("children") or []
        if children:
            for child in children:
                leaves.append({"id": child["id"], "name": f"{cat.get('name')} / {child.get('name')}"})
        else:
            # category has no sub-level: it is itself a leaf
            leaves.append({"id": cat["id"], "name": cat.get("name")})

    await asyncio.gather(*(fetch_subs(cat) for cat in top))
    # de-dup leaf taxonomy ids (a product taxonomy can appear under multiple parents)
    unique: dict[int, dict] = {leaf["id"]: leaf for leaf in leaves}
    return list(unique.values())


async def search_taxonomy(client: httpx.AsyncClient, sem: asyncio.Semaphore, token: str, taxonomy_id: int) -> list[dict]:
    """Paginate one taxonomy, returning all product cards (clamped to AH's 30-page ceiling)."""
    headers = base_headers(token)
    params0 = {"taxonomyId": taxonomy_id, "adType": "TAXONOMY", "sortOn": "RELEVANCE", "page": 0, "size": PAGE_SIZE}
    async with sem:
        first = await request_json(client, "GET", SEARCH_URL, params=params0, headers=headers)
    if not first:
        return []
    products = list(first.get("products") or [])
    total_pages = min((first.get("page") or {}).get("totalPages", 1), MAX_PAGES)
    total_elements = (first.get("page") or {}).get("totalElements", 0)
    if total_elements > MAX_PAGES * PAGE_SIZE:
        print(f"  ! taxonomy {taxonomy_id} has {total_elements} products (> {MAX_PAGES*PAGE_SIZE} cap); deeper split needed")

    async def fetch_page(page: int) -> list[dict]:
        params = {**params0, "page": page}
        async with sem:
            data = await request_json(client, "GET", SEARCH_URL, params=params, headers=headers)
        return list((data or {}).get("products") or [])

    if total_pages > 1:
        rest = await asyncio.gather(*(fetch_page(p) for p in range(1, total_pages)))
        for page_products in rest:
            products.extend(page_products)
    return products


async def fetch_detail(client: httpx.AsyncClient, sem: asyncio.Semaphore, token: str, webshop_id: int) -> dict | None:
    async with sem:
        return await request_json(
            client, "GET", DETAIL_URL.format(wid=webshop_id), headers=base_headers(token), retries=3
        )


def envelope(card: dict, detail: dict | None) -> dict:
    return {
        "store": STORE,
        "scraped_at": now_iso(),
        "external_id": str(card.get("webshopId")),
        "raw": {"card": card, "detail": detail},
    }


async def run(*, limit: int | None, no_detail: bool, max_leaves: int | None) -> None:
    out_path = default_output_path(STORE)
    async with httpx.AsyncClient(http2=True) as client:
        token = await get_token(client)
        print("Got AH anonymous token")
        leaves = await list_leaf_taxonomies(client, token)
        if max_leaves:
            leaves = leaves[:max_leaves]
        print(f"Walking {len(leaves)} leaf taxonomies")

        search_sem = asyncio.Semaphore(SEARCH_CONCURRENCY)
        cards_by_id: dict[str, dict] = {}
        for index, leaf in enumerate(leaves, start=1):
            cards = await search_taxonomy(client, search_sem, token, leaf["id"])
            for card in cards:
                wid = str(card.get("webshopId"))
                if wid and wid != "None":
                    cards_by_id.setdefault(wid, card)
            print(f"  [{index}/{len(leaves)}] {leaf.get('name')}: +{len(cards)} (total unique {len(cards_by_id)})")
            if limit and len(cards_by_id) >= limit:
                break

        ids = list(cards_by_id.keys())
        if limit:
            ids = ids[:limit]
        print(f"Collected {len(ids)} unique products")

        details: dict[str, dict | None] = {}
        if not no_detail:
            detail_sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

            async def hydrate(wid: str) -> None:
                try:
                    details[wid] = await fetch_detail(client, detail_sem, token, int(wid))
                except Exception as error:  # keep the full listing even if one detail endpoint is blocked
                    print(f"  ! detail {wid} failed: {type(error).__name__}: {str(error)[:160]}")
                    details[wid] = None

            # hydrate in chunks to bound memory and show progress
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                await asyncio.gather(*(hydrate(wid) for wid in chunk))
                print(f"  detail {min(start + 500, len(ids))}/{len(ids)}")

        with JsonlWriter(out_path) as writer:
            for wid in ids:
                detail = details.get(wid)
                detail_card = (detail or {}).get("productCard")
                merged_detail = detail
                writer.write(envelope(cards_by_id[wid], merged_detail))
        print(f"Wrote {len(ids)} products to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape the Albert Heijn catalog into a bronze JSONL artifact.")
    parser.add_argument("--limit", type=int, default=None, help="Cap total products (smoke test)")
    parser.add_argument("--max-leaves", type=int, default=None, help="Cap number of taxonomies walked (smoke test)")
    parser.add_argument("--no-detail", action="store_true", help="Skip the per-product detail call")
    args = parser.parse_args()
    asyncio.run(run(limit=args.limit, no_detail=args.no_detail, max_leaves=args.max_leaves))


if __name__ == "__main__":
    main()
