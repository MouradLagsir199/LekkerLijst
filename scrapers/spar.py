"""SPAR (spar.nl) catalog scraper.

SPAR exposes no public JSON product API: the product pages have no
``__NEXT_DATA__`` blob, no ``/api/`` XHR, no algolia/typesense search and no
graphql endpoint. They are server-rendered HTML whose structured data lives in
a schema.org Product JSON-LD ``<script>`` (sku, gtin13/gtin, brand, category,
image, offers/price, availability) plus visible "product information" HTML
sections (package size, ingredients, allergen info, nutrition table, contact
details). So the reverse-engineered data source here is JSON-LD + HTML sections,
not a clean API.

Product universe: the public product sitemap
``https://www.spar.nl/sitemap/products.xml`` (parse every ``<loc>``). For each
product URL we fetch the page and extract:
  - the parsed JSON-LD Product object (the cleanest structured payload), and
  - the parsed product-information sections (ingredients, allergens, nutrition,
    etc.) plus a few derived fields (visible price, images, nutriscore).

Like every store module this emits the shared bronze envelope
  {"store", "scraped_at", "external_id", "raw": {...}}
to a JSONL artifact, which scrapers.bronze_ingest loads into
catalog.bronze_products. The scraper stays dumb: it only parses and dumps raw
payloads; no normalization happens here.

Usage:
    python -m scrapers.spar                 # full catalog -> Output/spar_bronze.jsonl
    python -m scrapers.spar --limit 25      # smoke test (first ~25 products)

Note: SPAR says offers/prices are applied in the cart and may not show on a
product page when no store is selected, so price coverage is not guaranteed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import unicodedata
from html import unescape
from typing import Any
from urllib.parse import urljoin

import httpx

from .common import JsonlWriter, backoff_sleep, default_output_path, now_iso, should_retry

STORE = "spar"

BASE_URL = "https://www.spar.nl"
PRODUCT_SITEMAP_URL = f"{BASE_URL}/sitemap/products.xml"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

CONCURRENCY = 8

# ---------------------------------------------------------------------------
# Parsers (reverse-engineered from the SPAR product page markup; reused from the
# proven Spar/spar_scraper.py extraction logic, trimmed to what feeds `raw`).
# ---------------------------------------------------------------------------

LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
DETAIL_RE = re.compile(
    r'<details[^>]*class=["\'][^"\']*collapsible[^"\']*content[^"\']*["\'][^>]*>'
    r"(?P<body>.*?)</details>",
    re.IGNORECASE | re.DOTALL,
)
ARTICLE_RE = re.compile(r"<article[^>]*>(?P<body>.*?)</article>", re.IGNORECASE | re.DOTALL)
PRICE_RE = re.compile(
    r'<div[^>]*class=["\'][^"\']*c-offer__price[^"\']*["\'][^>]*>.*?'
    r'<span[^>]*class=["\'][^"\']*c-price__euro[^"\']*["\'][^>]*>'
    r"(?P<euro>.*?)</span>.*?"
    r'<span[^>]*class=["\'][^"\']*c-price__cent[^"\']*["\'][^>]*>'
    r"(?P<cent>.*?)</span>",
    re.IGNORECASE | re.DOTALL,
)
H1_RE = re.compile(
    r'<h1[^>]*class=["\'][^"\']*c-offer__title[^"\']*["\'][^>]*>(.*?)</h1>',
    re.IGNORECASE | re.DOTALL,
)
SUBTITLE_RE = re.compile(
    r'<h2[^>]*class=["\'][^"\']*c-offer__subtitle[^"\']*["\'][^>]*>(.*?)</h2>',
    re.IGNORECASE | re.DOTALL,
)
CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
IMG_ATTR_RE = re.compile(r'\b(?:data-src|src)=["\']([^"\']+)["\']', re.IGNORECASE)
DATA_LAYER_TWID_RE = re.compile(r'"twid"\s*:\s*"?(\d+)"?', re.IGNORECASE)
DATA_LAYER_PRICE_RE = re.compile(r'"price"\s*:\s*"?(?P<price>\d+(?:[.,]\d+)?)"?', re.IGNORECASE)


def clean_text(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, list):
        parts = [clean_text(item) for item in value]
        return " | ".join(part for part in parts if part) or None
    text = str(value)
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = TAG_RE.sub(" ", text)
    text = unescape(text).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def normalize_key(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def absolute_url(value: str | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    return urljoin(BASE_URL, text)


def product_id_from_url(url: str) -> str | None:
    match = re.search(r"-(\d+)/?(?:[?#].*)?$", url)
    return match.group(1) if match else None


def clean_sitemap_url(value: str) -> str | None:
    """Normalize a sitemap <loc> into a requestable URL.

    SPAR's product sitemap can contain embedded control characters, e.g. a tab
    in the middle of a URL. httpx rejects those before any network request, so
    strip ASCII control characters at parse time.
    """
    url = unescape(value)
    url = re.sub(r"[\x00-\x1f\x7f]+", "", url).strip()
    return url or None


def parse_sitemap_locs(xml_text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in LOC_RE.finditer(xml_text):
        url = clean_sitemap_url(match.group(1))
        if url and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def iter_jsonld_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_jsonld_objects(child)
    elif isinstance(value, list):
        for item in value:
            yield from iter_jsonld_objects(item)


def type_matches(obj: dict[str, Any], type_name: str) -> bool:
    raw_type = obj.get("@type")
    if isinstance(raw_type, list):
        return any(str(item).lower() == type_name.lower() for item in raw_type)
    return str(raw_type).lower() == type_name.lower()


def parse_json_ld(html: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (Product, BreadcrumbList) JSON-LD objects if present."""
    product: dict[str, Any] | None = None
    breadcrumb: dict[str, Any] | None = None
    for match in JSON_LD_RE.finditer(html):
        raw = match.group(1).strip()
        data = None
        for candidate in (raw, unescape(raw)):
            try:
                data = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
        if data is None:
            continue
        for obj in iter_jsonld_objects(data):
            if product is None and type_matches(obj, "Product"):
                product = obj
            elif breadcrumb is None and type_matches(obj, "BreadcrumbList"):
                breadcrumb = obj
        if product is not None and breadcrumb is not None:
            break
    return product, breadcrumb


def breadcrumb_items(breadcrumb: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not breadcrumb:
        return []
    items: list[dict[str, Any]] = []
    for raw_item in breadcrumb.get("itemListElement") or []:
        if not isinstance(raw_item, dict):
            continue
        item = raw_item.get("item")
        if isinstance(item, dict):
            name = clean_text(item.get("name"))
            url = absolute_url(item.get("@id"))
        else:
            name = clean_text(raw_item.get("name"))
            url = absolute_url(raw_item.get("item"))
        if name or url:
            items.append({"position": raw_item.get("position"), "name": name, "url": url})
    return items


def extract_first(pattern: re.Pattern[str], html: str) -> str | None:
    match = pattern.search(html)
    return clean_text(match.group(1)) if match else None


def extract_canonical(html: str, fallback_url: str) -> str:
    match = CANONICAL_RE.search(html)
    return absolute_url(match.group(1)) if match else fallback_url


def parse_price_from_parts(euro: str, cent: str) -> str | None:
    euro_text = re.sub(r"\D", "", clean_text(euro) or "")
    cent_text = re.sub(r"\D", "", clean_text(cent) or "")
    if not euro_text:
        return None
    if not cent_text:
        cent_text = "00"
    if len(cent_text) == 1:
        cent_text = f"0{cent_text}"
    return f"{euro_text}.{cent_text[:2]}"


def extract_visible_price(html: str) -> str | None:
    match = PRICE_RE.search(html)
    if not match:
        return None
    return parse_price_from_parts(match.group("euro"), match.group("cent"))


def extract_data_layer_price(html: str) -> str | None:
    match = DATA_LAYER_PRICE_RE.search(html)
    if not match:
        return None
    return match.group("price").replace(",", ".")


def extract_twid(html: str) -> str | None:
    match = DATA_LAYER_TWID_RE.search(html)
    return match.group(1) if match else None


def extract_images(html: str, schema_image: Any) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                add(item)
            return
        url = absolute_url(value)
        if not url or url in seen:
            return
        if "media.spar.nl/" not in url and "/content/img/product-not-available" not in url:
            return
        seen.add(url)
        images.append(url)

    add(schema_image)
    for match in IMG_ATTR_RE.finditer(html):
        add(match.group(1))
    return images


def parse_articles(section_html: str) -> dict[str, str]:
    articles: dict[str, str] = {}
    for article_match in ARTICLE_RE.finditer(section_html):
        article_html = article_match.group("body")
        strong_match = re.search(
            r"<strong[^>]*>(?P<head>.*?)</strong>", article_html, re.IGNORECASE | re.DOTALL
        )
        if not strong_match:
            continue
        heading = clean_text(strong_match.group("head"))
        key = normalize_key(heading)
        value_html = article_html[: strong_match.start()] + article_html[strong_match.end() :]
        value = clean_text(value_html)
        if key and value:
            articles[key] = value
    return articles


def parse_information_sections(html: str) -> dict[str, dict[str, Any]]:
    """Parse the collapsible 'product information' blocks (omschrijving,
    ingredienten, voedingswaarden, bewaren, gebruik, contactgegevens, ...)."""
    sections: dict[str, dict[str, Any]] = {}
    for detail_match in DETAIL_RE.finditer(html):
        detail_html = detail_match.group("body")
        title_match = re.search(r"<h2[^>]*>(?P<title>.*?)</h2>", detail_html, re.I | re.S)
        if not title_match:
            continue
        title = clean_text(title_match.group("title"))
        key = normalize_key(title)
        section_match = re.search(
            r'<section[^>]*class=["\'][^"\']*product-information-block[^"\']*["\'][^>]*>'
            r"(?P<body>.*?)</section>",
            detail_html,
            re.IGNORECASE | re.DOTALL,
        )
        body = section_match.group("body") if section_match else detail_html
        if key:
            sections[key] = {
                "title": title,
                "text": clean_text(body),
                "articles": parse_articles(body),
                "html": body,
            }
    return sections


def extract_nutrients(sections: dict[str, dict[str, Any]]) -> tuple[list[dict[str, str]], str | None]:
    section = sections.get("voedingswaarden")
    if not section:
        return [], None
    body = str(section.get("html") or "")
    pre_table = body.split("product-information-table", 1)[0]
    basis_note = clean_text(pre_table)
    rows: list[dict[str, str]] = []
    basis: str | None = None
    for name_html, value_html in re.findall(
        r"<p[^>]*>\s*<span[^>]*>(.*?)</span>\s*<span[^>]*>(.*?)</span>\s*</p>",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        name = clean_text(name_html)
        value = clean_text(value_html)
        if not name or not value:
            continue
        if normalize_key(name) == "soort":
            basis = value
            continue
        rows.append({"name": name, "value": value, "basis": basis})
    return rows, basis or basis_note


def extract_nutriscore(html: str) -> str | None:
    normalized = unescape(html)
    patterns = [
        r"nutri[-_\s]?score[^a-e0-9]{0,25}([a-e])\b",
        r"nutriscore[^a-e0-9]{0,25}([a-e])\b",
        r"nutri[-_\s]?score[-_\s]?([a-e])(?:\.|_|-|/)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def schema_offer(product_schema: dict[str, Any] | None) -> dict[str, Any]:
    if not product_schema:
        return {}
    offers = product_schema.get("offers")
    if isinstance(offers, list):
        return next((offer for offer in offers if isinstance(offer, dict)), {})
    return offers if isinstance(offers, dict) else {}


def gtin_from_schema(product_schema: dict[str, Any] | None) -> str | None:
    if not product_schema:
        return None
    return (
        clean_text(product_schema.get("gtin13"))
        or clean_text(product_schema.get("gtin"))
        or clean_text(product_schema.get("gtin14"))
        or clean_text(product_schema.get("gtin8"))
    )


def extract_raw(html: str, *, url: str) -> dict[str, Any]:
    """Build the full raw payload for one product page.

    Stores the parsed JSON-LD Product object verbatim plus the parsed
    product-information sections and a few derived/visible fields. No
    normalization beyond parsing.
    """
    product_schema, breadcrumb_schema = parse_json_ld(html)
    offer = schema_offer(product_schema)
    sections = parse_information_sections(html)
    breadcrumbs = breadcrumb_items(breadcrumb_schema)

    # Sections without the heavy raw `html` (keep title/text/articles).
    section_payload = {
        key: {
            "title": value.get("title"),
            "text": value.get("text"),
            "articles": value.get("articles"),
        }
        for key, value in sections.items()
        if value.get("text") or value.get("articles")
    }

    ingredient_articles = sections.get("ingredienten", {}).get("articles") or {}
    nutrients, nutrient_basis = extract_nutrients(sections)
    images = extract_images(html, (product_schema or {}).get("image"))

    return {
        "url": url,
        "canonical_url": extract_canonical(html, url),
        "source": "spar_product_page_jsonld_html",
        # Cleanest structured payload: the schema.org Product JSON-LD verbatim.
        "json_ld_product": product_schema,
        "json_ld_offer": offer or None,
        "breadcrumbs": breadcrumbs,
        # Reverse-engineered HTML sections + derived fields.
        "product_name": clean_text((product_schema or {}).get("name")) or extract_first(H1_RE, html),
        "brand": clean_text((product_schema or {}).get("brand")),
        "package": extract_first(SUBTITLE_RE, html)
        or (sections.get("omschrijving", {}).get("articles") or {}).get("inhoud_en_gewicht"),
        "category": clean_text((product_schema or {}).get("category")),
        "sku": clean_text((product_schema or {}).get("sku")) or product_id_from_url(url),
        "gtin13": gtin_from_schema(product_schema),
        "twid": extract_twid(html),
        "price_jsonld": clean_text(offer.get("price")),
        "price_visible": extract_visible_price(html),
        "price_data_layer": extract_data_layer_price(html),
        "price_currency": clean_text(offer.get("priceCurrency")),
        "price_valid_until": clean_text(offer.get("priceValidUntil")),
        "availability": clean_text(offer.get("availability")),
        "images": images,
        "ingredients": ingredient_articles.get("ingredienten"),
        "allergen_info": ingredient_articles.get("allergie_informatie"),
        "nutrients": nutrients,
        "nutrient_basis": nutrient_basis,
        "nutriscore": extract_nutriscore(html),
        "product_information_sections": section_payload,
    }


# ---------------------------------------------------------------------------
# Fetch + run
# ---------------------------------------------------------------------------


def base_headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    }


async def fetch_sitemap_urls(client: httpx.AsyncClient) -> list[str]:
    resp = await client.get(PRODUCT_SITEMAP_URL, timeout=60.0)
    resp.raise_for_status()
    return parse_sitemap_locs(resp.text)


async def fetch_product(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
    *,
    retries: int = 4,
) -> dict[str, Any] | None:
    """Fetch one product page and return its envelope; None on hard failure/skip."""
    async with sem:
        for attempt in range(1, retries + 1):
            try:
                resp = await client.get(url, timeout=60.0)
            except httpx.InvalidURL as error:
                print(f"  ! invalid SPAR URL skipped: {url!r} ({error})")
                return None
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == retries:
                    return None
                await backoff_sleep(attempt, base=1.25, mult=1.8)
                continue
            if resp.status_code in (404, 410):
                return None
            if should_retry(resp.status_code) and attempt < retries:
                await backoff_sleep(attempt, base=1.25, mult=1.8)
                continue
            if resp.status_code >= 400:
                return None
            raw = extract_raw(resp.text, url=str(resp.url))
            external_id = raw.get("gtin13") or raw.get("sku") or product_id_from_url(str(resp.url))
            return {
                "store": STORE,
                "scraped_at": now_iso(),
                "external_id": str(external_id) if external_id else None,
                "raw": raw,
            }
    return None


async def run(*, limit: int | None) -> None:
    out_path = default_output_path(STORE)
    timeout = httpx.Timeout(60.0, connect=30.0)
    limits = httpx.Limits(max_connections=max(20, CONCURRENCY * 2))
    async with httpx.AsyncClient(
        headers=base_headers(), follow_redirects=True, timeout=timeout, limits=limits
    ) as client:
        urls = await fetch_sitemap_urls(client)
        print(f"Product sitemap lists {len(urls)} product URLs")
        if limit:
            urls = urls[:limit]
        print(f"Fetching {len(urls)} product pages (concurrency {CONCURRENCY})")

        sem = asyncio.Semaphore(CONCURRENCY)
        tasks = [asyncio.create_task(fetch_product(client, sem, url)) for url in urls]

        written = 0
        skipped = 0
        with JsonlWriter(out_path) as writer:
            for done, task in enumerate(asyncio.as_completed(tasks), start=1):
                try:
                    envelope = await task
                except Exception as error:  # keep one malformed page from killing the run
                    print(f"  ! SPAR product task failed: {type(error).__name__}: {str(error)[:160]}")
                    skipped += 1
                    continue
                if envelope is None:
                    skipped += 1
                    continue
                writer.write(envelope)
                written += 1
                if done % 100 == 0 or done == len(tasks):
                    print(f"  [{done}/{len(tasks)}] written {written}, skipped {skipped}")
        print(f"Wrote {written} products to {out_path} ({skipped} skipped)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape the SPAR (spar.nl) catalog into a bronze JSONL artifact."
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap total products (smoke test)")
    args = parser.parse_args()
    asyncio.run(run(limit=args.limit))


if __name__ == "__main__":
    main()
