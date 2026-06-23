"""
Dirk.nl catalog scraper
=======================

Uses Dirk's public GraphQL gateway instead of scraping rendered HTML. The
frontend exposes the same gateway and API key in its Nuxt payload.

Strategy:
  1. Fetch departments and web groups with listDepartments.
  2. Fetch each web group's current store assortment/prices with
     listWebGroupProducts(webGroupId). Dirk also exposes huge historical
     productIds lists; those are opt-in with --include-inactive.
  3. Hydrate products with product(productId) to collect barcode/GTIN, product
     details, ingredients, allergens, and nutrients.

Setup:
    pip install httpx

Usage:
    python Dirk/dirk_scraper.py --probe
    python Dirk/dirk_scraper.py --all
    python Dirk/dirk_scraper.py --all --limit 1000
    python Dirk/dirk_scraper.py --query komkommer --limit 25
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import httpx


BASE_URL = "https://www.dirk.nl"
BOODSCHAPPEN_URL = f"{BASE_URL}/boodschappen"
GRAPHQL_URL = "https://web-gateway.dirk.nl/graphql"
FILES_BASE_URL = "https://web-fileserver.dirk.nl/"
DEFAULT_STORE_ID = 66
GATEWAY_API_KEY = "6d3a42a3-6d93-4f98-838d-bcc0ab2307fd"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "Output" / "dirk_products.csv"
DEFAULT_DISCOVERY_OUT = (
    Path(__file__).resolve().parents[1] / "Output" / "dirk_discovery.json"
)

CSV_COLUMNS = [
    "catalog_rank",
    "product_id",
    "article_number",
    "gtin",
    "ean",
    "barcode",
    "product_name",
    "brand",
    "department",
    "webgroup",
    "categories_json",
    "packaging",
    "description",
    "main_description",
    "sub_description",
    "additional_description",
    "normal_price",
    "offer_price",
    "effective_price",
    "base_unit_price",
    "base_unit_price_value",
    "base_unit_price_unit",
    "price_status",
    "price_start_date",
    "price_end_date",
    "price_date",
    "offer_label",
    "offer_start_date",
    "offer_end_date",
    "single_use_plastic",
    "single_use_plastic_value",
    "is_weight_product",
    "max_amount",
    "nutriscore",
    "logos_json",
    "image_url",
    "images_json",
    "ingredients",
    "allergen_info",
    "allergens_json",
    "nutrients_json",
    "nutrient_basis",
    "nutrient_preparation_state",
    "storage_instructions_json",
    "cooking_instructions_json",
    "instructions_for_use_json",
    "contact_information_json",
    "url",
    "store_id",
    "source",
    "scraped_at",
]


LIST_DEPARTMENTS_QUERY = """
query ListDepartments {
  listDepartments {
    departments {
      id
      description
      webGroups {
        description
        webGroupId
        webSubGroups {
          description
          webSubGroupId
        }
      }
    }
  }
}
"""


LIST_WEB_GROUP_PRODUCTS_QUERY = """
query ListWebGroupProducts($webGroupId: Int!, $storeId: Int!) {
  listWebGroupProducts(webGroupId: $webGroupId) {
    productIds
    productAssortment(storeId: $storeId) {
      productId
      productNumber
      normalPrice
      offerPrice
      isSingleUsePlastic
      singleUsePlasticValue
      startDate
      endDate
      priceDate
      productOffer {
        textPriceSign
        startDate
        endDate
        disclaimerStartDate
        disclaimerEndDate
      }
      productInformation {
        productId
        headerText
        subText
        packaging
        image
        department
        webgroup
        brand
        logos {
          description
          position
          link
          image
        }
      }
    }
  }
}
"""


LIST_WEB_GROUP_ASSORTMENT_QUERY = """
query ListWebGroupAssortment($webGroupId: Int!, $storeId: Int!) {
  listWebGroupProducts(webGroupId: $webGroupId) {
    productAssortment(storeId: $storeId) {
      productId
      productNumber
      normalPrice
      offerPrice
      isSingleUsePlastic
      singleUsePlasticValue
      startDate
      endDate
      priceDate
      productOffer {
        textPriceSign
        startDate
        endDate
        disclaimerStartDate
        disclaimerEndDate
      }
      productInformation {
        productId
        headerText
        subText
        packaging
        image
        department
        webgroup
        brand
        logos {
          description
          position
          link
          image
        }
      }
    }
  }
}
"""


PRODUCT_DETAIL_QUERY = """
query ProductDetail($productId: Int!, $storeId: Int!) {
  product(productId: $productId) {
    productId
    articleNumber
    barcode
    brand
    department
    headerText
    packaging
    description
    additionalDescription
    mainDescription
    subDescription
    webgroup
    isWeightProduct
    maxAmount
    images {
      image
      rankNumber
      mainImage
    }
    logos {
      description
      position
      link
      image
    }
    declarations {
      storageInstructions
      cookingInstructions
      instructionsForUse
      ingredients
      contactInformation {
        contactName
        contactAdress
      }
      nutritionalInformation {
        standardPackagingUnit
        soldOrPrepared
        nutritionalValues {
          text
          value
          nutritionalSubValues {
            text
            value
          }
        }
      }
      allergiesInformation {
        text
      }
    }
    productAssortment(storeId: $storeId) {
      productId
      productNumber
      normalPrice
      offerPrice
      isSingleUsePlastic
      singleUsePlasticValue
      startDate
      endDate
      priceDate
      productOffer {
        textPriceSign
        startDate
        endDate
        disclaimerStartDate
        disclaimerEndDate
      }
      productInformation {
        productId
        headerText
        subText
        packaging
        image
        department
        webgroup
        brand
        logos {
          description
          position
          link
          image
        }
      }
    }
  }
}
"""


SEARCH_PRODUCTS_QUERY = """
query SearchProducts($search: String!, $limit: Int!, $storeId: Int!) {
  searchProducts(search: $search, limit: $limit) {
    products {
      ranking
      product {
        productId
        headerText
        brand
        department
        packaging
        image
        webgroup
        productAssortment(storeId: $storeId) {
          productId
          productNumber
          normalPrice
          offerPrice
          isSingleUsePlastic
          singleUsePlasticValue
          startDate
          endDate
          priceDate
          productOffer {
            textPriceSign
            startDate
            endDate
            disclaimerStartDate
            disclaimerEndDate
          }
          productInformation {
            productId
            headerText
            subText
            packaging
            image
            department
            webgroup
            brand
            logos {
              description
              position
              link
              image
            }
          }
        }
      }
    }
  }
}
"""


@dataclass
class ProductSeed:
    product_id: int
    rank: int
    categories: list[dict[str, Any]] = field(default_factory=list)
    assortment: dict[str, Any] | None = None
    search_product: dict[str, Any] | None = None


def default_headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "api_key": GATEWAY_API_KEY,
    }


def as_json(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def clean_text(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, list):
        return " | ".join(str(item).strip() for item in value if str(item).strip()) or None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        dec = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return dec if dec.is_finite() else None


def number_or_none(value: Any) -> float | None:
    dec = decimal_or_none(value)
    return float(dec) if dec is not None else None


def amount_for_csv(value: Any) -> str | None:
    dec = decimal_or_none(value)
    if dec is None:
        return None
    return format(dec.normalize(), "f")


def effective_price(assortment: dict[str, Any] | None) -> Decimal | None:
    if not assortment:
        return None
    offer = decimal_or_none(assortment.get("offerPrice"))
    normal = decimal_or_none(assortment.get("normalPrice"))
    if offer is not None and offer > 0:
        return offer
    return normal


def absolute_file_url(path: str | None, *, width: int | None = None) -> str | None:
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        url = path
    else:
        url = urljoin(FILES_BASE_URL, quote(path.replace("\\", "/"), safe="/"))
    if width and "?" not in url:
        url = f"{url}?width={width}"
    return url


def slugify(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = text.replace("&", " ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def product_url(product: dict[str, Any], assortment: dict[str, Any] | None) -> str | None:
    # Dirk's public product route exists for products in the current store
    # assortment. Some old/catalog-only product IDs return the "helaas" page.
    if not assortment:
        return None
    department = clean_text(product.get("department")) or clean_text(
        assortment.get("productInformation", {}).get("department")
    )
    webgroup = clean_text(product.get("webgroup")) or clean_text(
        assortment.get("productInformation", {}).get("webgroup")
    )
    name = clean_text(product.get("headerText")) or clean_text(
        assortment.get("productInformation", {}).get("headerText")
    )
    product_id = product.get("productId") or assortment.get("productId")
    parts = [slugify(department), slugify(webgroup), slugify(name), str(product_id)]
    if not all(parts):
        return None
    return f"{BOODSCHAPPEN_URL}/{'/'.join(parts)}"


def primary_image(product: dict[str, Any], assortment: dict[str, Any] | None) -> str | None:
    images = product.get("images")
    if isinstance(images, list):
        for image in images:
            if image.get("mainImage") and image.get("image"):
                return absolute_file_url(image.get("image"), width=500)
        for image in images:
            if image.get("image"):
                return absolute_file_url(image.get("image"), width=500)
    info = (assortment or {}).get("productInformation") or {}
    return absolute_file_url(info.get("image") or product.get("image"), width=500)


def combined_logos(product: dict[str, Any], assortment: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw_logos: list[dict[str, Any]] = []
    if isinstance(product.get("logos"), list):
        raw_logos.extend(product["logos"])
    info_logos = ((assortment or {}).get("productInformation") or {}).get("logos")
    if isinstance(info_logos, list):
        raw_logos.extend(info_logos)

    seen: set[tuple[str, str]] = set()
    logos: list[dict[str, Any]] = []
    for logo in raw_logos:
        if not isinstance(logo, dict):
            continue
        key = (str(logo.get("description") or ""), str(logo.get("image") or ""))
        if key in seen:
            continue
        seen.add(key)
        normalized = dict(logo)
        if normalized.get("image"):
            normalized["image_url"] = absolute_file_url(normalized.get("image"))
        logos.append(normalized)
    return logos


def extract_nutriscore(logos: list[dict[str, Any]]) -> str | None:
    for logo in logos:
        desc = str(logo.get("description") or "")
        match = re.search(r"nutri[- ]?score\s*([a-e])\b", desc, re.IGNORECASE)
        if match:
            return match.group(1).upper()
        image = str(logo.get("image") or "")
        match = re.search(r"nutri[-_ ]?score[_ -]?([a-e])\b", image, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def allergen_text(allergies: Any) -> str | None:
    if not isinstance(allergies, list):
        return None
    parts = [str(item.get("text")).strip() for item in allergies if item.get("text")]
    return " | ".join(parts) or None


def compute_base_unit_price(
    packaging: str | None,
    price: Decimal | None,
) -> tuple[str | None, str | None, str | None]:
    if not packaging or price is None or price <= 0:
        return None, None, None

    text = packaging.lower().replace(",", ".")
    if any(marker in text for marker in (" of ", " t/m ", " tot ")):
        return None, None, None

    multiplier = Decimal("1")
    match_multiplier = re.search(r"(\d+(?:\.\d+)?)\s*[x×]\s*", text)
    if match_multiplier:
        multiplier = Decimal(match_multiplier.group(1))
        text = text[match_multiplier.end() :]

    amount_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(kilogram|kilo|kg|gram|gr|g|liter|litre|ltr|l|ml|cl)\b",
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
        elif unit_raw == "cl":
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

    pieces = re.search(r"(\d+(?:\.\d+)?)\s*(stuks|stuk|st\.?|tabs|rollen|zakjes)\b", text)
    if pieces:
        quantity = Decimal(pieces.group(1)) * multiplier
        if quantity > 0:
            value = price / quantity
            value_s = f"{value:.2f}"
            return f"{value_s}/stuk", value_s, "stuk"

    return None, None, None


class DirkGraphQL:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        retries: int = 4,
    ) -> dict[str, Any]:
        backoff = 1.5
        payload = {"query": query, "variables": variables or {}}
        for attempt in range(1, retries + 1):
            try:
                resp = await self.client.post(GRAPHQL_URL, json=payload, timeout=60)
                if resp.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                    print(
                        f"  GraphQL HTTP {resp.status_code}; retry in {backoff:.1f}s"
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 1.8
                    continue
                resp.raise_for_status()
                data = resp.json()
                if data.get("errors"):
                    raise RuntimeError(json.dumps(data["errors"], ensure_ascii=False))
                return data["data"]
            except (httpx.HTTPError, RuntimeError):
                if attempt == retries:
                    raise
                await asyncio.sleep(backoff)
                backoff *= 1.8
        raise RuntimeError("GraphQL query failed")


async def fetch_departments(api: DirkGraphQL) -> list[dict[str, Any]]:
    data = await api.query(LIST_DEPARTMENTS_QUERY)
    return data["listDepartments"]["departments"] or []


def flatten_webgroups(departments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    webgroups: list[dict[str, Any]] = []
    for department in departments:
        for webgroup in department.get("webGroups") or []:
            webgroups.append(
                {
                    "department_id": department.get("id"),
                    "department": department.get("description"),
                    "webgroup_id": webgroup.get("webGroupId"),
                    "webgroup": webgroup.get("description"),
                    "websubgroups": webgroup.get("webSubGroups") or [],
                }
            )
    return webgroups


async def fetch_webgroup_products(
    api: DirkGraphQL,
    webgroup: dict[str, Any],
    store_id: int,
    sem: asyncio.Semaphore,
    include_inactive: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    async with sem:
        data = await api.query(
            LIST_WEB_GROUP_PRODUCTS_QUERY
            if include_inactive
            else LIST_WEB_GROUP_ASSORTMENT_QUERY,
            {"webGroupId": webgroup["webgroup_id"], "storeId": store_id},
        )
    return webgroup, data["listWebGroupProducts"] or {}


async def build_product_seeds(
    api: DirkGraphQL,
    *,
    store_id: int,
    concurrency: int,
    limit: int | None,
    include_inactive: bool,
) -> tuple[list[ProductSeed], list[dict[str, Any]], dict[str, Any]]:
    departments = await fetch_departments(api)
    webgroups = flatten_webgroups(departments)
    print(
        f"GraphQL taxonomy: {len(departments)} departments, "
        f"{len(webgroups)} web groups"
    )

    sem = asyncio.Semaphore(concurrency)
    tasks = [
        asyncio.create_task(
            fetch_webgroup_products(api, wg, store_id, sem, include_inactive)
        )
        for wg in webgroups
        if wg.get("webgroup_id") is not None
    ]

    seeds_by_id: dict[int, ProductSeed] = {}
    ranked_ids: list[int] = []
    raw_product_id_count = 0
    assortment_count = 0

    for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
        webgroup, payload = await task
        category = {
            "department_id": webgroup.get("department_id"),
            "department": webgroup.get("department"),
            "webgroup_id": webgroup.get("webgroup_id"),
            "webgroup": webgroup.get("webgroup"),
        }
        if include_inactive:
            product_ids = [pid for pid in payload.get("productIds") or [] if pid]
            raw_product_id_count += len(product_ids)
            for product_id in product_ids:
                pid = int(product_id)
                if pid not in seeds_by_id:
                    ranked_ids.append(pid)
                    seeds_by_id[pid] = ProductSeed(product_id=pid, rank=len(ranked_ids))
                if category not in seeds_by_id[pid].categories:
                    seeds_by_id[pid].categories.append(category)

        for item in payload.get("productAssortment") or []:
            if not item:
                continue
            pid = int(item.get("productId"))
            assortment_count += 1
            if pid not in seeds_by_id:
                ranked_ids.append(pid)
                seeds_by_id[pid] = ProductSeed(product_id=pid, rank=len(ranked_ids))
            if category not in seeds_by_id[pid].categories:
                seeds_by_id[pid].categories.append(category)
            if seeds_by_id[pid].assortment is None:
                seeds_by_id[pid].assortment = item

        if completed % 25 == 0 or completed == len(tasks):
            print(f"  fetched {completed}/{len(tasks)} web groups")

    seeds = [seeds_by_id[pid] for pid in ranked_ids]
    if limit:
        seeds = seeds[:limit]
    diagnostics = {
        "department_count": len(departments),
        "webgroup_count": len(webgroups),
        "raw_product_id_count": raw_product_id_count,
        "unique_product_id_count": len(seeds_by_id),
        "assortment_rows_seen": assortment_count,
        "products_with_assortment": sum(
            1 for seed in seeds_by_id.values() if seed.assortment is not None
        ),
        "include_inactive_product_ids": include_inactive,
    }
    print(
        f"Collected {len(seeds_by_id)} unique product IDs "
        f"({diagnostics['products_with_assortment']} with store assortment)"
    )
    return seeds, departments, diagnostics


async def build_search_seeds(
    api: DirkGraphQL,
    *,
    query: str,
    store_id: int,
    limit: int,
) -> tuple[list[ProductSeed], dict[str, Any]]:
    data = await api.query(
        SEARCH_PRODUCTS_QUERY,
        {"search": query, "limit": limit, "storeId": store_id},
    )
    results = data.get("searchProducts", {}).get("products") or []
    seeds: list[ProductSeed] = []
    for idx, result in enumerate(results, start=1):
        product = result.get("product") or {}
        product_id = product.get("productId")
        if not product_id:
            continue
        seeds.append(
            ProductSeed(
                product_id=int(product_id),
                rank=int(result.get("ranking") or idx),
                categories=[],
                assortment=product.get("productAssortment"),
                search_product=product,
            )
        )
    return seeds, {"query": query, "result_count": len(seeds)}


async def fetch_product_detail(
    api: DirkGraphQL,
    seed: ProductSeed,
    store_id: int,
    sem: asyncio.Semaphore,
) -> tuple[int, dict[str, Any] | None, str | None]:
    async with sem:
        try:
            data = await api.query(
                PRODUCT_DETAIL_QUERY,
                {"productId": seed.product_id, "storeId": store_id},
            )
            return seed.product_id, data.get("product"), None
        except Exception as exc:
            return seed.product_id, None, str(exc)


async def hydrate_products(
    api: DirkGraphQL,
    seeds: list[ProductSeed],
    *,
    store_id: int,
    concurrency: int,
    checkpoint_every: int,
    out_path: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    products: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(concurrency)
    tasks = [
        asyncio.create_task(fetch_product_detail(api, seed, store_id, sem))
        for seed in seeds
    ]
    seed_by_id = {seed.product_id: seed for seed in seeds}

    for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
        product_id, detail, error = await task
        seed = seed_by_id[product_id]
        if detail:
            detail["_seed"] = {
                "rank": seed.rank,
                "categories": seed.categories,
                "assortment": seed.assortment,
                "search_product": seed.search_product,
            }
            products.append(detail)
        else:
            failures.append({"product_id": product_id, "error": error})
            fallback = seed.search_product or {}
            fallback["_seed"] = {
                "rank": seed.rank,
                "categories": seed.categories,
                "assortment": seed.assortment,
                "search_product": seed.search_product,
            }
            fallback["productId"] = product_id
            products.append(fallback)

        if completed % checkpoint_every == 0 or completed == len(tasks):
            print(f"  hydrated {completed}/{len(tasks)} products")
            if out_path:
                products_sorted = sorted(
                    products,
                    key=lambda item: item.get("_seed", {}).get("rank", math.inf),
                )
                write_csv(products_sorted, out_path, store_id=store_id)

    products.sort(key=lambda item: item.get("_seed", {}).get("rank", math.inf))
    return products, failures


def product_to_row(product: dict[str, Any], rank: int, store_id: int, scraped_at: str) -> dict[str, Any]:
    seed = product.get("_seed") or {}
    assortment = product.get("productAssortment") or seed.get("assortment")
    if not assortment and seed.get("search_product"):
        assortment = seed["search_product"].get("productAssortment")
    declarations = product.get("declarations") or {}
    nutrient_info = declarations.get("nutritionalInformation") or {}
    allergens = declarations.get("allergiesInformation")
    logos = combined_logos(product, assortment)

    packaging = clean_text(product.get("packaging")) or clean_text(
        (assortment or {}).get("productInformation", {}).get("packaging")
    )
    price = effective_price(assortment)
    base_display, base_value, base_unit = compute_base_unit_price(packaging, price)

    normal_price = amount_for_csv((assortment or {}).get("normalPrice"))
    offer_price = amount_for_csv((assortment or {}).get("offerPrice"))
    effective = amount_for_csv(price)
    product_offer = (assortment or {}).get("productOffer") or {}
    barcode = clean_text(product.get("barcode"))

    if assortment and price is not None:
        status = "available"
    elif product.get("productId"):
        status = "not_in_store_assortment"
    else:
        status = "detail_failed"

    return {
        "catalog_rank": rank,
        "product_id": product.get("productId"),
        "article_number": product.get("articleNumber"),
        "gtin": barcode,
        "ean": barcode,
        "barcode": barcode,
        "product_name": clean_text(product.get("headerText"))
        or clean_text((assortment or {}).get("productInformation", {}).get("headerText")),
        "brand": clean_text(product.get("brand"))
        or clean_text((assortment or {}).get("productInformation", {}).get("brand")),
        "department": clean_text(product.get("department"))
        or clean_text((assortment or {}).get("productInformation", {}).get("department")),
        "webgroup": clean_text(product.get("webgroup"))
        or clean_text((assortment or {}).get("productInformation", {}).get("webgroup")),
        "categories_json": as_json(seed.get("categories")),
        "packaging": packaging,
        "description": clean_text(product.get("description")),
        "main_description": clean_text(product.get("mainDescription")),
        "sub_description": clean_text(product.get("subDescription")),
        "additional_description": clean_text(product.get("additionalDescription")),
        "normal_price": normal_price,
        "offer_price": offer_price,
        "effective_price": effective,
        "base_unit_price": base_display,
        "base_unit_price_value": base_value,
        "base_unit_price_unit": base_unit,
        "price_status": status,
        "price_start_date": clean_text((assortment or {}).get("startDate")),
        "price_end_date": clean_text((assortment or {}).get("endDate")),
        "price_date": clean_text((assortment or {}).get("priceDate")),
        "offer_label": clean_text(product_offer.get("textPriceSign")),
        "offer_start_date": clean_text(product_offer.get("startDate")),
        "offer_end_date": clean_text(product_offer.get("endDate")),
        "single_use_plastic": (assortment or {}).get("isSingleUsePlastic"),
        "single_use_plastic_value": amount_for_csv(
            (assortment or {}).get("singleUsePlasticValue")
        ),
        "is_weight_product": product.get("isWeightProduct"),
        "max_amount": product.get("maxAmount"),
        "nutriscore": extract_nutriscore(logos),
        "logos_json": as_json(logos),
        "image_url": primary_image(product, assortment),
        "images_json": as_json(product.get("images")),
        "ingredients": clean_text(declarations.get("ingredients")),
        "allergen_info": allergen_text(allergens),
        "allergens_json": as_json(allergens),
        "nutrients_json": as_json(nutrient_info.get("nutritionalValues")),
        "nutrient_basis": clean_text(nutrient_info.get("standardPackagingUnit")),
        "nutrient_preparation_state": clean_text(nutrient_info.get("soldOrPrepared")),
        "storage_instructions_json": as_json(declarations.get("storageInstructions")),
        "cooking_instructions_json": as_json(declarations.get("cookingInstructions")),
        "instructions_for_use_json": as_json(declarations.get("instructionsForUse")),
        "contact_information_json": as_json(declarations.get("contactInformation")),
        "url": product_url(product, assortment),
        "store_id": store_id,
        "source": "dirk_graphql",
        "scraped_at": scraped_at,
    }


def write_csv(products: list[dict[str, Any]], out_path: Path, *, store_id: int) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    scraped_at = datetime.now(timezone.utc).isoformat()
    with out_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for rank, product in enumerate(products, start=1):
            writer.writerow(product_to_row(product, rank, store_id, scraped_at))


def write_discovery(
    out_path: Path,
    *,
    store_id: int,
    mode: str,
    diagnostics: dict[str, Any],
    product_count: int,
    failures: list[dict[str, Any]],
    started_at: str,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at,
        "strategy": "public_graphql_gateway",
        "mode": mode,
        "store_id": store_id,
        "source_urls": {
            "boodschappen": BOODSCHAPPEN_URL,
            "graphql": GRAPHQL_URL,
            "robots": f"{BASE_URL}/robots.txt",
            "sitemap": f"{BASE_URL}/sitemap.xml",
        },
        "diagnostics": diagnostics,
        "product_count_written": product_count,
        "detail_failures_count": len(failures),
        "detail_failures_sample": failures[:20],
        "note": (
            "Prices are fetched for the store context used by Dirk's frontend "
            f"(store_id={store_id} by default). Barcode is written to gtin/ean "
            "when Dirk exposes it."
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
        api = DirkGraphQL(client)
        if args.query:
            seeds, diagnostics = await build_search_seeds(
                api,
                query=args.query,
                store_id=args.store_id,
                limit=args.limit or 100,
            )
            departments: list[dict[str, Any]] = []
            mode = "query"
        else:
            seeds, departments, diagnostics = await build_product_seeds(
                api,
                store_id=args.store_id,
                concurrency=args.concurrency,
                limit=args.limit,
                include_inactive=args.include_inactive,
            )
            mode = "catalog"

        if args.no_details:
            products = []
            for seed in seeds:
                fallback = seed.search_product or {}
                if seed.assortment and not fallback:
                    fallback = seed.assortment.get("productInformation") or {}
                fallback = dict(fallback)
                fallback["productId"] = fallback.get("productId") or seed.product_id
                fallback["_seed"] = {
                    "rank": seed.rank,
                    "categories": seed.categories,
                    "assortment": seed.assortment,
                    "search_product": seed.search_product,
                }
                products.append(fallback)
            failures: list[dict[str, Any]] = []
        else:
            products, failures = await hydrate_products(
                api,
                seeds,
                store_id=args.store_id,
                concurrency=args.concurrency,
                checkpoint_every=args.checkpoint_every,
                out_path=args.out,
            )

    write_csv(products, args.out, store_id=args.store_id)
    diagnostics["department_payload_count"] = len(departments)
    write_discovery(
        args.discovery_out,
        store_id=args.store_id,
        mode=mode,
        diagnostics=diagnostics,
        product_count=len(products),
        failures=failures,
        started_at=started_at,
    )
    print(f"Wrote {len(products)} products to {args.out}")
    print(f"Wrote discovery diagnostics to {args.discovery_out}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Dirk.nl product catalog")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrape the full catalog. This is also the default without --query.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Small smoke run; equivalent to --limit 20 unless --limit is supplied.",
    )
    parser.add_argument(
        "--query",
        default="",
        help="Optional search query instead of full taxonomy crawl",
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximum products")
    parser.add_argument(
        "--store-id",
        type=int,
        default=DEFAULT_STORE_ID,
        help=f"Dirk store id for prices (default: {DEFAULT_STORE_ID})",
    )
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="Skip product(productId) hydration; faster but omits barcode/nutrients",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help=(
            "Also include Dirk's huge historical productIds lists. Default is "
            "current store assortment only."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Concurrent GraphQL requests",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=250,
        help="Write CSV checkpoints during detail hydration",
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
