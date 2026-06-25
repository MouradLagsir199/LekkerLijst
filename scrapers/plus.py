"""PLUS supermarket (plus.nl) catalog scraper.

PLUS is an OutSystems Reactive Web app. There is no clean mobile API: we drive
the same screen-service POST endpoints the browser uses, impersonating Chrome
via ``curl_cffi`` so the TLS/JA3 fingerprint passes the edge anti-bot.

  - Categories (menu) : POST /screenservices/ECP_Product_CW/Categories/CategoryList_TF/DataActionGetMenuCategories
  - Product list (PLP): POST /screenservices/ECP_Composition_CW/ProductLists/PLP_Content/DataActionGetProductListAndCategoryInfo
  - Product detail(PDP): POST /screenservices/ECP_Product_CW/ProductDetails/PDPContent/DataActionGetProductDetailsAndAgeInfo

Completeness: we enumerate top-level menu categories (falling back to a hardcoded
slug list), walk every PLP page (PageNumber 1..TotalPages) collecting product
slugs, dedupe, then PDP-fetch each slug for the rich block (EAN, nutrition,
allergens, ingredients). The PLP listing carries price + image; the PDP detail
carries the trade-item data. We store BOTH merged under ``raw``. No normalization.

Anti-bot + stale tokens: the OutSystems ``moduleVersion`` and per-action
``apiVersion`` constants drift on every PLUS deploy, and the CSRF token rotates.
``moduleVersion`` self-heals from the public manifest. The CSRF token and
apiVersions are scraped fresh from a live product-list page's OutSystems bootstrap
when the seed constants are rejected (HTTP 403 "Invalid Login" / version-changed).

This mirrors the reference scraper (scrapers.ah): every store module emits the
same envelope
  {"store", "scraped_at", "external_id", "raw": {...}}
to a JSONL artifact, which scrapers.bronze_ingest loads into catalog.bronze_products.

Usage:
    python -m scrapers.plus                                  # full catalog -> Output/plus_bronze.jsonl
    python -m scrapers.plus --limit 25 --max-categories 2    # smoke test
    python -m scrapers.plus --no-detail                      # fast: PLP listing only
"""

from __future__ import annotations

import argparse
import re
import time
from typing import Any

from curl_cffi import requests as curl_requests

from .common import JsonlWriter, default_output_path, now_iso, should_retry

STORE = "plus"
BASE_URL = "https://www.plus.nl"

URL_PRODUCT_LIST = (
    BASE_URL
    + "/screenservices/ECP_Composition_CW/ProductLists/PLP_Content"
    + "/DataActionGetProductListAndCategoryInfo"
)
URL_PRODUCT_DETAIL = (
    BASE_URL
    + "/screenservices/ECP_Product_CW/ProductDetails/PDPContent"
    + "/DataActionGetProductDetailsAndAgeInfo"
)
URL_MENU_CATEGORIES = (
    BASE_URL
    + "/screenservices/ECP_Product_CW/Categories/CategoryList_TF"
    + "/DataActionGetMenuCategories"
)
URL_MODULE_VERSION = BASE_URL + "/moduleservices/moduleversioninfo"

# ── Stale token seeds (refreshed at runtime if rejected) ─────────────────────
# These drift on every PLUS deploy. Treated as a starting point only: the
# scraper self-heals moduleVersion from the manifest and re-scrapes the CSRF
# token + apiVersions from a live page when the server rejects them.
CSRF_TOKEN = "T6C+9iB49TLra4jEsMeSckDMNhQ="
MODULE_VERSION = "aYUiHBQTI6MJSUYDwYY6gQ"
API_VERSION_LIST = "cafT+CKg7ockKx+9Kx_BsQ"
API_VERSION_DETAIL = "CDRjyW8mae+R63Y3xIWPrQ"
API_VERSION_MENU = "hgxmcT1MOcvN0BntQ3hEaA"

CATEGORY_SLUGS_FALLBACK = [
    "aardappelen-groente-fruit",
    "vlees-kip-vis-vega",
    "zuivel-eieren-boter",
    "brood-gebak-bakproducten",
    "ontbijtgranen-broodbeleg-tussendoor",
    "kaas-vleeswaren-tapas",
    "diepvries",
    "snoep-koek-chocolade-chips-noten",
    "frisdrank-sappen-koffie-thee",
    "wijn-bier-sterke-drank",
    "pasta-rijst-internationale-keuken",
    "soepen-conserven-sauzen-smaakmakers",
    "baby-drogisterij",
    "bewuste-voeding",
    "huishouden",
    "koken-non-food-service",
    "huisdier",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def base_headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "nl-NL,nl;q=0.9",
        "Content-Type": "application/json; charset=UTF-8",
        "Origin": BASE_URL,
        "outsystems-locale": "nl-NL",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "x-csrftoken": CSRF_TOKEN,
    }


# ── OutSystems request scaffolds ─────────────────────────────────────────────
# OutSystems requires every list-type screen variable to be present with an
# empty-item template (EmptyListItem). These were captured verbatim from a
# working PLUS browser request; without them the server returns HTTP 400.

_EMPTY_PLP_PRODUCT = {
    "SKU": "", "Brand": "", "Name": "", "Product_Subtitle": "", "Slug": "", "ImageURL": "",
    "ImageLabel": "", "MetaTitle": "", "MetaDescription": "", "OriginalPrice": "0",
    "NewPrice": "0", "Quantity": 0, "LineItemId": "", "IsProductOverMajorityAge": False,
    "Logos": {k: {"List": [], "EmptyListItem": {"Name": "", "LongDescription": "", "URL": "", "Order": 0}}
              for k in ("PLPInUpperLeft", "PLPAboveTitle", "PLPBehindSizeUnit")},
    "EAN": "", "Packging": "",
    "Categories": {"List": [], "EmptyListItem": {"Name": ""}},
    "IsAvailable": False, "PromotionLabel": "", "PromotionBasedLabel": "",
    "PromotionStartDate": "1900-01-01", "PromotionEndDate": "1900-01-01",
    "IsFreeDeliveryOffer": False, "IsOfflineSaleOnly": False,
    "MaxOrderLimit": 0, "CitrusAdId": "", "IsLocalItem": False,
}

_EMPTY_PDP_PRODUCT = {
    "Overview": {"Name": "", "Subtitle": "", "Brand": "", "Slug": "",
                 "Image": {"Label": "", "URL": ""},
                 "Meta": {"Description": "", "Title": ""},
                 "IsNIX18": False, "Price": "0", "BaseUnitPrice": "",
                 "LineItem": {"Id": "", "Quantity": 0},
                 "IsOfflineSaleOnly": False, "IsServiceItem": False,
                 "IsAvailableInStore": False, "MaxOrderLimit": 0,
                 "IsNoIndex": False, "IsLocalItem": False},
    "ProductClassificationId": "",
    "Categories": {"List": [], "EmptyListItem": {"Name": ""}},
    "Logos": {k: {"List": [], "EmptyListItem": {"Name": "", "LongDescription": "", "URL": "", "Order": 0}}
              for k in ("PDPInUpperLeft", "PDPInProductInformation",
                        "PDPBehindSizeUnit", "PDPBelowAddToCart",
                        "PDPAboveTitle", "PDPInRemarks")},
    "Legal": {"RegulatedName": "", "HealthClaim": "",
              "DrainWeight": {"UoM": "", "Value": 0},
              "RequiredNotificationByLaw": "", "AppointedAuthority": "",
              "AdittionalClassification": {"System": "", "Trades": ""}},
    "UsageDuring": {"BreastFeeding": "", "Pregnancy": "", "SafePeriodAfterOpening": 0},
    "Marketing": {"Description": "", "UniqueSellingPoint": "", "Message": ""},
    "SupplierContact": {"LegalContact": {"Address": "", "Name": ""},
                        "LegalSupplier": {"Address": "", "Name": ""},
                        "PDP_ProductMeans": {k: {"List": [], "EmptyListItem": ""}
                                             for k in ("Email_List", "SocialMedia_List", "Contact_List", "WebSites_List")}},
    "Composition": "", "Ingredients": "",
    "Nutrient": {"Base": {"UoM": "", "Value": 0},
                 "Additional": {"NutricionalClaim": "", "PreparedDeviation": "", "ReferenceIntake": ""},
                 "Nutrients": {"List": [], "EmptyListItem": {"TypeCode": "", "UnitCode": "", "Description": "", "ParentCode": "", "DailyValueIntakePercent": "", "QuantityContained": {"Value": "0", "UoM": ""}, "SortOrder": 0}}},
    "Allergen": {"Warning": "", "Description_Contains": "", "Description_MayContain": ""},
    "InstructionsAndSuggestions": {"Instructions": {"Preparation": "", "Storage": "", "Usage": ""},
                                   "Suggestions": {"Serving": ""}},
    "PercentageOfAlcohol": "",
    "Beer": {"Kind": "", "Taste": "", "FoodAdvice": "", "Description": {"Long": "", "Short": ""}},
    "Wine": {"Type": "", "Quote": "", "LongDescription": "", "Flavour": "", "GrapeVariety": "", "Country": "", "Region": "", "WineTastingNote": {"FoodAdvice": "", "SmellAndTaste": "", "FoodAdvices": {"List": [], "EmptyListItem": ""}}, "Awards": {"List": [], "EmptyListItem": ""}},
    "SeaFood": {"Production": {"Method": ""}, "Catch": {"Areas": "", "Methods": ""}},
    "PetFood": {"TargetConsumptionBy": "", "Feed": {"Instructions": "", "Type": ""}, "FoodStatetment": {"Additive": "", "AnalyticalConstituents": "", "Composition": ""}},
    "Medicine": {"EAN": ""},
    "DrugStore": {"Store": {"Origin": "", "Number": {"RVG": "", "RVH": ""}, "Certification": {"Agency": "", "Standard": ""}},
                  "Dosage": {"Admnistration": "", "Recommendation": ""},
                  "SideEffectsAndWarnings": ""},
    "HealthCare": {"UsageAge": {"Description": "", "Max": {"UoM": "", "Value": 0}, "Min": {"UoM": "", "Value": 0}},
                   "SunProtection": {"Category": "", "Factor": ""}},
    "LightBulb": {"BaseType": "", "LampTypeCode": "", "NumberOfSwitches": "", "SuitableForAccentLighting": "",
                  "DeclaredPower": {"UoM": "", "Value": 0}, "EquivalentPower": {"UoM": "", "Value": 0},
                  "Diameter": {"UoM": "", "Value": 0}, "VisibleLight": {"UoM": "", "Value": 0},
                  "ColourTemperature": {"Avg": {"UoM": "", "Value": 0}, "Max": {"UoM": "", "Value": 0}, "Min": {"UoM": "", "Value": 0}},
                  "WarmUpTime": {"UoM": "", "Value": 0}},
    "Battery": {"Voltage": {"UoM": "", "Value": "0"}, "Capacity": {"UoM": "", "Value": 0},
                "Weight": {"UoM": "", "Value": "0"}, "Quantity": 0, "MaterialAgency": "", "Type": "",
                "TechnologyTypes": {"List": [], "EmptyListItem": ""}, "IsRechargeable": False,
                "BuiltIn": {"IsBuiltIn": False, "Quantity": 0}},
    "Hazardous": {"ChildSafeClosure": "",
                  "Chemical": {"Identification": "", "Name": "", "Organisation": "", "Concentration": 0},
                  "SafetyRecommendations": {"List": [], "EmptyListItem": {"Key": "", "Value": ""}},
                  "HazardDesignations": {"List": [], "EmptyListItem": {"Key": "", "Value": ""}},
                  "GHSSignal": {"Symbols": "", "Word": ""}},
    "IsVisibleSection": {k: False for k in ("AboutThisBeer", "AboutThisProduct", "AboutThisWine",
                                            "AllergieInfo", "HandyInfo", "Ingredients", "LegalInfo",
                                            "NutrionalValues", "PreparationInstruction", "ServingSuggestions",
                                            "SupplierContact", "TasteInfo", "UsageAndStorage")},
    "IsNoIndex": False,
}


def build_menu_categories_payload() -> dict:
    return {
        "versionInfo": {"moduleVersion": MODULE_VERSION, "apiVersion": API_VERSION_MENU},
        "viewName": "MainFlow.ProductListPage",
        "screenData": {"variables": {}},
    }


def build_category_payload(category_slug: str, page_number: int = 1) -> dict:
    return {
        "versionInfo": {"moduleVersion": MODULE_VERSION, "apiVersion": API_VERSION_LIST},
        "viewName": "MainFlow.ProductListPage",
        "screenData": {"variables": {
            "AppliedFiltersList": {"List": [], "EmptyListItem": {"Name": "", "Quantity": "0", "IsSelected": False, "URL": ""}},
            "LocalCategoryID": 0, "LocalCategoryName": "", "LocalCategoryParentId": 0, "LocalCategoryTitle": "",
            "IsLoadingMore": False, "IsFirstDataFetched": False, "ShowFilters": False, "IsShowData": False,
            "StoreNumber": 0, "StoreChannel": "", "CheckoutId": "00000000-0000-0000-0000-000000000000",
            "IsOrderEditMode": False,
            "ProductList_All": {"List": [], "EmptyListItem": _EMPTY_PLP_PRODUCT},
            "PageNumber": page_number, "SelectedSort": "", "OrderEditId": "",
            "IsListRendered": False, "IsAlreadyFetch": False, "IsPromotionBannersFetched": False,
            "Period": {"FromDate": "2026-01-01", "ToDate": "2030-01-01"},
            "UserStoreId": "0",
            "FilterExpandedList": {"List": [], "EmptyListItem": False},
            "ItemsInCart": {"List": [], "EmptyListItem": {
                "LineItemId": "", "SKU": "",
                "MainCategory": {"Name": "", "Webkey": "", "OrderHint": "0"},
                "Quantity": 0, "Name": "", "Subtitle": "", "Brand": "",
                "Image": {"Label": "", "URL": ""},
                "ItemTypeAttributeId": "", "DepositFee": "0", "Slug": "", "ChannelId": "",
                "Promotion": {"BasedLabel": "", "Label": "", "StampURL": "", "NewPrice": "0", "IsFreeDelivery": False},
                "IsNIX18": False, "Price": "0", "MaxOrderLimit": 0, "QuantityOfFreeProducts": 0,
            }},
            "HideDummy": False,
            "OneWelcomeUserId": "", "_oneWelcomeUserIdInDataFetchStatus": 1,
            "CategorySlug": category_slug, "_categorySlugInDataFetchStatus": 1,
            "SearchKeyword": "", "_searchKeywordInDataFetchStatus": 1,
            "IsDesktop": False, "_isDesktopInDataFetchStatus": 1,
            "IsSearch": False, "_isSearchInDataFetchStatus": 1,
            "URLPageNumber": 0, "_uRLPageNumberInDataFetchStatus": 1,
            "FilterQueryURL": "", "_filterQueryURLInDataFetchStatus": 1,
            "IsMobile": True, "_isMobileInDataFetchStatus": 1,
            "IsTablet": False, "_isTabletInDataFetchStatus": 1,
            "Monitoring_FlowTypeId": 3, "_monitoring_FlowTypeIdInDataFetchStatus": 1,
            "IsCustomerUnderAge": False, "_isCustomerUnderAgeInDataFetchStatus": 1,
        }},
    }


def build_detail_payload(slug: str) -> dict:
    """The detail endpoint wants SKU + ProductName, split from the slug.

      slug        = 'plus-boerentrots-kipfilet-2-stuks-stuk-350-g-563318'
      SKU         = '563318'                                       (trailing digits)
      ProductName = 'plus-boerentrots-kipfilet-2-stuks-stuk-350-g' (slug without -SKU)
    """
    m = re.match(r"^(.*)-(\d+)$", slug)
    if not m:
        product_name, sku = slug, ""
    else:
        product_name, sku = m.group(1), m.group(2)

    return {
        "versionInfo": {"moduleVersion": MODULE_VERSION, "apiVersion": API_VERSION_DETAIL},
        "viewName": "MainFlow.ProductDetailsPage",
        "screenData": {"variables": {
            "ShowMedicineSidebar": False,
            "Product": _EMPTY_PDP_PRODUCT,
            "ChannelId": "",
            "Locale": "nl-NL",
            "StoreId": "0",
            "StoreNumber": 0,
            "CheckoutId": "00000000-0000-0000-0000-000000000000",
            "OrderEditId": "",
            "IsOrderEditMode": False,
            "TotalLineItemQuantity": 0,
            "ShoppingListProducts": {"List": [], "EmptyListItem": {"SKU": "", "Quantity": "0"}},
            "HasDailyValueIntakePercent": False,
            "CartPromotionDeliveryDate": "2026-01-01",
            "LineItemQuantity": 0,
            "Disclaimers": {"List": [], "EmptyListItem": {
                "DisclaimerType": "", "Text": "", "InternalTitle": ""
            }},
            "IsPhone": True, "_isPhoneInDataFetchStatus": 1,
            "OneWelcomeUserId": "", "_oneWelcomeUserIdInDataFetchStatus": 1,
            "SKU": sku, "_sKUInDataFetchStatus": 1,
            "TotalCartItems": 0, "_totalCartItemsInDataFetchStatus": 1,
            "ProductName": product_name,
            "_productNameInDataFetchStatus": 1,
        }},
    }


# ── Token refresh (self-heal) ────────────────────────────────────────────────

# Pull a base64-looking CSRF token out of the OutSystems bootstrap embedded in a
# product page. OutSystems serializes it as "csrfToken":"<base64>" (or x-csrftoken).
_CSRF_RE = re.compile(r'["\']?(?:x-csrftoken|csrfToken|csrftoken)["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=_-]{16,})["\']')
# Per-action apiVersion appears next to the action URL in the bootstrap manifest.
_APIVERSION_RE = re.compile(r'["\']?apiVersion["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=_-]{8,})["\']')


def refresh_module_version(session: "curl_requests.Session") -> bool:
    """Fetch PLUS's current global OutSystems module token from the public manifest."""
    global MODULE_VERSION
    try:
        response = session.get(
            URL_MODULE_VERSION,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "OutSystems-client-env": "browser",
            },
            params={"cache_buster": str(int(time.time() * 1000))},
            timeout=20,
        )
        response.raise_for_status()
        token = str(response.json().get("versionToken") or "").strip()
    except Exception as exc:
        print(f"  Could not refresh PLUS module version: {exc!r}")
        return False
    if not token:
        print("  PLUS module version response did not contain a versionToken.")
        return False
    if token != MODULE_VERSION:
        MODULE_VERSION = token
        print(f"  Refreshed PLUS moduleVersion from the live manifest: {token}")
        return True
    return False


def refresh_csrf_token(session: "curl_requests.Session") -> bool:
    """Scrape a fresh CSRF token from a live PLUS product-list page's bootstrap JS.

    Returns True if a token was found and differs from the current one.
    """
    global CSRF_TOKEN
    page_url = f"{BASE_URL}/producten/aardappelen-groente-fruit"
    try:
        resp = session.get(
            page_url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=30,
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        print(f"  Could not fetch PLUS page to refresh CSRF token: {exc!r}")
        return False

    # OutSystems may stash the token in a referenced moduleservices script too.
    candidates = _CSRF_RE.findall(html)
    if not candidates:
        for script_url in re.findall(r'src=["\']([^"\']*moduleservices[^"\']*)["\']', html):
            full = script_url if script_url.startswith("http") else BASE_URL + script_url
            try:
                js = session.get(full, headers={"User-Agent": USER_AGENT}, timeout=20).text
            except Exception:
                continue
            candidates = _CSRF_RE.findall(js)
            if candidates:
                break

    if not candidates:
        print("  Could not locate a CSRF token in the PLUS page bootstrap.")
        return False

    token = candidates[0]
    if token and token != CSRF_TOKEN:
        CSRF_TOKEN = token
        print(f"  Refreshed PLUS CSRF token from live page: {token}")
        return True
    return False


# ── HTTP ─────────────────────────────────────────────────────────────────────

def make_session() -> "curl_requests.Session":
    return curl_requests.Session(impersonate="chrome124")


def post(
    session: "curl_requests.Session",
    url: str,
    payload: dict,
    referer: str,
    *,
    retries: int = 3,
    allow_self_heal: bool = True,
) -> dict | None:
    """POST an OutSystems action, self-healing stale tokens once on rejection.

    Returns parsed JSON, or None for an unrecoverable 400/403/network failure.
    """
    headers = {**base_headers(), "Referer": referer}
    backoff = 2.0
    for attempt in range(1, retries + 1):
        try:
            r = session.post(url, headers=headers, json=payload, timeout=30)
        except Exception as exc:
            if attempt == retries:
                print(f"  network error: {exc!r}")
                return None
            time.sleep(backoff)
            backoff *= 2
            continue

        if r.status_code == 403:
            body = r.text[:200]
            if allow_self_heal and ("Invalid Login" in body or "csrf" in body.lower()):
                print(f"  HTTP 403 (token rejected): {body}")
                if refresh_csrf_token(session):
                    headers = {**base_headers(), "Referer": referer}
                    return post(session, url, _with_versions(payload), referer,
                                retries=retries, allow_self_heal=False)
            print(f"  HTTP 403: {body}")
            return None

        if r.status_code == 400:
            print(f"  HTTP 400: {r.text[:400]}")
            return None

        if should_retry(r.status_code):
            if attempt == retries:
                print(f"  HTTP {r.status_code} (giving up): {r.text[:200]}")
                return None
            time.sleep(backoff)
            backoff *= 2
            continue

        try:
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            print(f"  bad response: {exc!r}")
            return None

        # OutSystems echoes whether our version tokens went stale. Self-heal once.
        vi = data.get("versionInfo", {}) if isinstance(data, dict) else {}
        if allow_self_heal and (vi.get("hasModuleVersionChanged") or vi.get("hasApiVersionChanged")):
            healed = False
            if vi.get("hasModuleVersionChanged"):
                healed = refresh_module_version(session) or healed
            if vi.get("hasApiVersionChanged"):
                print(f"  WARNING: PLUS apiVersion is stale; refreshing CSRF/page tokens. versionInfo={vi}")
                healed = refresh_csrf_token(session) or healed
            if healed:
                return post(session, url, _with_versions(payload), referer,
                            retries=1, allow_self_heal=False)
        return data
    return None


def _with_versions(payload: dict) -> dict:
    """Re-stamp a payload's versionInfo with the (possibly refreshed) module version."""
    vi = payload.get("versionInfo")
    if isinstance(vi, dict):
        vi["moduleVersion"] = MODULE_VERSION
    return payload


# ── Parsing (structure only — no normalization of the raw payload) ───────────

def parse_category_response(resp: dict) -> tuple[list[dict], int]:
    """Returns (plp_products, total_pages).

    Shape: data.ProductList.List = [ {"PLP_Str": {...product fields...}}, ... ]
    """
    d = resp.get("data", {}) if isinstance(resp, dict) else {}
    products: list[dict] = []
    pl = d.get("ProductList")
    if isinstance(pl, dict) and isinstance(pl.get("List"), list):
        for item in pl["List"]:
            if isinstance(item, dict):
                core = item.get("PLP_Str") or item
                if isinstance(core, dict):
                    products.append(core)
    total_pages = int(d.get("TotalPages") or 1)
    return products, total_pages


def parse_detail_product(resp: dict) -> dict | None:
    """Return the full PDP ``data`` block (raw, unnormalized).

    The rich product lives under ``data.ProductOut`` (fallback ``data.Product``),
    but useful siblings ride alongside it: ``ImageURL``, ``Label``,
    ``LegalInfoText``, ``ProductClassificationId``, ``IsUnderAge`` etc. We keep
    the whole ``data`` object so nothing is dropped, only skipping the request
    echo. Returns None when there is no product object at all.
    """
    d = resp.get("data", {}) if isinstance(resp, dict) else {}
    if not isinstance(d, dict):
        return None
    if not (isinstance(d.get("ProductOut"), dict) or isinstance(d.get("Product"), dict)):
        return None
    return d


def _menu_record(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    cat = item.get("Category_str") or item
    if not isinstance(cat, dict) or not cat.get("Slug"):
        return None
    return {
        "name": (cat.get("Name") or "").strip(),
        "slug": cat.get("Slug"),
        "external_id": int(cat.get("ExternalId") or 0),
        "parent_external_id": int(cat.get("ParentExternalId") or 0),
        "has_child": bool(cat.get("HasChild")),
        "sort_order": float(cat.get("SortOrder") or 0),
    }


def parse_menu_categories(resp: dict) -> list[dict]:
    data = resp.get("data", {}) if isinstance(resp, dict) else {}
    raw_items: list = []
    categories = data.get("Categories")
    if isinstance(categories, dict) and isinstance(categories.get("List"), list):
        raw_items = categories["List"]
    elif isinstance(data.get("CategoriesJson"), str) and data["CategoriesJson"]:
        try:
            import json as _json
            raw_items = _json.loads(data["CategoriesJson"])
        except Exception:
            raw_items = []

    records, seen = [], set()
    for item in raw_items:
        rec = _menu_record(item)
        if not rec or rec["slug"] in seen:
            continue
        seen.add(rec["slug"])
        records.append(rec)
    return sorted(records, key=lambda r: (r["parent_external_id"] != 0, r["sort_order"], r["name"].lower()))


def top_level_slugs(session: "curl_requests.Session") -> list[str]:
    """Top-level menu slugs (parent==0, id!=0, has_child); fallback to hardcoded."""
    resp = post(session, URL_MENU_CATEGORIES, build_menu_categories_payload(), f"{BASE_URL}/producten")
    cats = parse_menu_categories(resp) if resp else []
    top = [c for c in cats if c["parent_external_id"] == 0 and c["external_id"] != 0 and c["has_child"]]
    if top:
        print(f"Discovered {len(top)} top-level categories from the live PLUS menu.")
        return [c["slug"] for c in top]
    print("Live menu empty/unavailable; using fallback category slugs.")
    return CATEGORY_SLUGS_FALLBACK


# ── Envelope ─────────────────────────────────────────────────────────────────

def envelope(sku: str, plp: dict | None, pdp: dict | None) -> dict:
    raw: dict[str, Any] = {}
    if plp:
        raw["plp"] = plp
    if pdp:
        raw["pdp"] = pdp
    return {
        "store": STORE,
        "scraped_at": now_iso(),
        "external_id": str(sku),
        "raw": raw,
    }


def _sku_from_slug(slug: str) -> str | None:
    m = re.search(r"-(\d+)$", slug or "")
    return m.group(1) if m else None


# ── Orchestration ────────────────────────────────────────────────────────────

def walk_category(
    session: "curl_requests.Session",
    slug: str,
    *,
    limit: int | None,
    collected: int,
    sleep_between: float = 0.3,
) -> dict[str, dict]:
    """Walk all PLP pages of one category. Returns {sku: plp_product} keyed by SKU."""
    print(f"\n=== Category: {slug} ===")
    plp_by_sku: dict[str, dict] = {}
    page, total_pages = 1, 1
    while page <= total_pages:
        referer = f"{BASE_URL}/producten/{slug}"
        resp = post(session, URL_PRODUCT_LIST, build_category_payload(slug, page), referer)
        if not resp:
            break
        products, total_pages = parse_category_response(resp)
        if not products:
            break
        for p in products:
            sku = str(p.get("SKU") or "").strip() or _sku_from_slug(p.get("Slug") or "")
            if sku:
                plp_by_sku.setdefault(sku, p)
        print(f"  page {page}/{total_pages}: +{len(products)} (category unique {len(plp_by_sku)})")
        if limit and collected + len(plp_by_sku) >= limit:
            break
        page += 1
        if sleep_between:
            time.sleep(sleep_between)
    return plp_by_sku


def run(*, limit: int | None, max_categories: int | None, no_detail: bool, sleep_between: float) -> None:
    out_path = default_output_path(STORE)
    session = make_session()

    # Proactively self-heal the module version before the run (cheap, avoids a
    # guaranteed first-request rejection right after a PLUS deploy).
    refresh_module_version(session)

    slugs = top_level_slugs(session)
    if max_categories:
        slugs = slugs[:max_categories]
    print(f"Walking {len(slugs)} categories (limit={limit}, no_detail={no_detail}).")

    plp_by_sku: dict[str, dict] = {}
    for slug in slugs:
        cat_products = walk_category(
            session, slug, limit=limit, collected=len(plp_by_sku), sleep_between=sleep_between
        )
        for sku, plp in cat_products.items():
            plp_by_sku.setdefault(sku, plp)
        if limit and len(plp_by_sku) >= limit:
            break

    skus = list(plp_by_sku.keys())
    if limit:
        skus = skus[:limit]
    print(f"\nCollected {len(skus)} unique products across {len(slugs)} categories.")

    pdp_by_sku: dict[str, dict | None] = {}
    if not no_detail:
        for i, sku in enumerate(skus, start=1):
            slug = (plp_by_sku[sku].get("Slug") or "").strip()
            if not slug:
                pdp_by_sku[sku] = None
                continue
            resp = post(session, URL_PRODUCT_DETAIL, build_detail_payload(slug), f"{BASE_URL}/product/{slug}")
            pdp_by_sku[sku] = parse_detail_product(resp) if resp else None
            if i % 10 == 0 or i == len(skus):
                print(f"  detail {i}/{len(skus)}")
            if sleep_between:
                time.sleep(sleep_between)

    with JsonlWriter(out_path) as writer:
        for sku in skus:
            writer.write(envelope(sku, plp_by_sku.get(sku), pdp_by_sku.get(sku)))
    print(f"\nWrote {len(skus)} products to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape the PLUS (plus.nl) catalog into a bronze JSONL artifact.")
    parser.add_argument("--limit", type=int, default=None, help="Cap total products (smoke test)")
    parser.add_argument("--max-categories", type=int, default=None, help="Cap number of categories walked (smoke test)")
    parser.add_argument("--no-detail", action="store_true", help="Skip the per-product PDP detail call (PLP listing only)")
    parser.add_argument("--sleep", type=float, default=0.3, help="Seconds to sleep between requests")
    args = parser.parse_args()
    run(limit=args.limit, max_categories=args.max_categories, no_detail=args.no_detail, sleep_between=args.sleep)


if __name__ == "__main__":
    main()
