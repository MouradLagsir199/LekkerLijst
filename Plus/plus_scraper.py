"""
Plus.nl OutSystems API Scraper — final working version
======================================================
The CSRF token is static and stateless. No session/cookies needed for reads.
If the scraper starts returning 403 with "Invalid Login", capture a fresh token:
  1. Open plus.nl in Chrome
  2. DevTools > Application > Cookies > nr2Users
  3. Copy the 'crf=' value, URL-decode it, paste into CSRF_TOKEN below.

Setup:
    pip install curl_cffi pandas

Usage:
    python plus_scraper.py --probe
    python plus_scraper.py --list-categories
    python plus_scraper.py --category aardappelen-groente-fruit
    python plus_scraper.py --all --concurrency 8
"""

import argparse
import csv
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from curl_cffi import requests as cc

# ─── CONFIG — UPDATE THESE WHEN PLUS DEPLOYS ─────────────────────────────────

# Static CSRF token. Lives until Plus rotates it (weeks/months typically).
# If you see 403 "Invalid Login", grab a fresh one from your browser cookies.
CSRF_TOKEN = "T6C+9iB49TLra4jEsMeSckDMNhQ="

# OutSystems version tokens. Also stable until a deploy. moduleVersion is global,
# apiVersion is per-endpoint. If versions are stale, OutSystems returns
# "hasModuleVersionChanged": true in the response and refuses to serve.
MODULE_VERSION         = "aYUiHBQTI6MJSUYDwYY6gQ"
API_VERSION_LIST       = "cafT+CKg7ockKx+9Kx_BsQ"
API_VERSION_DETAIL     = "CDRjyW8mae+R63Y3xIWPrQ"
API_VERSION_MENU       = "hgxmcT1MOcvN0BntQ3hEaA"

# ─── ENDPOINTS ────────────────────────────────────────────────────────────────

BASE_URL = "https://www.plus.nl"
URL_PRODUCT_LIST = (BASE_URL +
    "/screenservices/ECP_Composition_CW/ProductLists/PLP_Content"
    "/DataActionGetProductListAndCategoryInfo")
URL_PRODUCT_DETAIL = (BASE_URL +
    "/screenservices/ECP_Product_CW/ProductDetails/PDPContent"
    "/DataActionGetProductDetailsAndAgeInfo")
URL_MENU_CATEGORIES = (BASE_URL +
    "/screenservices/ECP_Product_CW/Categories/CategoryList_TF"
    "/DataActionGetMenuCategories")

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

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/148.0.0.0 Safari/537.36"),
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

CSV_COLUMNS = [
    "product_id", "gtin", "slug", "title", "brand", "subtitle",
    "price", "base_unit_price", "categories", "ingredients",
    "allergen_warning", "allergen_contains", "allergen_may_contain",
    "nutriscore", "logos", "regulated_name",
    "is_nix18", "is_available_in_store", "image_url", "url",
    "nutrients_json", "preparation", "storage",
]

# ─── Payload builders ─────────────────────────────────────────────────────────

_EMPTY_PLP_PRODUCT = {
    "SKU":"","Brand":"","Name":"","Product_Subtitle":"","Slug":"","ImageURL":"",
    "ImageLabel":"","MetaTitle":"","MetaDescription":"","OriginalPrice":"0",
    "NewPrice":"0","Quantity":0,"LineItemId":"","IsProductOverMajorityAge":False,
    "Logos":{k:{"List":[],"EmptyListItem":{"Name":"","LongDescription":"","URL":"","Order":0}}
             for k in ("PLPInUpperLeft","PLPAboveTitle","PLPBehindSizeUnit")},
    "EAN":"","Packging":"",
    "Categories":{"List":[],"EmptyListItem":{"Name":""}},
    "IsAvailable":False,"PromotionLabel":"","PromotionBasedLabel":"",
    "PromotionStartDate":"1900-01-01","PromotionEndDate":"1900-01-01",
    "IsFreeDeliveryOffer":False,"IsOfflineSaleOnly":False,
    "MaxOrderLimit":0,"CitrusAdId":"","IsLocalItem":False,
}

_EMPTY_PDP_PRODUCT = {
    "Overview":{"Name":"","Subtitle":"","Brand":"","Slug":"",
                "Image":{"Label":"","URL":""},
                "Meta":{"Description":"","Title":""},
                "IsNIX18":False,"Price":"0","BaseUnitPrice":"",
                "LineItem":{"Id":"","Quantity":0},
                "IsOfflineSaleOnly":False,"IsServiceItem":False,
                "IsAvailableInStore":False,"MaxOrderLimit":0,
                "IsNoIndex":False,"IsLocalItem":False},
    "ProductClassificationId":"",
    "Categories":{"List":[],"EmptyListItem":{"Name":""}},
    "Logos":{k:{"List":[],"EmptyListItem":{"Name":"","LongDescription":"","URL":"","Order":0}}
             for k in ("PDPInUpperLeft","PDPInProductInformation",
                       "PDPBehindSizeUnit","PDPBelowAddToCart",
                       "PDPAboveTitle","PDPInRemarks")},
    "Legal":{"RegulatedName":"","HealthClaim":"",
             "DrainWeight":{"UoM":"","Value":0},
             "RequiredNotificationByLaw":"","AppointedAuthority":"",
             "AdittionalClassification":{"System":"","Trades":""}},
    "UsageDuring":{"BreastFeeding":"","Pregnancy":"","SafePeriodAfterOpening":0},
    "Marketing":{"Description":"","UniqueSellingPoint":"","Message":""},
    "SupplierContact":{"LegalContact":{"Address":"","Name":""},
                       "LegalSupplier":{"Address":"","Name":""},
                       "PDP_ProductMeans":{k:{"List":[],"EmptyListItem":""}
                                           for k in ("Email_List","SocialMedia_List","Contact_List","WebSites_List")}},
    "Composition":"","Ingredients":"",
    "Nutrient":{"Base":{"UoM":"","Value":0},
                "Additional":{"NutricionalClaim":"","PreparedDeviation":"","ReferenceIntake":""},
                "Nutrients":{"List":[],"EmptyListItem":{"TypeCode":"","UnitCode":"","Description":"","ParentCode":"","DailyValueIntakePercent":"","QuantityContained":{"Value":"0","UoM":""},"SortOrder":0}}},
    "Allergen":{"Warning":"","Description_Contains":"","Description_MayContain":""},
    "InstructionsAndSuggestions":{"Instructions":{"Preparation":"","Storage":"","Usage":""},
                                   "Suggestions":{"Serving":""}},
    "PercentageOfAlcohol":"",
    "Beer":{"Kind":"","Taste":"","FoodAdvice":"","Description":{"Long":"","Short":""}},
    "Wine":{"Type":"","Quote":"","LongDescription":"","Flavour":"","GrapeVariety":"","Country":"","Region":"","WineTastingNote":{"FoodAdvice":"","SmellAndTaste":"","FoodAdvices":{"List":[],"EmptyListItem":""}},"Awards":{"List":[],"EmptyListItem":""}},
    "SeaFood":{"Production":{"Method":""},"Catch":{"Areas":"","Methods":""}},
    "PetFood":{"TargetConsumptionBy":"","Feed":{"Instructions":"","Type":""},"FoodStatetment":{"Additive":"","AnalyticalConstituents":"","Composition":""}},
    "Medicine":{"EAN":""},
    "DrugStore":{"Store":{"Origin":"","Number":{"RVG":"","RVH":""},"Certification":{"Agency":"","Standard":""}},
                 "Dosage":{"Admnistration":"","Recommendation":""},
                 "SideEffectsAndWarnings":""},
    "HealthCare":{"UsageAge":{"Description":"","Max":{"UoM":"","Value":0},"Min":{"UoM":"","Value":0}},
                  "SunProtection":{"Category":"","Factor":""}},
    "LightBulb":{"BaseType":"","LampTypeCode":"","NumberOfSwitches":"","SuitableForAccentLighting":"",
                 "DeclaredPower":{"UoM":"","Value":0},"EquivalentPower":{"UoM":"","Value":0},
                 "Diameter":{"UoM":"","Value":0},"VisibleLight":{"UoM":"","Value":0},
                 "ColourTemperature":{"Avg":{"UoM":"","Value":0},"Max":{"UoM":"","Value":0},"Min":{"UoM":"","Value":0}},
                 "WarmUpTime":{"UoM":"","Value":0}},
    "Battery":{"Voltage":{"UoM":"","Value":"0"},"Capacity":{"UoM":"","Value":0},
               "Weight":{"UoM":"","Value":"0"},"Quantity":0,"MaterialAgency":"","Type":"",
               "TechnologyTypes":{"List":[],"EmptyListItem":""},"IsRechargeable":False,
               "BuiltIn":{"IsBuiltIn":False,"Quantity":0}},
    "Hazardous":{"ChildSafeClosure":"",
                 "Chemical":{"Identification":"","Name":"","Organisation":"","Concentration":0},
                 "SafetyRecommendations":{"List":[],"EmptyListItem":{"Key":"","Value":""}},
                 "HazardDesignations":{"List":[],"EmptyListItem":{"Key":"","Value":""}},
                 "GHSSignal":{"Symbols":"","Word":""}},
    "IsVisibleSection":{k:False for k in ("AboutThisBeer","AboutThisProduct","AboutThisWine",
                                          "AllergieInfo","HandyInfo","Ingredients","LegalInfo",
                                          "NutrionalValues","PreparationInstruction","ServingSuggestions",
                                          "SupplierContact","TasteInfo","UsageAndStorage")},
    "IsNoIndex":False,
}


def build_category_payload(category_slug: str, page_number: int = 1) -> dict:
    return {
        "versionInfo": {"moduleVersion": MODULE_VERSION,
                        "apiVersion":    API_VERSION_LIST},
        "viewName": "MainFlow.ProductListPage",
        "screenData": {"variables": {
            "AppliedFiltersList":{"List":[],"EmptyListItem":{"Name":"","Quantity":"0","IsSelected":False,"URL":""}},
            "LocalCategoryID":0,"LocalCategoryName":"","LocalCategoryParentId":0,"LocalCategoryTitle":"",
            "IsLoadingMore":False,"IsFirstDataFetched":False,"ShowFilters":False,"IsShowData":False,
            "StoreNumber":0,"StoreChannel":"","CheckoutId":"00000000-0000-0000-0000-000000000000",
            "IsOrderEditMode":False,
            "ProductList_All":{"List":[],"EmptyListItem":_EMPTY_PLP_PRODUCT},
            "PageNumber":page_number,"SelectedSort":"","OrderEditId":"",
            "IsListRendered":False,"IsAlreadyFetch":False,"IsPromotionBannersFetched":False,
            "Period":{"FromDate":"2026-01-01","ToDate":"2030-01-01"},
            "UserStoreId":"0",
            "FilterExpandedList":{"List":[],"EmptyListItem":False},
            "ItemsInCart":{"List":[],"EmptyListItem":{
                "LineItemId":"","SKU":"",
                "MainCategory":{"Name":"","Webkey":"","OrderHint":"0"},
                "Quantity":0,"Name":"","Subtitle":"","Brand":"",
                "Image":{"Label":"","URL":""},
                "ItemTypeAttributeId":"","DepositFee":"0","Slug":"","ChannelId":"",
                "Promotion":{"BasedLabel":"","Label":"","StampURL":"","NewPrice":"0","IsFreeDelivery":False},
                "IsNIX18":False,"Price":"0","MaxOrderLimit":0,"QuantityOfFreeProducts":0,
            }},
            "HideDummy":False,
            "OneWelcomeUserId":"","_oneWelcomeUserIdInDataFetchStatus":1,
            "CategorySlug":category_slug,"_categorySlugInDataFetchStatus":1,
            "SearchKeyword":"","_searchKeywordInDataFetchStatus":1,
            "IsDesktop":False,"_isDesktopInDataFetchStatus":1,
            "IsSearch":False,"_isSearchInDataFetchStatus":1,
            "URLPageNumber":0,"_uRLPageNumberInDataFetchStatus":1,
            "FilterQueryURL":"","_filterQueryURLInDataFetchStatus":1,
            "IsMobile":True,"_isMobileInDataFetchStatus":1,
            "IsTablet":False,"_isTabletInDataFetchStatus":1,
            "Monitoring_FlowTypeId":3,"_monitoring_FlowTypeIdInDataFetchStatus":1,
            "IsCustomerUnderAge":False,"_isCustomerUnderAgeInDataFetchStatus":1,
        }}
    }


def build_menu_categories_payload() -> dict:
    return {
        "versionInfo": {"moduleVersion": MODULE_VERSION,
                        "apiVersion":    API_VERSION_MENU},
        "viewName": "MainFlow.ProductListPage",
        "screenData": {"variables": {}},
    }


def build_detail_payload(slug: str) -> dict:
    """
    The detail endpoint wants SKU + ProductName (NOT the full slug).
      slug = 'plus-boerentrots-kipfilet-2-stuks-stuk-350-g-563318'
      SKU  = '563318'                                              (trailing digits)
      ProductName = 'plus-boerentrots-kipfilet-2-stuks-stuk-350-g' (slug without -SKU)
    """
    m = re.match(r"^(.*)-(\d+)$", slug)
    if not m:
        # Fallback: send the whole slug as ProductName and an empty SKU
        product_name, sku = slug, ""
    else:
        product_name, sku = m.group(1), m.group(2)

    return {
        "versionInfo": {"moduleVersion": MODULE_VERSION,
                        "apiVersion":    API_VERSION_DETAIL},
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
            "IsPhone":            True,  "_isPhoneInDataFetchStatus":            1,
            "OneWelcomeUserId":   "",    "_oneWelcomeUserIdInDataFetchStatus":   1,
            "SKU":                sku,   "_sKUInDataFetchStatus":                1,
            "TotalCartItems":     0,     "_totalCartItemsInDataFetchStatus":     1,
            "ProductName":        product_name,
            "_productNameInDataFetchStatus": 1,
        }}
    }


# ─── HTTP ─────────────────────────────────────────────────────────────────────

def post(session: cc.Session, url: str, payload: dict, referer: str,
         retries: int = 3) -> dict | None:
    headers = {**HEADERS, "Referer": referer}
    backoff = 2.0
    for attempt in range(1, retries + 1):
        try:
            r = session.post(url, headers=headers, json=payload, timeout=30)
            if r.status_code == 403:
                msg = r.text[:200]
                if "Invalid Login" in msg:
                    print(f"  CSRF token rejected. Update CSRF_TOKEN constant.")
                    print(f"  Server said: {msg}")
                    return None
                print(f"  HTTP 403: {msg}")
                return None
            if r.status_code == 400:
                print(f"  HTTP 400: {r.text[:400]}")
                return None
            if r.status_code in {429, 500, 502, 503, 504}:
                if attempt == retries:
                    return None
                time.sleep(backoff)
                backoff *= 2
                continue
            r.raise_for_status()
            data = r.json()
            # Check for version drift
            vi = data.get("versionInfo", {})
            if vi.get("hasModuleVersionChanged") or vi.get("hasApiVersionChanged"):
                print(f"  WARNING: versions stale. Update MODULE_VERSION / API_VERSION_* constants.")
                print(f"  Response versionInfo: {vi}")
            return data
        except Exception as exc:
            if attempt == retries:
                print(f"  network error: {exc!r}")
                return None
            time.sleep(backoff)
            backoff *= 2
    return None


# ─── Parsing ──────────────────────────────────────────────────────────────────

def normalize_gtin(value) -> str | None:
    gtin = re.sub(r"\D", "", str(value or ""))
    return gtin or None


def parse_product_detail(resp: dict, slug: str | None = None,
                         gtin: str | None = None) -> dict:
    data = (resp.get("data", {}).get("ProductOut", {})
            or resp.get("data", {}).get("Product", {}))
    overview = data.get("Overview", {})

    cats = [c.get("Name") for c in data.get("Categories", {}).get("List", []) if c.get("Name")]
    logos = [l.get("Name") for l in
             data.get("Logos", {}).get("PDPInProductInformation", {}).get("List", [])
             if l.get("Name")]
    nutriscore = next(
        (l.replace("Nutri-Score ", "") for l in logos if l.startswith("Nutri-Score")),
        None,
    )

    nutrients_list = data.get("Nutrient", {}).get("Nutrients", {}).get("List", [])
    nutrients = {
        n.get("Description"): {"value": n.get("QuantityContained", {}).get("Value"),
                               "uom":   n.get("QuantityContained", {}).get("UoM")}
        for n in nutrients_list if n.get("Description")
    }

    allergen = data.get("Allergen", {})
    instr    = data.get("InstructionsAndSuggestions", {}).get("Instructions", {})
    legal    = data.get("Legal", {})
    slug_val = overview.get("Slug") or slug
    gtin_val = (normalize_gtin(gtin)
                or normalize_gtin(data.get("EAN"))
                or normalize_gtin(data.get("Medicine", {}).get("EAN")))

    pid = None
    if slug_val:
        m = re.search(r"-(\d+)$", slug_val)
        if m:
            pid = m.group(1)

    return {
        "product_id":            pid,
        "gtin":                  gtin_val,
        "slug":                  slug_val,
        "title":                 overview.get("Name"),
        "brand":                 overview.get("Brand"),
        "subtitle":              overview.get("Subtitle"),
        "price":                 overview.get("Price"),
        "base_unit_price":       overview.get("BaseUnitPrice"),
        "categories":            " > ".join(cats) if cats else None,
        "ingredients":           data.get("Ingredients"),
        "allergen_warning":      allergen.get("Warning"),
        "allergen_contains":     allergen.get("Description_Contains"),
        "allergen_may_contain":  allergen.get("Description_MayContain"),
        "nutriscore":            nutriscore,
        "logos":                 "; ".join(logos) if logos else None,
        "regulated_name":        legal.get("RegulatedName"),
        "is_nix18":              overview.get("IsNIX18"),
        "is_available_in_store": overview.get("IsAvailableInStore"),
        "image_url":             ((overview.get("Image") or {}).get("URL")
                                  or resp.get("data", {}).get("ImageURL")),
        "url":                   f"{BASE_URL}/product/{slug_val}" if slug_val else None,
        "nutrients_json":        json.dumps(nutrients, ensure_ascii=False) if nutrients else None,
        "preparation":           instr.get("Preparation"),
        "storage":               instr.get("Storage"),
    }


def parse_category_response(resp: dict) -> tuple[list[dict], int, int]:
    """
    Returns (products, total_pages, total_items).
    Plus's actual shape: data.ProductList.List = [ {PLP_Str: {...real fields...}}, ... ]
    Each item has the product wrapped under PLP_Str.
    """
    d = resp.get("data", {})
    products: list[dict] = []

    pl = d.get("ProductList")
    if isinstance(pl, dict) and isinstance(pl.get("List"), list):
        for item in pl["List"]:
            # Each item is {"PLP_Str": {...product fields...}}
            if isinstance(item, dict):
                core = item.get("PLP_Str") or item
                if isinstance(core, dict):
                    products.append(core)

    total_pages = d.get("TotalPages", 1)
    total_items = d.get("TotalNumberItems", len(products))
    return products, total_pages, total_items


def _category_record_from_menu_item(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    cat = item.get("Category_str") or item
    if not isinstance(cat, dict) or not cat.get("Slug"):
        return None
    return {
        "name": (cat.get("Name") or "").strip(),
        "slug": cat.get("Slug"),
        "external_id": int(cat.get("ExternalId") or 0),
        "parent_name": (cat.get("ParentName") or "").strip(),
        "parent_external_id": int(cat.get("ParentExternalId") or 0),
        "has_child": bool(cat.get("HasChild")),
        "sort_order": float(cat.get("SortOrder") or 0),
        "is_seasonal": bool(cat.get("IsSeasonal")),
    }


def parse_menu_categories(resp: dict) -> list[dict]:
    """Parse category records from the live PLUS menu endpoint."""
    data = resp.get("data", {})
    raw_items = []

    categories = data.get("Categories")
    if isinstance(categories, dict) and isinstance(categories.get("List"), list):
        raw_items = categories["List"]
    elif isinstance(data.get("CategoriesJson"), str) and data["CategoriesJson"]:
        try:
            raw_items = json.loads(data["CategoriesJson"])
        except json.JSONDecodeError as exc:
            print(f"  Could not parse CategoriesJson: {exc}")
            raw_items = []

    records = []
    seen = set()
    for item in raw_items:
        record = _category_record_from_menu_item(item)
        if not record or record["slug"] in seen:
            continue
        seen.add(record["slug"])
        records.append(record)

    return sorted(records, key=lambda r: (r["parent_external_id"] != 0,
                                          r["sort_order"], r["name"].lower()))


def select_scrape_category_slugs(categories: list[dict]) -> list[str]:
    """Use broad, real top-level category slugs for --all to avoid guessed names."""
    top_level = [
        c for c in categories
        if c["parent_external_id"] == 0 and c["external_id"] != 0 and c["has_child"]
    ]
    if not top_level:
        return CATEGORY_SLUGS_FALLBACK
    return [c["slug"] for c in top_level]


# ─── Top-level operations ─────────────────────────────────────────────────────

def fetch_menu_categories(s: cc.Session) -> list[dict]:
    resp = post(s, URL_MENU_CATEGORIES, build_menu_categories_payload(),
                f"{BASE_URL}/producten")
    if not resp:
        return []
    return parse_menu_categories(resp)


def fetch_category_page(s: cc.Session, slug: str, page: int = 1) -> tuple[list[dict], int]:
    referer = f"{BASE_URL}/producten/{slug}"
    payload = build_category_payload(slug, page_number=page)
    resp = post(s, URL_PRODUCT_LIST, payload, referer)
    if not resp:
        return [], 0
    products, total_pages, _ = parse_category_response(resp)
    return products, total_pages


def fetch_product_detail(s: cc.Session, slug: str,
                         gtin: str | None = None) -> dict | None:
    referer = f"{BASE_URL}/product/{slug}"
    payload = build_detail_payload(slug)
    resp = post(s, URL_PRODUCT_DETAIL, payload, referer)
    if not resp:
        return None
    try:
        return parse_product_detail(resp, slug=slug, gtin=gtin)
    except Exception as exc:
        print(f"  parse error for {slug}: {exc}")
        return None


def make_session() -> cc.Session:
    """Each thread gets its own curl_cffi session."""
    return cc.Session(impersonate="chrome124")


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_probe(args):
    s = make_session()
    cat = args.category or "aardappelen-groente-fruit"

    print(f"[1/2] Category list: {cat}")
    rows, pages = fetch_category_page(s, cat, page=1)
    print(f"  {len(rows)} products on page 1 of {pages}")
    if rows:
        r0 = rows[0]
        print(f"  first: name={r0.get('Name')!r}")
        print(f"         slug={r0.get('Slug')!r}")
        print(f"         price={r0.get('NewPrice')!r}")

    test_slug = (rows[0].get("Slug") if rows and rows[0].get("Slug")
                 else "plus-boerentrots-kipfilet-2-stuks-stuk-350-g-563318")
    print(f"\n[2/2] Product detail: {test_slug}")
    test_gtin = rows[0].get("EAN") if rows else None
    row = fetch_product_detail(s, test_slug, gtin=test_gtin)
    if row:
        print("  SUCCESS:")
        for k in ("title", "brand", "gtin", "price", "nutriscore", "categories", "ingredients"):
            v = row.get(k)
            if v:
                print(f"    {k}: {str(v)[:80]}")
    else:
        print("  FAILED — product detail. Check ProductSlug field name.")


def cmd_list_categories(args):
    categories = fetch_menu_categories(make_session())
    if not categories:
        print("No live categories returned.")
        return

    if args.category_scope == "top":
        categories = [c for c in categories
                      if c["parent_external_id"] == 0 and c["external_id"] != 0]
    elif args.category_scope == "leaf":
        categories = [c for c in categories if not c["has_child"]]

    for c in categories:
        indent = "" if c["parent_external_id"] == 0 else "  "
        parent = f" parent={c['parent_name']!r}" if c["parent_name"] else ""
        child = " +" if c["has_child"] else ""
        print(f"{indent}{c['slug']}{child}  {c['name']!r}{parent}")
    print(f"\n{len(categories)} categories")


def scrape_one_category(slug: str, max_pages: int | None,
                        concurrency: int, sleep_between: float) -> list[dict]:
    """Scrape all pages of one category, then fetch all product details."""
    s = make_session()

    print(f"\n=== Category: {slug} ===")
    all_slugs: list[str] = []
    gtins_by_slug: dict[str, str] = {}
    page = 1
    total_pages = 1
    while page <= total_pages:
        rows, total_pages = fetch_category_page(s, slug, page)
        if not rows:
            break
        page_slugs = [r.get("Slug") for r in rows if r.get("Slug")]
        for r in rows:
            product_slug = r.get("Slug")
            gtin = normalize_gtin(r.get("EAN"))
            if product_slug and gtin:
                gtins_by_slug[product_slug] = gtin
        all_slugs.extend(page_slugs)
        print(f"  page {page}/{total_pages}: +{len(page_slugs)} slugs (total {len(all_slugs)})")
        page += 1
        if max_pages and page > max_pages:
            break
        if sleep_between:
            time.sleep(sleep_between)

    # Dedup while keeping order
    seen = set()
    unique_slugs = [s for s in all_slugs if not (s in seen or seen.add(s))]
    print(f"  unique slugs: {len(unique_slugs)}")

    # Parallel detail fetch — each thread gets its own session
    rows: list[dict] = []
    def fetch_one(slug):
        return fetch_product_detail(make_session(), slug,
                                    gtin=gtins_by_slug.get(slug))

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(fetch_one, sl): sl for sl in unique_slugs}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            if r:
                rows.append(r)
            if i % 25 == 0 or i == len(unique_slugs):
                print(f"  details: {i}/{len(unique_slugs)}")
    return rows


def cmd_scrape_category(args):
    rows = scrape_one_category(args.category, args.max_pages,
                               args.concurrency, args.sleep)
    save_csv(rows, args.out)


def cmd_scrape_all(args):
    categories = fetch_menu_categories(make_session())
    slugs = select_scrape_category_slugs(categories)
    source = "live menu endpoint" if categories else "fallback constants"
    print(f"Using {len(slugs)} top-level category slugs from {source}.")

    all_rows: list[dict] = []
    for slug in slugs:
        rows = scrape_one_category(slug, args.max_pages,
                                   args.concurrency, args.sleep)
        all_rows.extend(rows)
        save_csv(all_rows, args.out)   # progress save after each category
    save_csv(all_rows, args.out)


def save_csv(rows: list[dict], path: str):
    if not rows:
        print("No rows to save.")
        return
    df = (pd.DataFrame(rows)
          .drop_duplicates(subset="product_id")
          .reset_index(drop=True)
          .reindex(columns=CSV_COLUMNS))
    df.to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    print(f"  → saved {len(df)} unique products to {Path(path).resolve()}")


def main():
    ap = argparse.ArgumentParser(description="Plus.nl OutSystems scraper")
    ap.add_argument("--probe",        action="store_true")
    ap.add_argument("--list-categories", action="store_true",
                    help="Print category slugs from the live PLUS menu endpoint")
    ap.add_argument("--category-scope", choices=("top", "leaf", "all"), default="top",
                    help="Which categories to show with --list-categories")
    ap.add_argument("--category",     default=None)
    ap.add_argument("--all",          action="store_true",
                    help="Scrape every top-level category discovered from the live PLUS menu")
    ap.add_argument("--out",          default="plus_products.csv")
    ap.add_argument("--concurrency",  type=int, default=6)
    ap.add_argument("--sleep",        type=float, default=0.3,
                    help="Seconds to sleep between category pages")
    ap.add_argument("--max-pages",    type=int, default=None)
    args = ap.parse_args()

    t0 = time.perf_counter()
    if args.probe:
        cmd_probe(args)
    elif args.list_categories:
        cmd_list_categories(args)
    elif args.all:
        cmd_scrape_all(args)
    elif args.category:
        cmd_scrape_category(args)
    else:
        ap.print_help()
    print(f"\nElapsed: {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
