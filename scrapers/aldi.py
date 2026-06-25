"""Aldi Netherlands catalog scraper.

Aldi NL's webshop is backed by a PUBLIC Algolia search index, so no auth, no
browser, and no impersonation is needed:
  - Search : POST https://2HU29PF6BH-dsn.algolia.net/1/indexes/an_prd_nl_nl_products2/query
  - Detail : GET  https://www.aldi.nl/product/{productSlug}.html  (Next.js __NEXT_DATA__)

Completeness: the Algolia index is the full catalog (~3000 products). We page
through it with hitsPerPage=1000 and dedupe by objectID. The search hit carries
price, images, brand and categories; EAN / allergens / ingredients / nutrition
are sparse there, so when detail hydration is on we scrape the product page's
embedded Next.js payload (PRODUCT_DETAIL_GET) for whatever richer fields exist.

Aldi NL genuinely does NOT expose EAN/allergens/nutrition for many products even
on the detail page, so coverage of those fields is honestly partial.

This mirrors the reference scraper (scrapers.ah): every store module emits the
same envelope
  {"store", "scraped_at", "external_id", "raw": {...}}
to a JSONL artifact, which scrapers.bronze_ingest loads into catalog.bronze_products.

Usage:
    python -m scrapers.aldi                       # full catalog -> Output/aldi_bronze.jsonl
    python -m scrapers.aldi --limit 50            # smoke test (first ~50 products)
    python -m scrapers.aldi --no-detail           # fast: skip the per-product detail page
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from typing import Any

import httpx

from .common import JsonlWriter, backoff_sleep, default_output_path, now_iso, should_retry

ALGOLIA_APP_ID = "2HU29PF6BH"
ALGOLIA_API_KEY = "686cf0c8ddcf740223d420d1115c94c1"
ALGOLIA_INDEX = "an_prd_nl_nl_products2"
ALGOLIA_URL = f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"

PRODUCT_PAGE_URL = "https://www.aldi.nl/product/{slug}.html"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HITS_PER_PAGE = 1000
DETAIL_CONCURRENCY = 8

STORE = "aldi"

# Pulls the embedded Next.js bootstrap JSON out of the product HTML page.
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


def algolia_headers() -> dict[str, str]:
    return {
        "x-algolia-application-id": ALGOLIA_APP_ID,
        "x-algolia-api-key": ALGOLIA_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


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


async def request_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict | None = None,
    retries: int = 3,
) -> str | None:
    """Return response text, None for 400/404 (skip), raising after exhausting retries."""
    for attempt in range(1, retries + 1):
        try:
            resp = await client.get(url, headers=headers, timeout=60.0)
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == retries:
                return None
            await backoff_sleep(attempt, base=2.0)
            continue
        if resp.status_code in (400, 404):
            return None
        if should_retry(resp.status_code) and attempt < retries:
            await backoff_sleep(attempt, base=2.0)
            continue
        resp.raise_for_status()
        return resp.text
    return None


async def query_page(client: httpx.AsyncClient, page: int) -> dict | None:
    """One Algolia search page."""
    body = {"query": "", "hitsPerPage": HITS_PER_PAGE, "page": page}
    return await request_json(client, "POST", ALGOLIA_URL, json=body, headers=algolia_headers())


async def enumerate_catalog(client: httpx.AsyncClient, limit: int | None) -> list[dict]:
    """Page through the whole Algolia index, deduping hits by objectID."""
    first = await query_page(client, 0)
    if not first:
        raise RuntimeError("Failed to query Aldi Algolia index (page 0)")
    nb_hits = first.get("nbHits", 0)
    nb_pages = first.get("nbPages", 1)
    print(f"Aldi Algolia index: {nb_hits} hits across {nb_pages} page(s) (hitsPerPage={HITS_PER_PAGE})")

    hits_by_id: dict[str, dict] = {}
    for hit in first.get("hits") or []:
        oid = str(hit.get("objectID"))
        if oid and oid != "None":
            hits_by_id.setdefault(oid, hit)

    page = 1
    while page < nb_pages:
        if limit and len(hits_by_id) >= limit:
            break
        data = await query_page(client, page)
        for hit in (data or {}).get("hits") or []:
            oid = str(hit.get("objectID"))
            if oid and oid != "None":
                hits_by_id.setdefault(oid, hit)
        print(f"  page {page + 1}/{nb_pages}: total unique {len(hits_by_id)}")
        page += 1

    return list(hits_by_id.values())


def parse_detail(html: str) -> dict | None:
    """Dig the PRODUCT_DETAIL_GET product object out of the page's __NEXT_DATA__.

    Layout (verified live):
      __NEXT_DATA__ JSON -> props.pageProps.apiData  (a JSON *string*, re-parse)
      -> a list of [opName, payload] entries
      -> find first whose opName == "PRODUCT_DETAIL_GET"
      -> payload.res.products[0]
    Defensive at every hop: anything missing/malformed just yields None.
    """
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        next_data = json.loads(match.group(1))
    except (ValueError, TypeError):
        return None

    api_data = (((next_data or {}).get("props") or {}).get("pageProps") or {}).get("apiData")
    if not api_data:
        return None
    # apiData is itself a JSON-encoded string.
    if isinstance(api_data, str):
        try:
            api_data = json.loads(api_data)
        except (ValueError, TypeError):
            return None
    if not isinstance(api_data, list):
        return None

    for entry in api_data:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        if entry[0] != "PRODUCT_DETAIL_GET":
            continue
        payload = entry[1]
        if not isinstance(payload, dict):
            continue
        products = ((payload.get("res") or {}).get("products")) or []
        if products:
            return products[0]
    return None


async def fetch_detail(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, slug: str
) -> dict | None:
    """Fetch + parse one product detail page. Never raises; returns None on any miss."""
    if not slug:
        return None
    url = PRODUCT_PAGE_URL.format(slug=slug)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    async with sem:
        try:
            html = await request_text(client, url, headers=headers)
        except httpx.HTTPError:
            return None
    if not html:
        return None
    try:
        return parse_detail(html)
    except Exception:  # defensive: malformed page must never crash the run
        return None


def envelope(hit: dict, detail: dict | None) -> dict:
    raw: dict[str, Any] = dict(hit)
    if detail:
        raw["detail"] = detail
    return {
        "store": STORE,
        "scraped_at": now_iso(),
        "external_id": str(hit.get("objectID")),
        "raw": raw,
    }


async def run(*, limit: int | None, no_detail: bool) -> None:
    out_path = default_output_path(STORE)
    async with httpx.AsyncClient() as client:
        hits = await enumerate_catalog(client, limit)
        if limit:
            hits = hits[:limit]
        print(f"Collected {len(hits)} unique products")

        details: dict[str, dict | None] = {}
        if not no_detail:
            detail_sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

            async def hydrate(hit: dict) -> None:
                oid = str(hit.get("objectID"))
                details[oid] = await fetch_detail(client, detail_sem, hit.get("productSlug") or "")

            for start in range(0, len(hits), 200):
                chunk = hits[start : start + 200]
                await asyncio.gather(*(hydrate(hit) for hit in chunk))
                print(f"  detail {min(start + 200, len(hits))}/{len(hits)}")

        with JsonlWriter(out_path) as writer:
            for hit in hits:
                oid = str(hit.get("objectID"))
                writer.write(envelope(hit, details.get(oid)))
        print(f"Wrote {len(hits)} products to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape the Aldi NL catalog into a bronze JSONL artifact.")
    parser.add_argument("--limit", type=int, default=None, help="Cap total products (smoke test)")
    parser.add_argument("--no-detail", action="store_true", help="Skip the per-product detail page hydration")
    args = parser.parse_args()
    asyncio.run(run(limit=args.limit, no_detail=args.no_detail))


if __name__ == "__main__":
    main()
