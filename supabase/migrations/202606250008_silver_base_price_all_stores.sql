-- Extend base_price coverage to Dirk, Plus and Spar.
--
-- These three APIs expose no native unit price, but each has a clean pack-size
-- string with 100% coverage, so we derive base_price = consumer price / pack qty
-- (the same approach already used for Aldi):
--   dirk : raw.detail.packaging       ("454 g", "5 liter", "Los. 1 kg")
--   plus : raw.plp.Product_Subtitle   ("Per 395 g", "Per 55 ml", "Per 16 st")
--   spar : raw.package                ("898 Gram", "553 Milliliter", "4 Kilogram")
-- Per-piece units ("5 stuks", "Per 16 st") yield no weight/volume, so base_price
-- stays NULL for those (correct — there is no comparable per-kg/l price).
--
-- AH (text unitPriceDescription) and Jumbo (native pricePerUnit) keep their own
-- extraction. silver_qty_base is widened to understand full-word units
-- (Gram/Kilogram/Milliliter/Centiliter/Liter) that SPAR uses.

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
  -- volume first; longest tokens first so "l" cannot match inside "liter"/"ml".
  m := regexp_match(s, '([0-9]+(?:[.,][0-9]+)?)\s*(milliliter|millilitre|centiliter|centilitre|deciliter|decilitre|liter|litre|ml|cl|dl|l)(?![a-z])');
  IF m IS NOT NULL THEN
    qty := replace(m[1], ',', '.')::numeric * mult
           * CASE m[2]
               WHEN 'ml' THEN 0.001 WHEN 'milliliter' THEN 0.001 WHEN 'millilitre' THEN 0.001
               WHEN 'cl' THEN 0.01  WHEN 'centiliter' THEN 0.01  WHEN 'centilitre' THEN 0.01
               WHEN 'dl' THEN 0.1   WHEN 'deciliter' THEN 0.1    WHEN 'decilitre' THEN 0.1
               ELSE 1  -- l / liter / litre
             END;
    unit := 'l';
    IF qty > 0 THEN RETURN; END IF;
  END IF;
  -- weight
  m := regexp_match(s, '([0-9]+(?:[.,][0-9]+)?)\s*(kilogram|gram|kg|mg|g)(?![a-z])');
  IF m IS NOT NULL THEN
    qty := replace(m[1], ',', '.')::numeric * mult
           * CASE m[2]
               WHEN 'g' THEN 0.001 WHEN 'gram' THEN 0.001
               WHEN 'mg' THEN 0.000001
               ELSE 1  -- kg / kilogram
             END;
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
      COALESCE(
        l.raw_data #>> '{raw,card,unitPriceDescription}',
        l.raw_data #>> '{raw,detail,productCard,unitPriceDescription}'
      ) AS ah_unitprice_text,
      -- comparable pack quantity (kg/l) parsed from each store's pack-size string
      (catalog.silver_qty_base(
        CASE l.store
          WHEN 'aldi' THEN l.raw_data #>> '{raw,salesUnit}'
          WHEN 'dirk' THEN l.raw_data #>> '{raw,detail,packaging}'
          WHEN 'plus' THEN l.raw_data #>> '{raw,plp,Product_Subtitle}'
          WHEN 'spar' THEN l.raw_data #>> '{raw,package}'
        END
      )).qty AS pack_qty,
      (catalog.silver_qty_base(
        CASE l.store
          WHEN 'aldi' THEN l.raw_data #>> '{raw,salesUnit}'
          WHEN 'dirk' THEN l.raw_data #>> '{raw,detail,packaging}'
          WHEN 'plus' THEN l.raw_data #>> '{raw,plp,Product_Subtitle}'
          WHEN 'spar' THEN l.raw_data #>> '{raw,package}'
        END
      )).unit AS pack_unit
    FROM latest l
  ),
  -- compute the consumer price once so base_price can reference it
  priced AS (
    SELECT
      e.*,
      catalog.silver_clean_text(
        CASE e.store
          WHEN 'ah' THEN COALESCE(e.raw_data #>> '{raw,card,title}', e.raw_data #>> '{raw,detail,productCard,title}')
          WHEN 'jumbo' THEN COALESCE(e.raw_data #>> '{raw,detail,title}', e.raw_data #>> '{raw,listing,title}')
          WHEN 'dirk' THEN COALESCE(e.raw_data #>> '{raw,detail,headerText}', e.raw_data #>> '{raw,list,productInformation,headerText}')
          WHEN 'plus' THEN COALESCE(e.raw_data #>> '{raw,pdp,Name}', e.raw_data #>> '{raw,plp,Name}')
          WHEN 'spar' THEN e.raw_data #>> '{raw,product_name}'
          WHEN 'aldi' THEN COALESCE(e.raw_data #>> '{raw,detail,name}', e.raw_data #>> '{raw,name}')
        END
      ) AS name,
      catalog.silver_clean_text(
        CASE e.store
          WHEN 'ah' THEN COALESCE(e.raw_data #>> '{raw,detail,tradeItem,gtin}', e.raw_data #>> '{raw,card,gtin}')
          WHEN 'jumbo' THEN e.raw_data #>> '{raw,detail,ean}'
          WHEN 'dirk' THEN e.raw_data #>> '{raw,detail,barcode}'
          WHEN 'plus' THEN COALESCE(e.raw_data #>> '{raw,pdp,EAN}', e.raw_data #>> '{raw,pdp,Medicine,EAN}', e.raw_data #>> '{raw,plp,EAN}')
          WHEN 'spar' THEN e.raw_data #>> '{raw,gtin13}'
          WHEN 'aldi' THEN COALESCE(e.raw_data #>> '{raw,detail,ean}', e.raw_data #>> '{raw,ean}', e.raw_data #>> '{raw,detail,gtin}', e.raw_data #>> '{raw,gtin}')
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
        WHEN 'aldi' THEN COALESCE(
          catalog.silver_to_numeric(e.raw_data #>> '{raw,currentPrice,priceValue}'),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,detail,currentPrice,priceValue}')
        )
      END AS price,
      catalog.silver_clean_text(
        CASE e.store
          WHEN 'ah' THEN e.raw_data #>> '{raw,card,images,0,url}'
          WHEN 'jumbo' THEN COALESCE(e.raw_data #>> '{raw,detail,image}', e.raw_data #>> '{raw,listing,image}')
          WHEN 'dirk' THEN CASE
            WHEN e.dirk_image_path IS NULL THEN NULL
            WHEN e.dirk_image_path LIKE 'http%' THEN e.dirk_image_path
            ELSE 'https://web-fileserver.dirk.nl/' || replace(ltrim(e.dirk_image_path, '/'), E'\\', '/')
          END
          WHEN 'plus' THEN COALESCE(e.raw_data #>> '{raw,pdp,ImageURL}', e.raw_data #>> '{raw,plp,ImageURL}')
          WHEN 'spar' THEN e.raw_data #>> '{raw,images,0}'
          WHEN 'aldi' THEN COALESCE(e.aldi_primary_image, e.raw_data #>> '{raw,detail,assets,0,url}', e.raw_data #>> '{raw,assets,0,url}')
        END
      ) AS image_url
    FROM extracted e
  ),
  mapped AS (
    SELECT
      p.bronze_product_id,
      p.store,
      p.external_id,
      p.name,
      p.ean,
      p.price,
      -- base / unit price: AH from text, Jumbo native cents, the rest derived price/qty
      CASE p.store
        WHEN 'ah' THEN catalog.silver_to_numeric(
          substring(p.ah_unitprice_text from '([0-9]+(?:[.,][0-9]+)?)\s*$')
        )
        WHEN 'jumbo' THEN COALESCE(
          catalog.silver_price_from_cents(p.raw_data #>> '{raw,detail,price,pricePerUnit,price}'),
          catalog.silver_price_from_cents(p.raw_data #>> '{raw,listing,price,pricePerUnit,price}')
        )
        ELSE  -- dirk, plus, spar, aldi: consumer price / parsed pack quantity
          CASE WHEN p.pack_qty IS NOT NULL AND p.pack_qty > 0 AND p.price IS NOT NULL
               THEN round(p.price / p.pack_qty, 2)
               ELSE NULL END
      END AS base_price,
      catalog.silver_norm_unit(
        CASE p.store
          WHEN 'ah' THEN substring(p.ah_unitprice_text from 'per\s+([0-9]*\s*[a-zA-Z]+)')
          WHEN 'jumbo' THEN COALESCE(
            p.raw_data #>> '{raw,detail,price,pricePerUnit,unit}',
            p.raw_data #>> '{raw,listing,price,pricePerUnit,unit}'
          )
          ELSE p.pack_unit
        END
      ) AS base_price_unit,
      p.image_url,
      p.scraped_at
    FROM priced p
  ),
  upserted AS (
    INSERT INTO catalog.silver_products (
      bronze_product_id, store, external_id, name, ean, price,
      base_price, base_price_unit, image_url, first_seen_at, last_seen_at, updated_at
    )
    SELECT
      m.bronze_product_id, m.store, m.external_id, m.name, m.ean, m.price,
      m.base_price, m.base_price_unit, m.image_url, m.scraped_at, m.scraped_at, now()
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

GRANT EXECUTE ON FUNCTION catalog.silver_qty_base(text) TO service_role;
GRANT EXECUTE ON FUNCTION catalog.refresh_silver_products(text) TO service_role;
