"""
Spar.nl catalog scraper
=======================

Uses SPAR's public product sitemap plus public product-detail HTML. The
product pages expose schema.org Product JSON-LD for SKU/GTIN/brand/category
and visible HTML blocks for price, package size, ingredients, allergens, and
nutrition tables.

Setup:
    pip install httpx

Usage:
    python Spar/spar_scraper.py --probe
    python Spar/spar_scraper.py --all
    python Spar/spar_scraper.py --all --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import re
import time
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx


BASE_URL = "https://www.spar.nl"
PRODUCTS_URL = f"{BASE_URL}/boodschappen/"
ROBOTS_URL = f"{BASE_URL}/robots.txt"
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap.xml"
PRODUCT_SITEMAP_URL = f"{BASE_URL}/sitemap/products.xml"
CATEGORY_SITEMAP_URL = f"{BASE_URL}/sitemap/categories.xml"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "Output" / "spar_products.csv"
DEFAULT_DISCOVERY_OUT = (
    Path(__file__).resolve().parents[1] / "Output" / "spar_discovery.json"
)

CSV_COLUMNS = [
    "catalog_rank",
    "product_id",
    "twid",
    "gtin",
    "ean",
    "url",
    "canonical_url",
    "product_name",
    "brand",
    "package",
    "short_description",
    "description",
    "category",
    "breadcrumb_json",
    "categories_json",
    "price",
    "base_unit_price",
    "base_unit_price_value",
    "base_unit_price_unit",
    "price_currency",
    "price_valid_until",
    "price_status",
    "availability",
    "is_available",
    "image_url",
    "images_json",
    "ingredients",
    "allergen_info",
    "nutriscore",
    "nutrients_json",
    "nutrient_basis",
    "product_information_json",
    "storage_instructions",
    "usage_instructions",
    "contact_information",
    "source",
    "scraped_at",
    "http_status",
    "fetch_error",
]

PROBE_URLS = [
    "https://www.spar.nl/spar-saucijzenbroodje-3128547/",
    "https://www.spar.nl/healthy-neusspray-htp-xylometazoline-hcl-1,0-mg/ml-1954040/",
    "https://www.spar.nl/snelle-jelle-ontbijtkoek-naturel-repen-3334651/",
]

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
BRAND_RE = re.compile(
    r'<h3[^>]*class=["\'][^"\']*c-offer__brand[^"\']*["\'][^>]*>(.*?)</h3>',
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


def default_headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    }


def as_json(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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


def decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        dec = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return dec if dec.is_finite() else None


def amount_for_csv(value: Any) -> str | None:
    dec = decimal_or_none(value)
    if dec is None:
        return None
    return format(dec.normalize(), "f")


def absolute_url(value: str | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    return urljoin(BASE_URL, text)


def product_id_from_url(url: str) -> str | None:
    match = re.search(r"-(\d+)/?(?:[?#].*)?$", url)
    return match.group(1) if match else None


def parse_sitemap_locs(xml_text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in LOC_RE.finditer(xml_text):
        url = unescape(match.group(1).strip())
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
            items.append(
                {
                    "position": raw_item.get("position"),
                    "name": name,
                    "url": url,
                }
            )
    return items


def extract_first(pattern: re.Pattern[str], html: str) -> str | None:
    match = pattern.search(html)
    return clean_text(match.group(1)) if match else None


def extract_canonical(html: str, fallback_url: str) -> str:
    match = CANONICAL_RE.search(html)
    return absolute_url(match.group(1)) if match else fallback_url


def parse_price_from_parts(euro: str, cent: str) -> Decimal | None:
    euro_text = re.sub(r"\D", "", clean_text(euro) or "")
    cent_text = re.sub(r"\D", "", clean_text(cent) or "")
    if not euro_text:
        return None
    if not cent_text:
        cent_text = "00"
    if len(cent_text) == 1:
        cent_text = f"0{cent_text}"
    return decimal_or_none(f"{euro_text}.{cent_text[:2]}")


def extract_visible_price(html: str) -> Decimal | None:
    match = PRICE_RE.search(html)
    if not match:
        return None
    return parse_price_from_parts(match.group("euro"), match.group("cent"))


def extract_data_layer_price(html: str) -> Decimal | None:
    match = DATA_LAYER_PRICE_RE.search(html)
    return decimal_or_none(match.group("price")) if match else None


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
        if (
            "media.spar.nl/" not in url
            and "/content/img/product-not-available" not in url
        ):
            return
        seen.add(url)
        images.append(url)

    add(schema_image)
    for match in IMG_ATTR_RE.finditer(html):
        add(match.group(1))
    return images


def extract_description(html: str) -> str | None:
    match = re.search(
        r'<p[^>]*class=["\'][^"\']*notification[^"\']*["\'][^>]*>.*?</p>\s*'
        r'<div[^>]*class=["\'][^"\']*c-offer__description[^"\']*["\'][^>]*>\s*'
        r'<div[^>]*class=["\'][^"\']*content[^"\']*["\'][^>]*>(?P<body>.*?)</div>\s*</div>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        return clean_text(match.group("body"))
    descriptions = re.findall(
        r'<div[^>]*class=["\'][^"\']*c-offer__description[^"\']*["\'][^>]*>'
        r"(?P<body>.*?)</div>",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    for fragment in descriptions:
        text = clean_text(fragment)
        if text and "aanbiedingen zijn niet zichtbaar" not in text.lower():
            return text
    return None


def parse_articles(section_html: str) -> dict[str, str]:
    articles: dict[str, str] = {}
    for article_match in ARTICLE_RE.finditer(section_html):
        article_html = article_match.group("body")
        strong_match = re.search(
            r"<strong[^>]*>(?P<head>.*?)</strong>",
            article_html,
            re.IGNORECASE | re.DOTALL,
        )
        if not strong_match:
            continue
        heading = clean_text(strong_match.group("head"))
        key = normalize_key(heading)
        value_html = (
            article_html[: strong_match.start()]
            + article_html[strong_match.end() :]
        )
        value = clean_text(value_html)
        if key and value:
            articles[key] = value
    return articles


def parse_information_sections(html: str) -> dict[str, dict[str, Any]]:
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


def extract_nutrients(
    sections: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, str]], str | None]:
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


def compute_base_unit_price(
    package: str | None,
    price: Decimal | None,
) -> tuple[str | None, str | None, str | None]:
    if not package or price is None or price <= 0:
        return None, None, None

    text = package.lower().replace(",", ".")
    if any(marker in text for marker in (" of ", " t/m ", " tot ")):
        return None, None, None

    multiplier = Decimal("1")
    match_multiplier = re.search(r"(\d+(?:\.\d+)?)\s*[x]\s*", text)
    if match_multiplier:
        multiplier = Decimal(match_multiplier.group(1))
        text = text[match_multiplier.end() :]

    amount_match = re.search(
        r"(\d+(?:\.\d+)?)\s*"
        r"(kilogram|kilo|kg|gram|gr|g|liter|litre|ltr|l|"
        r"milliliter|millilitre|ml|centiliter|centilitre|cl)\b",
        text,
    )
    if amount_match:
        amount = Decimal(amount_match.group(1)) * multiplier
        unit_raw = amount_match.group(2)
        if unit_raw in {"kilogram", "kilo", "kg"}:
            quantity = amount
            unit = "kg"
        elif unit_raw in {"gram", "gr", "g"}:
            quantity = amount / Decimal("1000")
            unit = "kg"
        elif unit_raw in {"liter", "litre", "ltr", "l"}:
            quantity = amount
            unit = "l"
        elif unit_raw in {"centiliter", "centilitre", "cl"}:
            quantity = amount / Decimal("100")
            unit = "l"
        else:
            quantity = amount / Decimal("1000")
            unit = "l"
        if quantity > 0:
            value = price / quantity
            value_s = f"{value:.2f}"
            return f"{value_s}/{unit}", value_s, unit

    if "per stuk" in text:
        value_s = f"{price:.2f}"
        return f"{value_s}/stuk", value_s, "stuk"

    pieces = re.search(
        r"(\d+(?:\.\d+)?)\s*(stuks|stuk|st\.?|tabs|rollen|zakjes|capsules)\b",
        text,
    )
    if pieces:
        quantity = Decimal(pieces.group(1)) * multiplier
        if quantity > 0:
            value = price / quantity
            value_s = f"{value:.2f}"
            return f"{value_s}/stuk", value_s, "stuk"

    return None, None, None


def schema_offer(product_schema: dict[str, Any] | None) -> dict[str, Any]:
    if not product_schema:
        return {}
    offers = product_schema.get("offers")
    if isinstance(offers, list):
        return next((offer for offer in offers if isinstance(offer, dict)), {})
    return offers if isinstance(offers, dict) else {}


def availability_label(raw: Any) -> str | None:
    text = clean_text(raw)
    if not text:
        return None
    return text.rsplit("/", 1)[-1]


def parse_product_page(
    html: str,
    *,
    url: str,
    catalog_rank: int,
    status_code: int,
    scraped_at: str,
) -> dict[str, Any]:
    product_schema, breadcrumb_schema = parse_json_ld(html)
    offer = schema_offer(product_schema)
    sections = parse_information_sections(html)
    breadcrumbs = breadcrumb_items(breadcrumb_schema)
    category_names = [
        item["name"]
        for item in breadcrumbs
        if item.get("name")
        and normalize_key(item.get("name")) != normalize_key(product_schema.get("name") if product_schema else None)
    ]

    title = clean_text((product_schema or {}).get("name")) or extract_first(H1_RE, html)
    brand = clean_text((product_schema or {}).get("brand")) or extract_first(BRAND_RE, html)
    package = extract_first(SUBTITLE_RE, html)
    if not package:
        package = (sections.get("omschrijving", {}).get("articles") or {}).get(
            "inhoud_en_gewicht"
        )

    visible_price = extract_visible_price(html)
    schema_price = decimal_or_none(offer.get("price"))
    data_layer_price = extract_data_layer_price(html)
    price = schema_price or visible_price or data_layer_price
    base_display, base_value, base_unit = compute_base_unit_price(package, price)

    schema_availability = availability_label(offer.get("availability"))
    unavailable_text = "dit product is niet meer leverbaar" in html.lower()
    is_available = None
    if schema_availability:
        is_available = str(schema_availability).lower() == "instock"
    if unavailable_text:
        is_available = False
    if price is None:
        price_status = "missing_price"
    elif is_available is False:
        price_status = "sold_out_price_visible"
    else:
        price_status = "visible_price"

    ingredient_articles = sections.get("ingredienten", {}).get("articles") or {}
    ingredients = ingredient_articles.get("ingredienten")
    allergen_info = ingredient_articles.get("allergie_informatie")
    nutrients, nutrient_basis = extract_nutrients(sections)
    description = extract_description(html)
    short_description = None
    if description:
        short_description = description.split(" | ", 1)[0]

    images = extract_images(html, (product_schema or {}).get("image"))
    gtin = (
        clean_text((product_schema or {}).get("gtin13"))
        or clean_text((product_schema or {}).get("gtin"))
        or clean_text((product_schema or {}).get("gtin14"))
        or clean_text((product_schema or {}).get("gtin8"))
    )
    product_id = clean_text((product_schema or {}).get("sku")) or product_id_from_url(url)

    section_payload = {
        key: {
            "title": value.get("title"),
            "text": value.get("text"),
            "articles": value.get("articles"),
        }
        for key, value in sections.items()
        if value.get("text") or value.get("articles")
    }

    return {
        "catalog_rank": catalog_rank,
        "product_id": product_id,
        "twid": extract_twid(html),
        "gtin": gtin,
        "ean": gtin,
        "url": url,
        "canonical_url": extract_canonical(html, url),
        "product_name": title,
        "brand": brand,
        "package": package,
        "short_description": short_description,
        "description": description or clean_text((product_schema or {}).get("description")),
        "category": clean_text((product_schema or {}).get("category"))
        or " / ".join(category_names[:-1] or category_names)
        or None,
        "breadcrumb_json": as_json(breadcrumbs),
        "categories_json": as_json(category_names[:-1] or category_names),
        "price": amount_for_csv(price),
        "base_unit_price": base_display,
        "base_unit_price_value": base_value,
        "base_unit_price_unit": base_unit,
        "price_currency": clean_text(offer.get("priceCurrency")) or "EUR",
        "price_valid_until": clean_text(offer.get("priceValidUntil")),
        "price_status": price_status,
        "availability": schema_availability,
        "is_available": is_available,
        "image_url": images[0] if images else None,
        "images_json": as_json(images),
        "ingredients": ingredients,
        "allergen_info": allergen_info,
        "nutriscore": extract_nutriscore(html),
        "nutrients_json": as_json(nutrients),
        "nutrient_basis": nutrient_basis,
        "product_information_json": as_json(section_payload),
        "storage_instructions": sections.get("bewaren", {}).get("text"),
        "usage_instructions": sections.get("gebruik", {}).get("text"),
        "contact_information": sections.get("contactgegevens", {}).get("text"),
        "source": "spar_product_page_html",
        "scraped_at": scraped_at,
        "http_status": status_code,
        "fetch_error": None,
    }


def error_row(
    *,
    url: str,
    catalog_rank: int,
    status_code: int | None,
    error: str,
    scraped_at: str,
) -> dict[str, Any]:
    row = {column: None for column in CSV_COLUMNS}
    row.update(
        {
            "catalog_rank": catalog_rank,
            "product_id": product_id_from_url(url),
            "url": url,
            "source": "spar_product_page_html",
            "scraped_at": scraped_at,
            "http_status": status_code,
            "fetch_error": error,
        }
    )
    return row


async def fetch_product_sitemap_urls(client: httpx.AsyncClient) -> list[str]:
    resp = await client.get(PRODUCT_SITEMAP_URL, timeout=60)
    resp.raise_for_status()
    return parse_sitemap_locs(resp.text)


async def fetch_category_sitemap_count(client: httpx.AsyncClient) -> int | None:
    try:
        resp = await client.get(CATEGORY_SITEMAP_URL, timeout=60)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    return len(parse_sitemap_locs(resp.text))


async def fetch_product(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    *,
    url: str,
    catalog_rank: int,
    scraped_at: str,
    retries: int = 4,
) -> dict[str, Any]:
    backoff = 1.25
    async with sem:
        for attempt in range(1, retries + 1):
            try:
                resp = await client.get(url, timeout=60)
                if resp.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                    await asyncio.sleep(backoff)
                    backoff *= 1.8
                    continue
                if resp.status_code >= 400:
                    return error_row(
                        url=url,
                        catalog_rank=catalog_rank,
                        status_code=resp.status_code,
                        error=f"HTTP {resp.status_code}",
                        scraped_at=scraped_at,
                    )
                return parse_product_page(
                    resp.text,
                    url=str(resp.url),
                    catalog_rank=catalog_rank,
                    status_code=resp.status_code,
                    scraped_at=scraped_at,
                )
            except httpx.HTTPError as exc:
                if attempt == retries:
                    return error_row(
                        url=url,
                        catalog_rank=catalog_rank,
                        status_code=None,
                        error=repr(exc),
                        scraped_at=scraped_at,
                    )
                await asyncio.sleep(backoff)
                backoff *= 1.8
    return error_row(
        url=url,
        catalog_rank=catalog_rank,
        status_code=None,
        error="unreachable fetch state",
        scraped_at=scraped_at,
    )


def write_csv(products: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        products,
        key=lambda item: (
            math.inf if item.get("catalog_rank") in (None, "") else int(item["catalog_rank"]),
            str(item.get("product_id") or ""),
        ),
    )
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ordered)


async def scrape_products(
    client: httpx.AsyncClient,
    urls: list[str],
    *,
    concurrency: int,
    checkpoint_every: int,
    out_path: Path,
    scraped_at: str,
) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)
    tasks = [
        asyncio.create_task(
            fetch_product(
                client,
                sem,
                url=url,
                catalog_rank=index + 1,
                scraped_at=scraped_at,
            )
        )
        for index, url in enumerate(urls)
    ]
    products: list[dict[str, Any]] = []
    for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
        products.append(await task)
        if completed % checkpoint_every == 0 or completed == len(tasks):
            write_csv(products, out_path)
            prices = sum(1 for row in products if row.get("price"))
            gtins = sum(1 for row in products if row.get("gtin"))
            print(
                f"  checkpoint {completed}/{len(tasks)}: "
                f"{prices} prices, {gtins} GTIN/EAN, wrote {out_path}"
            )
    return products


def summarize_products(products: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "products": len(products),
        "fetch_errors": sum(1 for row in products if row.get("fetch_error")),
        "prices": sum(1 for row in products if row.get("price")),
        "base_unit_prices": sum(1 for row in products if row.get("base_unit_price")),
        "gtin_ean": sum(1 for row in products if row.get("gtin")),
        "ingredients": sum(1 for row in products if row.get("ingredients")),
        "allergen_info": sum(1 for row in products if row.get("allergen_info")),
        "nutrients": sum(1 for row in products if row.get("nutrients_json")),
        "nutriscore": sum(1 for row in products if row.get("nutriscore")),
        "available": sum(1 for row in products if row.get("is_available") is True),
        "unavailable": sum(1 for row in products if row.get("is_available") is False),
    }


def write_discovery(
    out_path: Path,
    *,
    started_at: str,
    product_sitemap_count: int,
    category_sitemap_count: int | None,
    urls_scraped: int,
    products: list[dict[str, Any]],
    probe: bool,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scraper": "Spar/spar_scraper.py",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "mode": "probe" if probe else "all",
        "sources": {
            "products_page": PRODUCTS_URL,
            "robots": ROBOTS_URL,
            "sitemap_index": SITEMAP_INDEX_URL,
            "product_sitemap": PRODUCT_SITEMAP_URL,
            "category_sitemap": CATEGORY_SITEMAP_URL,
        },
        "strategy": (
            "Use SPAR's public product sitemap as the product universe and parse "
            "each public product page. Product JSON-LD exposes SKU, GTIN/EAN, "
            "brand, category, availability, and usually price; visible page HTML "
            "contains package size, price markup, ingredients, allergens, and "
            "nutrition tables."
        ),
        "product_sitemap_url_count": product_sitemap_count,
        "category_sitemap_url_count": category_sitemap_count,
        "urls_scraped": urls_scraped,
        "summary": summarize_products(products),
        "notes": [
            "SPAR pages say offers are not shown on products and are applied in the cart.",
            "No explicit public base-unit-price field was found in product HTML; base unit prices are computed from package size when possible.",
            "Prices are the public no-store-selected page prices unless SPAR redirects or varies the HTML response.",
        ],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def select_urls(
    sitemap_urls: list[str],
    *,
    limit: int | None,
    probe: bool,
) -> list[str]:
    if probe:
        chosen: list[str] = []
        seen: set[str] = set()
        sitemap_set = set(sitemap_urls)
        for url in PROBE_URLS + sitemap_urls:
            if url not in sitemap_set and url not in PROBE_URLS:
                continue
            if url not in seen:
                chosen.append(url)
                seen.add(url)
            if limit and len(chosen) >= limit:
                break
        return chosen
    return sitemap_urls[:limit] if limit else sitemap_urls


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
        sitemap_urls = await fetch_product_sitemap_urls(client)
        category_count = await fetch_category_sitemap_count(client)
        print(f"Product sitemap lists {len(sitemap_urls)} product URLs")
        if category_count is not None:
            print(f"Category sitemap lists {category_count} category URLs")

        urls = select_urls(sitemap_urls, limit=args.limit, probe=args.probe)
        mode = "probe" if args.probe else "all"
        print(f"Scraping {len(urls)} product pages in {mode} mode")
        products = await scrape_products(
            client,
            urls,
            concurrency=args.concurrency,
            checkpoint_every=args.checkpoint_every,
            out_path=args.out,
            scraped_at=started_at,
        )

    write_csv(products, args.out)
    write_discovery(
        args.discovery_out,
        started_at=started_at,
        product_sitemap_count=len(sitemap_urls),
        category_sitemap_count=category_count,
        urls_scraped=len(urls),
        products=products,
        probe=args.probe,
    )
    summary = summarize_products(products)
    print(f"Wrote {len(products)} products to {args.out}")
    print(f"Wrote discovery diagnostics to {args.discovery_out}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Spar.nl product catalog")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrape all product URLs from the SPAR product sitemap. This is the default.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Small smoke run; equivalent to --limit 20 unless --limit is supplied.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum product pages to scrape",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Concurrent product page requests",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=100,
        help="Write CSV checkpoints during product page scraping",
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
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
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
