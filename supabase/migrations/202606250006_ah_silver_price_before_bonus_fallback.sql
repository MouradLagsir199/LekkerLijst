-- AH sometimes has no currentPrice but does have priceBeforeBonus in bronze.
-- Keep currentPrice preferred, then fall back to priceBeforeBonus.

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
      ) AS dirk_image_path
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
        WHEN 'aldi' THEN COALESCE(
          catalog.silver_to_numeric(e.raw_data #>> '{raw,detail,price}'),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,price}'),
          catalog.silver_to_numeric(e.raw_data #>> '{raw,salesPrice}')
        )
      END AS price,
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
