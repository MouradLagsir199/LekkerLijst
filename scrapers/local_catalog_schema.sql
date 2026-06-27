-- Local catalog build bootstrap.
--
-- Use this on a local Postgres database, not on hosted Supabase:
--
--   createdb lekkerlijst_catalog
--   psql "$LOCAL_CATALOG_DB_URL" -f scrapers/local_catalog_schema.sql
--
-- It creates only the catalog build surface plus the app-facing product/search
-- objects needed to verify locally. There are no Supabase auth/RLS grants.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS catalog;

-- ---------------------------------------------------------------------------
-- Public app-facing helpers

CREATE OR REPLACE FUNCTION public.fold_text(txt text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
  SELECT translate(
    lower(coalesce(txt, '')),
    'áàâäãåéèêëíìîïóòôöõúùûüýÿçñ',
    'aaaaaaeeeeiiiiooooouuuuyycn'
  );
$$;

CREATE OR REPLACE FUNCTION public.normalize_ean(raw text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  WITH d AS (
    SELECT regexp_replace(coalesce(raw, ''), '\D', '', 'g') AS digits
  )
  SELECT CASE
    WHEN length(d.digits) BETWEEN 8 AND 14
     AND d.digits !~ '^0+$'
    THEN lpad(d.digits, 14, '0')
    ELSE NULL
  END
  FROM d;
$$;

CREATE OR REPLACE FUNCTION public.tok_match(q text, pw text)
RETURNS numeric
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE
    WHEN q = '' OR pw = '' THEN 0.0
    WHEN pw = q THEN 1.00
    WHEN pw LIKE q || '%'
         AND substr(pw, length(q) + 1) IN
             ('s','e','n','en','er','tje','tjes','je','jes','eren','ren') THEN 0.93
    WHEN length(q) >= 4 AND pw LIKE '%' || q THEN 0.88
    WHEN length(q) >= 5 AND pw LIKE '%' || q || '%' THEN 0.42
    WHEN length(q) >= 3 AND pw LIKE q || '%' THEN 0.34
    WHEN strict_word_similarity(q, pw) >= 0.6 THEN 0.72
    ELSE 0.0
  END;
$$;

-- ---------------------------------------------------------------------------
-- Catalog staging tables

CREATE TABLE IF NOT EXISTS catalog.scrape_runs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  store         text NOT NULL CHECK (store IN ('ah','jumbo','dirk','plus','spar','aldi')),
  status        text NOT NULL DEFAULT 'running',
  row_count     integer,
  error_message text,
  started_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz
);

CREATE TABLE IF NOT EXISTS catalog.bronze_products (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scrape_run_id uuid REFERENCES catalog.scrape_runs(id) ON DELETE SET NULL,
  store         text NOT NULL CHECK (store IN ('ah','jumbo','dirk','plus','spar','aldi')),
  raw_data      jsonb NOT NULL,
  row_hash      text NOT NULL UNIQUE,
  scraped_at    timestamptz NOT NULL DEFAULT now(),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS bronze_products_store_idx
  ON catalog.bronze_products (store);

CREATE TABLE IF NOT EXISTS catalog.silver_products (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bronze_product_id uuid,
  store             text NOT NULL CHECK (store IN ('ah','jumbo','dirk','plus','spar','aldi')),
  external_id       text NOT NULL,
  name              text,
  ean               text,
  price             numeric(12, 2),
  base_price        numeric(12, 2),
  base_price_unit   text,
  image_url         text,
  first_seen_at     timestamptz NOT NULL DEFAULT now(),
  last_seen_at      timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (store, external_id)
);

CREATE INDEX IF NOT EXISTS silver_products_store_idx
  ON catalog.silver_products (store);

CREATE INDEX IF NOT EXISTS silver_products_ean_idx
  ON catalog.silver_products (ean)
  WHERE ean IS NOT NULL;

CREATE TABLE IF NOT EXISTS catalog.name_canonical (
  name_search   text PRIMARY KEY,
  canonical_key text NOT NULL,
  display_name  text,
  category      text,
  is_organic    boolean,
  unit_type     text,
  confidence    numeric,
  source        text NOT NULL DEFAULT 'rule' CHECK (source IN ('rule','ai_batch','manual')),
  model         text,
  tagged_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS name_canonical_key_idx
  ON catalog.name_canonical (canonical_key);

-- ---------------------------------------------------------------------------
-- App-facing products table, decoupled from catalog FKs.

CREATE TABLE IF NOT EXISTS public.products (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  store_id            text NOT NULL,
  silver_product_id   uuid,
  external_id         text NOT NULL,
  name                text NOT NULL,
  brand               text,
  category            text,
  subcategory         text,
  package_size_text   text,
  current_price_cents integer,
  unit_price_cents    integer,
  unit_price_unit     text,
  is_available        boolean NOT NULL DEFAULT true,
  image_url           text,
  product_url         text,
  synced_at           timestamptz NOT NULL DEFAULT now(),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  ean                 text,
  ean_norm            text GENERATED ALWAYS AS (public.normalize_ean(ean)) STORED,
  name_search         text GENERATED ALWAYS AS (public.fold_text(name)) STORED,
  canonical_key       text,
  canonical_name      text,
  is_organic          boolean,
  UNIQUE (store_id, external_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS products_silver_product_id_idx
  ON public.products (silver_product_id)
  WHERE silver_product_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS products_store_id_idx
  ON public.products (store_id);

CREATE INDEX IF NOT EXISTS products_ean_norm_idx
  ON public.products (ean_norm)
  WHERE ean_norm IS NOT NULL;

CREATE INDEX IF NOT EXISTS products_name_search_trgm_idx
  ON public.products USING gin (name_search gin_trgm_ops);

CREATE INDEX IF NOT EXISTS products_canonical_key_idx
  ON public.products (canonical_key)
  WHERE canonical_key IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Silver extraction helpers and refresh.

CREATE OR REPLACE FUNCTION catalog.silver_clean_text(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT NULLIF(btrim(value), '')
$$;

CREATE OR REPLACE FUNCTION catalog.silver_to_numeric(value text)
RETURNS numeric
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  cleaned text;
BEGIN
  value := catalog.silver_clean_text(value);
  IF value IS NULL THEN
    RETURN NULL;
  END IF;

  cleaned := regexp_replace(value, '[^0-9,.-]', '', 'g');
  cleaned := replace(cleaned, ',', '.');
  IF cleaned IS NULL OR cleaned = '' OR cleaned IN ('-', '.', '-.') THEN
    RETURN NULL;
  END IF;

  RETURN cleaned::numeric;
EXCEPTION
  WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION catalog.silver_price_from_cents(value text)
RETURNS numeric
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT round(catalog.silver_to_numeric(value) / 100, 2)
$$;

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

  mm := regexp_match(s, '([0-9]+)\s*[x]\s*');
  IF mm IS NOT NULL THEN
    mult := mm[1]::numeric;
  END IF;

  m := regexp_match(s, '([0-9]+(?:[.,][0-9]+)?)\s*(milliliter|millilitre|centiliter|centilitre|deciliter|decilitre|liter|litre|ml|cl|dl|l)(?![a-z])');
  IF m IS NOT NULL THEN
    qty := replace(m[1], ',', '.')::numeric * mult
           * CASE m[2]
               WHEN 'ml' THEN 0.001 WHEN 'milliliter' THEN 0.001 WHEN 'millilitre' THEN 0.001
               WHEN 'cl' THEN 0.01  WHEN 'centiliter' THEN 0.01  WHEN 'centilitre' THEN 0.01
               WHEN 'dl' THEN 0.1   WHEN 'deciliter' THEN 0.1    WHEN 'decilitre' THEN 0.1
               ELSE 1
             END;
    unit := 'l';
    IF qty > 0 THEN RETURN; END IF;
  END IF;

  m := regexp_match(s, '([0-9]+(?:[.,][0-9]+)?)\s*(kilogram|gram|kg|mg|g)(?![a-z])');
  IF m IS NOT NULL THEN
    qty := replace(m[1], ',', '.')::numeric * mult
           * CASE m[2]
               WHEN 'g' THEN 0.001 WHEN 'gram' THEN 0.001
               WHEN 'mg' THEN 0.000001
               ELSE 1
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
      CASE p.store
        WHEN 'ah' THEN catalog.silver_to_numeric(
          substring(p.ah_unitprice_text from '([0-9]+(?:[.,][0-9]+)?)\s*$')
        )
        WHEN 'jumbo' THEN COALESCE(
          catalog.silver_price_from_cents(p.raw_data #>> '{raw,detail,price,pricePerUnit,price}'),
          catalog.silver_price_from_cents(p.raw_data #>> '{raw,listing,price,pricePerUnit,price}')
        )
        ELSE CASE WHEN p.pack_qty IS NOT NULL AND p.pack_qty > 0 AND p.price IS NOT NULL
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

-- ---------------------------------------------------------------------------
-- Promotion and canonical grouping.

CREATE OR REPLACE FUNCTION catalog.promote_to_public(store_filter text DEFAULT NULL)
RETURNS TABLE(store_id text, rows_upserted integer)
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  WITH upserted AS (
    INSERT INTO public.products (
      store_id,
      silver_product_id,
      external_id,
      name,
      ean,
      current_price_cents,
      unit_price_cents,
      unit_price_unit,
      is_available,
      image_url,
      synced_at
    )
    SELECT
      sp.store,
      sp.id,
      sp.external_id,
      sp.name,
      sp.ean,
      CASE WHEN sp.price IS NOT NULL THEN round(sp.price * 100)::int END,
      CASE WHEN sp.base_price IS NOT NULL THEN round(sp.base_price * 100)::int END,
      sp.base_price_unit,
      true,
      sp.image_url,
      now()
    FROM catalog.silver_products sp
    WHERE sp.name IS NOT NULL
      AND (store_filter IS NULL OR sp.store = store_filter)
    ON CONFLICT ON CONSTRAINT products_store_id_external_id_key DO UPDATE SET
      silver_product_id     = EXCLUDED.silver_product_id,
      name                  = EXCLUDED.name,
      ean                   = EXCLUDED.ean,
      current_price_cents   = EXCLUDED.current_price_cents,
      unit_price_cents      = EXCLUDED.unit_price_cents,
      unit_price_unit       = EXCLUDED.unit_price_unit,
      is_available          = EXCLUDED.is_available,
      image_url             = EXCLUDED.image_url,
      synced_at             = EXCLUDED.synced_at,
      updated_at            = now()
    RETURNING public.products.store_id
  )
  SELECT u.store_id, count(*)::integer
  FROM upserted u
  GROUP BY u.store_id
  ORDER BY u.store_id;
END;
$$;

CREATE OR REPLACE FUNCTION catalog.canonical_key(txt text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  WITH folded AS (
    SELECT regexp_replace(public.fold_text(coalesce(txt, '')), '[^[:alnum:] ]+', ' ', 'g') AS s
  ),
  toks AS (
    SELECT w
    FROM folded, unnest(string_to_array(btrim(folded.s), ' ')) AS w
    WHERE w <> ''
      AND w NOT IN ('ah','jumbo','plus','spar','aldi','dirk','hema','gwoon','woon')
      AND w NOT IN ('g','gr','gram','ml','cl','dl','kg','l','ltr','liter','lt',
                    'st','stuks','stuk','stks','stk','pak','pakje','pakken','zak',
                    'zakje','fles','flesje','blik','blikje','pot','potje','bos',
                    'bosje','tray','rol','rolletje','plak','plakken','plakjes',
                    'doos','set','x','per','ca','stuk(s)')
      AND w NOT IN ('vers','verse','voordeel','value','aanbieding','actie','nieuw',
                    'original','origineel','de','het','een','van','met','en','of',
                    'voordeelverpakking','grootverpakking')
      AND w !~ '^[0-9]+$'
      AND w !~ '^[0-9]+(g|gr|gram|ml|cl|dl|kg|l|ltr|liter|st|stuks?|x|pack)$'
      AND w !~ '^[0-9]+x[0-9]+'
  )
  SELECT coalesce(string_agg(w, ' ' ORDER BY w), '') FROM toks;
$$;

CREATE OR REPLACE FUNCTION catalog.name_is_organic(txt text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT public.fold_text(coalesce(txt, '')) ~ '\m(bio|biologisch|biologische|organic)\M';
$$;

CREATE OR REPLACE FUNCTION catalog.refresh_canonical_baseline()
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
  n bigint;
BEGIN
  INSERT INTO catalog.name_canonical (name_search, canonical_key, is_organic, source)
  SELECT DISTINCT
    p.name_search,
    catalog.canonical_key(p.name_search),
    catalog.name_is_organic(p.name_search),
    'rule'
  FROM public.products p
  WHERE p.is_available = true
    AND p.name_search IS NOT NULL
    AND p.name_search <> ''
  ON CONFLICT (name_search) DO UPDATE
    SET canonical_key = EXCLUDED.canonical_key,
        is_organic    = EXCLUDED.is_organic,
        tagged_at     = now()
    WHERE catalog.name_canonical.source = 'rule';

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

CREATE OR REPLACE FUNCTION catalog.apply_canonical_keys()
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
  n bigint;
BEGIN
  UPDATE public.products p
  SET canonical_key  = nc.canonical_key,
      canonical_name = nc.display_name,
      is_organic     = nc.is_organic
  FROM catalog.name_canonical nc
  WHERE nc.name_search = p.name_search
    AND (p.canonical_key  IS DISTINCT FROM nc.canonical_key
      OR p.canonical_name IS DISTINCT FROM nc.display_name
      OR p.is_organic     IS DISTINCT FROM nc.is_organic);

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

CREATE OR REPLACE FUNCTION catalog.apply_canonical_prepass()
RETURNS TABLE(rule text, rows_updated bigint)
LANGUAGE plpgsql
AS $$
DECLARE
  n bigint;
BEGIN
  WITH remap AS (
    SELECT old.canonical_key AS old_key,
           regexp_replace(old.canonical_key, '_[0-9]+_plus$', '') AS new_key
    FROM catalog.name_canonical old
    WHERE old.canonical_key ~ '_[0-9]+_plus$'
      AND EXISTS (
        SELECT 1
        FROM catalog.name_canonical sibling
        WHERE sibling.canonical_key = regexp_replace(old.canonical_key, '_[0-9]+_plus$', '')
      )
  )
  UPDATE catalog.name_canonical nc
  SET canonical_key = r.new_key,
      tagged_at = now()
  FROM remap r
  WHERE nc.canonical_key = r.old_key;

  GET DIAGNOSTICS n = ROW_COUNT;
  rule := 'strip_trailing_fat_percent_plus_when_sibling_exists';
  rows_updated := n;
  RETURN NEXT;
END;
$$;

-- ---------------------------------------------------------------------------
-- App-facing search and offer surfaces.

CREATE OR REPLACE FUNCTION public.search_products(
  query_text   text,
  store_filter text    DEFAULT NULL,
  match_count  integer DEFAULT 8
)
RETURNS TABLE (
  product_id          uuid,
  store_id            text,
  product_name        text,
  brand               text,
  category            text,
  package_size_text   text,
  current_price_cents integer,
  image_url           text,
  product_url         text,
  match_score         numeric
)
LANGUAGE sql
STABLE
AS $$
  WITH params AS (
    SELECT
      public.fold_text(trim(coalesce(query_text, ''))) AS q,
      btrim(regexp_replace(public.fold_text(coalesce(query_text, '')), '[^[:alnum:] ]+', ' ', 'g')) AS qre
  ),
  stop AS (
    SELECT unnest(ARRAY[
      'de','het','een','van','met','en','of','in','op','voor','aan','per','ca',
      'ah','jumbo','plus','spar','aldi','dirk','hema',
      'g','gr','gram','ml','cl','dl','kg','st','stuks','stuk','pak','pakje',
      'zak','fles','blik','pot','liter','stk',
      'vers','verse','biologisch','bio','naturel','ongezouten','gezouten',
      'light','zero','mild','milde','fijn','fijne','grof','grove','klein',
      'kleine','groot','grote','mini','jong','jonge','oud','oude','belegen',
      'volle','halfvolle','magere','mager','rood','rode','groen','groene',
      'geel','gele','wit','witte','bruin','bruine','zoet','zoete','zuur','zure',
      'gerookt','gerookte','gesneden','geraspte','gesnipperd','gewassen',
      'voordeel','original','origineel','heel','hele'
    ]::text[]) AS word
  ),
  qtok AS (
    SELECT word
    FROM unnest(string_to_array((SELECT qre FROM params), ' ')) AS word
    WHERE length(word) >= 2
      AND word NOT IN (SELECT word FROM stop)
  ),
  qtok_eff AS (
    SELECT word FROM qtok
    UNION ALL
    SELECT (SELECT qre FROM params)
    WHERE NOT EXISTS (SELECT 1 FROM qtok) AND (SELECT qre FROM params) <> ''
  ),
  cand_ids AS (
    SELECT p.id
    FROM public.products p
    JOIN qtok_eff t ON t.word <% p.name_search
    WHERE p.is_available = true
      AND (store_filter IS NULL OR p.store_id = store_filter)
    UNION
    SELECT p.id
    FROM public.products p
    JOIN qtok_eff t ON p.name_search LIKE '%' || t.word || '%'
    WHERE p.is_available = true
      AND (store_filter IS NULL OR p.store_id = store_filter)
      AND length(t.word) >= 3
  ),
  prelim AS (
    SELECT p.id, p.store_id, p.name, p.brand, p.category, p.package_size_text,
           p.current_price_cents, p.image_url, p.product_url, p.name_search AS n
    FROM public.products p
    JOIN cand_ids ci ON ci.id = p.id
    ORDER BY word_similarity((SELECT qre FROM params), p.name_search) DESC NULLS LAST
    LIMIT 160
  ),
  scored AS (
    SELECT
      c.*,
      (SELECT coalesce(avg(best), 0) FROM (
         SELECT max(public.tok_match(qt.word, pw.word)) AS best
         FROM qtok_eff qt
         CROSS JOIN unnest(string_to_array(regexp_replace(c.n, '[^[:alnum:] ]+', ' ', 'g'), ' ')) AS pw(word)
         WHERE length(pw.word) >= 2 AND pw.word !~ '^[0-9]+$'
         GROUP BY qt.word
       ) cov) AS coverage,
      (SELECT CASE WHEN count(*) = 0 THEN 0 ELSE coalesce(avg(best), 0) END FROM (
         SELECT max(public.tok_match(qt.word, pw.word)) AS best
         FROM (
           SELECT w AS word
           FROM unnest(string_to_array(regexp_replace(c.n, '[^[:alnum:] ]+', ' ', 'g'), ' ')) AS w
           WHERE length(w) >= 2 AND w !~ '^[0-9]+$'
             AND w NOT IN (SELECT word FROM stop)
         ) pw
         CROSS JOIN qtok_eff qt
         GROUP BY pw.word
       ) sp) AS specificity
    FROM prelim c
  )
  SELECT
    s.id, s.store_id, s.name, s.brand, s.category, s.package_size_text,
    s.current_price_cents, s.image_url, s.product_url,
    least(1.0,
      0.55 * s.coverage
      + 0.45 * s.specificity
      + 0.05 * (length((SELECT qre FROM params))::numeric / greatest(length(s.n), 1))
    )::numeric AS match_score
  FROM scored s
  WHERE s.coverage > 0
  ORDER BY match_score DESC,
           length(s.name) ASC,
           s.current_price_cents ASC NULLS LAST,
           s.name ASC
  LIMIT least(greatest(match_count, 1), 20);
$$;

CREATE OR REPLACE VIEW public.product_price_matrix AS
SELECT
  p.ean_norm AS ean,
  min(p.name) AS product,
  max(p.current_price_cents) FILTER (WHERE p.store_id = 'ah') AS ah_cents,
  max(p.current_price_cents) FILTER (WHERE p.store_id = 'jumbo') AS jumbo_cents,
  max(p.current_price_cents) FILTER (WHERE p.store_id = 'dirk') AS dirk_cents,
  max(p.current_price_cents) FILTER (WHERE p.store_id = 'plus') AS plus_cents,
  max(p.current_price_cents) FILTER (WHERE p.store_id = 'spar') AS spar_cents,
  max(p.current_price_cents) FILTER (WHERE p.store_id = 'aldi') AS aldi_cents,
  count(DISTINCT p.store_id) AS store_count
FROM public.products p
WHERE p.ean_norm IS NOT NULL
  AND p.is_available = true
GROUP BY p.ean_norm;

CREATE OR REPLACE VIEW public.ingredient_price_matrix AS
SELECT
  p.canonical_key,
  min(p.canonical_name) AS display_name,
  min(p.name) AS sample_name,
  bool_or(p.is_organic) AS has_organic,
  min(p.current_price_cents) FILTER (WHERE p.store_id = 'ah') AS ah_cents,
  min(p.current_price_cents) FILTER (WHERE p.store_id = 'jumbo') AS jumbo_cents,
  min(p.current_price_cents) FILTER (WHERE p.store_id = 'dirk') AS dirk_cents,
  min(p.current_price_cents) FILTER (WHERE p.store_id = 'plus') AS plus_cents,
  min(p.current_price_cents) FILTER (WHERE p.store_id = 'spar') AS spar_cents,
  min(p.current_price_cents) FILTER (WHERE p.store_id = 'aldi') AS aldi_cents,
  count(DISTINCT p.store_id) AS store_count,
  count(*) AS member_count
FROM public.products p
WHERE p.is_available = true
  AND coalesce(p.canonical_key, '') <> ''
GROUP BY p.canonical_key;

CREATE OR REPLACE FUNCTION public.product_price_offers(p_product_id uuid)
RETURNS TABLE (
  product_id uuid,
  store_id text,
  product_name text,
  brand text,
  package_size_text text,
  current_price_cents int,
  unit_price_cents int,
  unit_price_unit text,
  image_url text,
  product_url text
)
LANGUAGE sql
STABLE
AS $$
  WITH self AS (
    SELECT id, ean_norm FROM public.products WHERE id = p_product_id
  )
  SELECT
    p.id, p.store_id, p.name, p.brand, p.package_size_text,
    p.current_price_cents, p.unit_price_cents, p.unit_price_unit,
    p.image_url, p.product_url
  FROM public.products p, self
  WHERE p.is_available = true
    AND ((self.ean_norm IS NOT NULL AND p.ean_norm = self.ean_norm) OR p.id = self.id)
  ORDER BY p.current_price_cents ASC NULLS LAST, p.store_id;
$$;

CREATE OR REPLACE FUNCTION public.product_group_offers(p_product_id uuid)
RETURNS TABLE (
  product_id uuid,
  store_id text,
  product_name text,
  brand text,
  package_size_text text,
  current_price_cents int,
  unit_price_cents int,
  unit_price_unit text,
  image_url text,
  product_url text,
  canonical_key text
)
LANGUAGE sql
STABLE
AS $$
  WITH self AS (
    SELECT canonical_key AS k FROM public.products WHERE id = p_product_id
  )
  SELECT DISTINCT ON (p.store_id)
    p.id, p.store_id, p.name, p.brand, p.package_size_text,
    p.current_price_cents, p.unit_price_cents, p.unit_price_unit,
    p.image_url, p.product_url, p.canonical_key
  FROM public.products p, self
  WHERE p.is_available = true
    AND p.canonical_key = self.k
    AND coalesce(self.k, '') <> ''
  ORDER BY p.store_id, p.current_price_cents ASC NULLS LAST, p.name;
$$;

CREATE OR REPLACE FUNCTION public.search_product_offers(query_text text, match_count integer DEFAULT 8)
RETURNS TABLE (
  product_id uuid,
  store_id text,
  product_name text,
  brand text,
  category text,
  package_size_text text,
  current_price_cents integer,
  unit_price_cents integer,
  unit_price_unit text,
  image_url text,
  product_url text,
  match_score numeric,
  canonical_key text,
  display_name text
)
LANGUAGE sql
STABLE
AS $$
  WITH params AS (
    SELECT regexp_replace(
             regexp_replace(
               btrim(regexp_replace(public.fold_text(coalesce(query_text, '')), '[^[:alnum:] ]+', ' ', 'g')),
               '\s+', '_', 'g'
             ),
             '^_+|_+$', '', 'g'
           ) AS qkey
  ),
  exact AS (
    SELECT
      p.canonical_key,
      1.0::numeric AS grp_score,
      count(*) AS hits,
      count(DISTINCT p.store_id) AS stores
    FROM public.products p, params
    WHERE p.is_available = true
      AND coalesce(p.canonical_key, '') <> ''
      AND p.canonical_key = params.qkey
    GROUP BY p.canonical_key
  ),
  ranked AS (
    SELECT s.match_score, p.canonical_key
    FROM public.search_products(query_text, NULL, greatest(coalesce(match_count, 8), 12)) s
    JOIN public.products p ON p.id = s.product_id
    WHERE coalesce(p.canonical_key, '') <> ''
  ),
  cand AS (
    SELECT
      r.canonical_key,
      max(r.match_score) AS grp_score,
      count(*) AS hits,
      (SELECT count(DISTINCT pp.store_id)
       FROM public.products pp
       WHERE pp.canonical_key = r.canonical_key AND pp.is_available) AS stores
    FROM ranked r
    GROUP BY r.canonical_key
  ),
  best AS (
    SELECT canonical_key, grp_score
    FROM (
      SELECT 0 AS priority, canonical_key, grp_score, hits, stores
      FROM exact
      UNION ALL
      SELECT 1 AS priority, canonical_key, grp_score, hits, stores
      FROM cand
      WHERE NOT EXISTS (SELECT 1 FROM exact)
        AND grp_score >= (SELECT max(grp_score) FROM cand) - 0.06
    ) choice
    ORDER BY priority, stores DESC, grp_score DESC, hits DESC, canonical_key
    LIMIT 1
  ),
  offers AS (
    SELECT DISTINCT ON (p.store_id)
      p.id, p.store_id, p.name, p.brand, p.category, p.package_size_text,
      p.current_price_cents, p.unit_price_cents, p.unit_price_unit,
      p.image_url, p.product_url,
      b.grp_score AS match_score, p.canonical_key, p.canonical_name
    FROM public.products p
    JOIN best b ON b.canonical_key = p.canonical_key
    WHERE p.is_available = true
    ORDER BY p.store_id, p.current_price_cents ASC NULLS LAST, p.name
  )
  SELECT
    o.id, o.store_id, o.name, o.brand, o.category, o.package_size_text,
    o.current_price_cents, o.unit_price_cents, o.unit_price_unit,
    o.image_url, o.product_url, o.match_score, o.canonical_key, o.canonical_name
  FROM offers o
  ORDER BY o.current_price_cents ASC NULLS LAST, o.store_id;
$$;
