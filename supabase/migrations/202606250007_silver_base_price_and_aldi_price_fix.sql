-- Silver: fix the Aldi consumer price and add a normalized base/unit price.
--
-- Two changes, both verified against the full bronze tables:
--
-- 1. ALDI CONSUMER PRICE BUG. The previous function read the Aldi price from
--    {raw,detail,price} / {raw,price} / {raw,salesPrice}. None of those keys
--    exist in Aldi's Algolia payload, so 100% of Aldi silver prices were NULL.
--    Aldi stores the consumer price at currentPrice.priceValue (euros) — present
--    in 5,240 / 6,178 rows (the rest are price-less special-buy/"offer" items).
--
-- 2. BASE / UNIT PRICE. New columns base_price (euros per unit) + base_price_unit
--    (kg / l / stuk / ...). Per-store availability in the loaded bronze:
--      jumbo : price.pricePerUnit { price (cents), unit }            ~98.8%
--      ah    : only the TEXT unitPriceDescription ("prijs per kg EUR 6.86") —
--              parsed to value + unit by regex (~51% of rows carry it)
--      aldi  : native currentPrice.basePrice is CORRUPT (often priceValue*1000 or
--              the raw gram weight, e.g. 375 g product -> basePriceValue 375), so
--              base_price is DERIVED: consumer price / quantity parsed from
--              salesUnit ("125 g" -> /kg, "1.5 l" -> /l, "Per stuk" -> none)
--      dirk  : no native base price (API exposes none)              -> NULL
--      plus  : no native base price (loaded bronze has listing only) -> NULL
--      spar  : no native base price (JSON-LD has none)              -> NULL

ALTER TABLE catalog.silver_products
  ADD COLUMN IF NOT EXISTS base_price      numeric(12, 2),
  ADD COLUMN IF NOT EXISTS base_price_unit text;

-- Normalize a unit token so kg/l are consistent across stores
-- (AH says "liter", Jumbo/Aldi already say "l").
CREATE OR REPLACE FUNCTION catalog.silver_norm_unit(u text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE lower(btrim(u))
    WHEN 'liter'    THEN 'l'
    WHEN 'litre'    THEN 'l'
    WHEN 'kilogram' THEN 'kg'
    WHEN 'kilo'     THEN 'kg'
    WHEN ''         THEN NULL
    ELSE NULLIF(lower(btrim(u)), '')
  END
$$;

-- Derive a per-kg / per-l quantity from a free-text pack-size string. Used for Aldi,
-- whose native basePrice value is unreliable, so we compute price / qty instead.
-- Handles "125 g", "1,5 l", "750 ml", "50 cl", "4 x 0.5 l", "Inhoud: 12 stuks, 600 g".
-- Returns (qty NULL, unit NULL) for per-piece / unparseable units like "Per stuk".
CREATE OR REPLACE FUNCTION catalog.silver_qty_base(sales_unit text, OUT qty numeric, OUT unit text)
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  s    text := lower(coalesce(sales_unit, ''));
  mult numeric := 1;
  mm   text[];
  m    text[];
BEGIN
  qty := NULL; unit := NULL;
  -- optional multipack multiplier, e.g. "4 x 0.5 l"
  mm := regexp_match(s, '([0-9]+)\s*[x×]\s*');
  IF mm IS NOT NULL THEN
    mult := mm[1]::numeric;
  END IF;
  -- volume first (l / ml / cl); lookahead keeps "l" from matching inside "liter"
  m := regexp_match(s, '([0-9]+(?:[.,][0-9]+)?)\s*(l|liter|litre|ml|cl)(?![a-z])');
  IF m IS NOT NULL THEN
    qty := replace(m[1], ',', '.')::numeric * mult;
    IF m[2] = 'ml' THEN qty := qty / 1000;
    ELSIF m[2] = 'cl' THEN qty := qty / 100;
    END IF;
    unit := 'l';
    IF qty > 0 THEN RETURN; END IF;
  END IF;
  -- weight (kg / g / gram)
  m := regexp_match(s, '([0-9]+(?:[.,][0-9]+)?)\s*(kg|kilogram|gram|g)(?![a-z])');
  IF m IS NOT NULL THEN
    qty := replace(m[1], ',', '.')::numeric * mult;
    IF m[2] IN ('g', 'gram') THEN qty := qty / 1000; END IF;
    unit := 'kg';
    IF qty > 0 THEN RETURN; END IF;
  END IF;
  qty := NULL; unit := NULL;
END;
$$;

CREATE OR REPLACE FUNCTION catalog.refresh_silver_products(store_filter text DEFAULT NULL)
RETURNS TABLE(store_id text, rows_refreshed integer)
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  WITH latest AS (
    SELECT DISTINCT ON (bp.store, COALESCE(bp.raw_data->>'external_id', bp.id::text))
      bp.id AS bronze_product_id,
      bp.store,
      COALESCE(bp.raw_data->>'external_id', bp.id::text) AS external_id,
      bp.raw_data,
      bp.scraped_at,
      bp.created_at
    FROM catalog.bronze_products bp
    WHERE store_filter IS NULL OR bp.store = store_filter
    ORDER BY
      bp.store,
      COALESCE(bp.raw_data->>'external_id', bp.id::text),
      bp.scraped_at DESC,
      bp.created_at DESC
  ),
  extracted AS (
    SELECT
      l.*,
      (
        SELECT asset->>'url'
        FROM jsonb_array_elements(COALESCE(l.raw_data #> '{raw,assets}', '[]'::jsonb)) AS asset
        WHERE asset->>'type' = 'primary'
        LIMIT 1
      ) AS aldi_primary_image,
      COALESCE(
        l.raw_data #>> '{raw,detail,images,0,image_url}',
        l.raw_data #>> '{raw,list,productInformation,image}'
      ) AS dirk_image_path,
      -- AH only exposes a comparative price as free text, e.g. "prijs per kg EUR 6.86".
      COALESCE(
        l.raw_data #>> '{raw,card,unitPriceDescription}',
        l.raw_data #>> '{raw,detail,productCard,unitPriceDescription}'
      ) AS ah_unitprice_text,
      -- Aldi: derive a comparable qty from the pack-size string (native base price is corrupt).
      (catalog.silver_qty_base(l.raw_data #>> '{raw,salesUnit}')).qty  AS aldi_qty,
      (catalog.silver_qty_base(l.raw_data #>> '{raw,salesUnit}')).unit AS aldi_base_unit
    FROM latest l
  ),
  mapped AS (
    SELECT
      e.bronze_product_id,
      e.store,
      e.external_id,
      catalog.silver_clean_text(
        CASE e.store
          WHEN 'ah' THEN COALESCE(
            e.raw_data #>> '{raw,card,title}',
            e.raw_data #>> '{raw,detail,productCard,title}'
          )
          WHEN 'jumbo' THEN COALESCE(
            e.raw_data #>> '{raw,detail,title}',
            e.raw_data #>> '{raw,listing,title}'
          )
          WHEN 'dirk' THEN COALESCE(
            e.raw_data #>> '{raw,detail,headerText}',
            e.raw_data #>> '{raw,list,productInformation,headerText}'
          )
          WHEN 'plus' THEN COALESCE(
            e.raw_data #>> '{raw,pdp,Name}',
            e.raw_data #>> '{raw,plp,Name}'
          )
          WHEN 'spar' THEN e.raw_data #>> '{raw,product_name}'
          WHEN 'aldi' THEN COALESCE(
            e.raw_data #>> '{raw,detail,name}',
            e.raw_data #>> '{raw,name}'
          )
        END
      ) AS name,
      catalog.silver_clean_text(
        CASE e.store
          WHEN 'ah' THEN COALESCE(
            e.raw_data #>> '{raw,detail,tradeItem,gtin}',
            e.raw_data #>> '{raw,card,gtin}'
          )
          WHEN 'jumbo' THEN e.raw_data #>> '{raw,detail,ean}'
          WHEN 'dirk' THEN e.raw_data #>> '{raw,detail,barcode}'
          WHEN 'plus' THEN COALESCE(
            e.raw_data #>> '{raw,pdp,EAN}',
            e.raw_data #>> '{raw,pdp,Medicine,EAN}',
            e.raw_data #>> '{raw,plp,EAN}'
          )
          WHEN 'spar' THEN e.raw_data #>> '{raw,gtin13}'
          WHEN 'aldi' THEN COALESCE(
            e.raw_data #>> '{raw,detail,ean}',
            e.raw_data #>> '{raw,ean}',
            e.raw_data #>> '{raw,detail,gtin}',
            e.raw_data #>> '{raw,gtin}'
          )
        END
      ) AS ean,
      CASE e.store
        WHEN 'ah' THEN COALESCE(
          catalog.silver_to_numeric(e.raw_data #>> '{raw,card,currentPrice}'),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,detail,productCard,currentPrice}'),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,card,priceBeforeBonus}'),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,detail,productCard,priceBeforeBonus}')
        )
        WHEN 'jumbo' THEN COALESCE(
          catalog.silver_price_from_cents(e.raw_data #>> '{raw,detail,price,promoPrice}'),
          catalog.silver_price_from_cents(e.raw_data #>> '{raw,detail,price,price}'),
          catalog.silver_price_from_cents(e.raw_data #>> '{raw,listing,price,promoPrice}'),
          catalog.silver_price_from_cents(e.raw_data #>> '{raw,listing,price,price}')
        )
        WHEN 'dirk' THEN COALESCE(
          NULLIF(catalog.silver_to_numeric(e.raw_data #>> '{raw,list,offerPrice}'), 0),
          NULLIF(catalog.silver_to_numeric(e.raw_data #>> '{raw,detail,productAssortment,offerPrice}'), 0),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,list,normalPrice}'),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,detail,productAssortment,normalPrice}')
        )
        WHEN 'plus' THEN COALESCE(
          NULLIF(catalog.silver_to_numeric(e.raw_data #>> '{raw,pdp,NewPrice}'), 0),
          NULLIF(catalog.silver_to_numeric(e.raw_data #>> '{raw,plp,NewPrice}'), 0),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,pdp,OriginalPrice}'),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,plp,OriginalPrice}')
        )
        WHEN 'spar' THEN COALESCE(
          catalog.silver_to_numeric(e.raw_data #>> '{raw,price_visible}'),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,price_jsonld}'),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,price_data_layer}')
        )
        -- FIX: Aldi price lives at currentPrice.priceValue (euros), not detail.price.
        WHEN 'aldi' THEN COALESCE(
          catalog.silver_to_numeric(e.raw_data #>> '{raw,currentPrice,priceValue}'),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,detail,currentPrice,priceValue}')
        )
      END AS price,
      -- NEW: base / unit (comparative) price in euros per unit.
      CASE e.store
        WHEN 'ah' THEN catalog.silver_to_numeric(
          substring(e.ah_unitprice_text from '([0-9]+(?:[.,][0-9]+)?)\s*$')
        )
        WHEN 'jumbo' THEN COALESCE(
          catalog.silver_price_from_cents(e.raw_data #>> '{raw,detail,price,pricePerUnit,price}'),
          catalog.silver_price_from_cents(e.raw_data #>> '{raw,listing,price,pricePerUnit,price}')
        )
        -- Aldi native basePrice is corrupt; derive from reliable price / pack-size qty.
        WHEN 'aldi' THEN CASE
          WHEN e.aldi_qty IS NOT NULL AND e.aldi_qty > 0 THEN round(
            COALESCE(
              catalog.silver_to_numeric(e.raw_data #>> '{raw,currentPrice,priceValue}'),
              catalog.silver_to_numeric(e.raw_data #>> '{raw,detail,currentPrice,priceValue}')
            ) / e.aldi_qty, 2)
          ELSE NULL
        END
        ELSE NULL  -- dirk, plus, spar: no native base price in the source
      END AS base_price,
      -- NEW: the unit the base price is per (kg / l / stuk / 100 g / ...).
      catalog.silver_norm_unit(
        CASE e.store
          WHEN 'ah' THEN substring(e.ah_unitprice_text from 'per\s+([0-9]*\s*[a-zA-Z]+)')
          WHEN 'jumbo' THEN COALESCE(
            e.raw_data #>> '{raw,detail,price,pricePerUnit,unit}',
            e.raw_data #>> '{raw,listing,price,pricePerUnit,unit}'
          )
          WHEN 'aldi' THEN e.aldi_base_unit
          ELSE NULL
        END
      ) AS base_price_unit,
      catalog.silver_clean_text(
        CASE e.store
          WHEN 'ah' THEN e.raw_data #>> '{raw,card,images,0,url}'
          WHEN 'jumbo' THEN COALESCE(
            e.raw_data #>> '{raw,detail,image}',
            e.raw_data #>> '{raw,listing,image}'
          )
          WHEN 'dirk' THEN CASE
            WHEN e.dirk_image_path IS NULL THEN NULL
            WHEN e.dirk_image_path LIKE 'http%' THEN e.dirk_image_path
            ELSE 'https://web-fileserver.dirk.nl/' || replace(ltrim(e.dirk_image_path, '/'), E'\\', '/')
          END
          WHEN 'plus' THEN COALESCE(
            e.raw_data #>> '{raw,pdp,ImageURL}',
            e.raw_data #>> '{raw,plp,ImageURL}'
          )
          WHEN 'spar' THEN e.raw_data #>> '{raw,images,0}'
          WHEN 'aldi' THEN COALESCE(
            e.aldi_primary_image,
            e.raw_data #>> '{raw,detail,assets,0,url}',
            e.raw_data #>> '{raw,assets,0,url}'
          )
        END
      ) AS image_url,
      e.scraped_at
    FROM extracted e
  ),
  upserted AS (
    INSERT INTO catalog.silver_products (
      bronze_product_id,
      store,
      external_id,
      name,
      ean,
      price,
      base_price,
      base_price_unit,
      image_url,
      first_seen_at,
      last_seen_at,
      updated_at
    )
    SELECT
      m.bronze_product_id,
      m.store,
      m.external_id,
      m.name,
      m.ean,
      m.price,
      m.base_price,
      m.base_price_unit,
      m.image_url,
      m.scraped_at,
      m.scraped_at,
      now()
    FROM mapped m
    ON CONFLICT (store, external_id) DO UPDATE SET
      bronze_product_id = EXCLUDED.bronze_product_id,
      name = EXCLUDED.name,
      ean = EXCLUDED.ean,
      price = EXCLUDED.price,
      base_price = EXCLUDED.base_price,
      base_price_unit = EXCLUDED.base_price_unit,
      image_url = EXCLUDED.image_url,
      last_seen_at = EXCLUDED.last_seen_at,
      updated_at = now()
    RETURNING catalog.silver_products.store
  )
  SELECT u.store, count(*)::integer
  FROM upserted u
  GROUP BY u.store
  ORDER BY u.store;
END;
$$;

GRANT EXECUTE ON FUNCTION catalog.silver_norm_unit(text) TO service_role;
GRANT EXECUTE ON FUNCTION catalog.silver_qty_base(text) TO service_role;
GRANT EXECUTE ON FUNCTION catalog.refresh_silver_products(text) TO service_role;
