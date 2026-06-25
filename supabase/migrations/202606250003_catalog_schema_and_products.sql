-- Medallion catalog schema + app-facing product tables.
--
-- Layout:
--   catalog schema  (service-role only, NOT exposed via PostgREST by default)
--     bronze  → catalog.scrape_runs, catalog.bronze_products   (raw, immutable)
--     silver  → catalog.silver_products                         (normalized)
--     gold    → catalog.gold_ingredients,                       (canonical)
--               catalog.gold_ingredient_aliases,
--               catalog.gold_product_mappings
--
--   public schema  (RLS, PostgREST API)
--     public.stores                   store definitions
--     public.products                 promoted from silver; consumed by shopping lists
--     public.recipe_product_matches   per-user ingredient → product overrides

CREATE SCHEMA IF NOT EXISTS catalog;

-- ────────────────────────────────────────────────────────────────────────────
-- BRONZE — raw, immutable scrape data
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE catalog.scrape_runs (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  store         text        NOT NULL CHECK (store IN ('ah','jumbo','dirk','plus')),
  status        text        NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running','completed','failed')),
  row_count     int,
  error_message text,
  started_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE catalog.bronze_products (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  scrape_run_id uuid        NOT NULL REFERENCES catalog.scrape_runs(id) ON DELETE CASCADE,
  store         text        NOT NULL CHECK (store IN ('ah','jumbo','dirk','plus')),
  raw_data      jsonb       NOT NULL,
  row_hash      text        NOT NULL UNIQUE,
  scraped_at    timestamptz NOT NULL DEFAULT now(),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX bronze_products_store_scraped_at_idx
  ON catalog.bronze_products (store, scraped_at DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- SILVER — normalized, standardized products
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE catalog.silver_products (
  id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  bronze_product_id uuid        REFERENCES catalog.bronze_products(id) ON DELETE SET NULL,
  store             text        NOT NULL CHECK (store IN ('ah','jumbo','dirk','plus')),
  external_id       text        NOT NULL,
  name              text        NOT NULL,
  brand             text,
  category          text,
  subcategory       text,
  package_size_text text,
  current_price_cents int,
  unit_price_cents  int,
  unit_price_unit   text,
  is_available      boolean     NOT NULL DEFAULT true,
  is_current        boolean     NOT NULL DEFAULT true,
  image_url         text,
  product_url       text,
  promotion         jsonb,
  attributes        jsonb,
  content_hash      text,
  first_seen_at     timestamptz NOT NULL DEFAULT now(),
  last_seen_at      timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (store, external_id)
);

CREATE INDEX silver_products_store_current_idx
  ON catalog.silver_products (store, is_current, is_available);

-- ────────────────────────────────────────────────────────────────────────────
-- GOLD — canonical ingredients + product mappings
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE catalog.gold_ingredients (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_name text        NOT NULL UNIQUE,
  category       text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE catalog.gold_ingredient_aliases (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  ingredient_id uuid        NOT NULL REFERENCES catalog.gold_ingredients(id) ON DELETE CASCADE,
  alias         text        NOT NULL,
  language      text        NOT NULL DEFAULT 'nl',
  confidence    float       NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ingredient_id, alias)
);

CREATE INDEX gold_ingredient_aliases_ingredient_id_idx
  ON catalog.gold_ingredient_aliases (ingredient_id);
CREATE INDEX gold_ingredient_aliases_alias_idx
  ON catalog.gold_ingredient_aliases (alias);

CREATE TABLE catalog.gold_product_mappings (
  id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  silver_product_id uuid        NOT NULL REFERENCES catalog.silver_products(id) ON DELETE CASCADE,
  ingredient_id     uuid        NOT NULL REFERENCES catalog.gold_ingredients(id) ON DELETE CASCADE,
  confidence        float       NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  mapping_source    text        NOT NULL DEFAULT 'ai_batch'
                                CHECK (mapping_source IN ('ai_batch','manual','rule')),
  review_status     text        NOT NULL DEFAULT 'pending'
                                CHECK (review_status IN ('pending','approved','rejected')),
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (silver_product_id, ingredient_id)
);

CREATE INDEX gold_product_mappings_ingredient_id_idx
  ON catalog.gold_product_mappings (ingredient_id);

-- ────────────────────────────────────────────────────────────────────────────
-- PUBLIC: app-facing stores + products
-- Column names match the app (shopping/[id].tsx, repository.ts).
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE public.stores (
  id          text    PRIMARY KEY,
  name        text    NOT NULL,
  logo_url    text,
  website_url text,
  is_active   boolean NOT NULL DEFAULT true
);

ALTER TABLE public.stores ENABLE ROW LEVEL SECURITY;
CREATE POLICY "stores_select_authenticated" ON public.stores
  FOR SELECT TO authenticated USING (true);

CREATE TABLE public.products (
  id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  store_id            text        NOT NULL REFERENCES public.stores(id),
  -- catalog lineage (nullable: products can be seeded manually before the pipeline runs)
  silver_product_id   uuid        REFERENCES catalog.silver_products(id) ON DELETE SET NULL,
  ingredient_id       uuid        REFERENCES catalog.gold_ingredients(id) ON DELETE SET NULL,
  -- app-facing columns (names must stay stable: app reads them by name)
  name                text        NOT NULL,
  brand               text,
  category            text,
  subcategory         text,
  package_size_text   text,
  current_price_cents int,
  unit_price_cents    int,
  unit_price_unit     text,
  is_available        boolean     NOT NULL DEFAULT true,
  image_url           text,
  product_url         text,
  synced_at           timestamptz NOT NULL DEFAULT now(),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX products_name_trgm_idx       ON public.products USING GIN (name gin_trgm_ops);
CREATE INDEX products_store_available_idx  ON public.products (store_id, is_available);
CREATE INDEX products_ingredient_id_idx    ON public.products (ingredient_id);

ALTER TABLE public.products ENABLE ROW LEVEL SECURITY;
CREATE POLICY "products_select_authenticated" ON public.products
  FOR SELECT TO authenticated USING (true);

CREATE TRIGGER products_set_updated_at
  BEFORE UPDATE ON public.products
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Restore product selection columns on shopping_list_items
ALTER TABLE public.shopping_list_items
  ADD COLUMN selected_product_id   uuid REFERENCES public.products(id) ON DELETE SET NULL,
  ADD COLUMN estimated_price_cents int;

-- Per-user ingredient → product overrides
CREATE TABLE public.recipe_product_matches (
  id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  recipe_ingredient_id uuid        NOT NULL REFERENCES public.recipe_ingredients(id) ON DELETE CASCADE,
  product_id           uuid        NOT NULL REFERENCES public.products(id) ON DELETE CASCADE,
  user_id              uuid        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  match_score          float,
  is_selected          boolean     NOT NULL DEFAULT false,
  created_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (recipe_ingredient_id, product_id, user_id)
);

ALTER TABLE public.recipe_product_matches ENABLE ROW LEVEL SECURITY;
CREATE POLICY "matches_own" ON public.recipe_product_matches
  USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- ────────────────────────────────────────────────────────────────────────────
-- search_products()
-- Return columns must stay stable: repository.ts and shopping/[id].tsx use them by name.
-- ────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION search_products(
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
  current_price_cents int,
  image_url           text,
  product_url         text,
  match_score         numeric
)
LANGUAGE sql STABLE
AS $$
  WITH norm AS (
    SELECT lower(trim(query_text)) AS q
  ),
  tokens AS (
    SELECT word
    FROM unnest(string_to_array((SELECT q FROM norm), ' ')) AS word
    WHERE length(word) >= 2
      AND word NOT IN (
        'de','het','een','van','met','en','of','in','op','voor','aan',
        'wit','rood','vers','biologisch','gezouten','klein','groot',
        'heel','fijn','jong','oud','licht','extra','zonder'
      )
  ),
  canonical_scores AS (
    SELECT
      p.id AS product_id,
      MAX(COALESCE(similarity(a.alias, (SELECT q FROM norm)), 0))
        + CASE WHEN gi.canonical_name = (SELECT q FROM norm) THEN 0.5 ELSE 0 END AS canon_score
    FROM public.products p
    JOIN catalog.gold_ingredients gi ON gi.id = p.ingredient_id
    LEFT JOIN catalog.gold_ingredient_aliases a ON a.ingredient_id = gi.id
    GROUP BY p.id, gi.canonical_name
  ),
  scored AS (
    SELECT
      p.id                        AS product_id,
      p.store_id,
      p.name                      AS product_name,
      p.brand,
      p.category,
      p.package_size_text,
      p.current_price_cents,
      p.image_url,
      p.product_url,
      GREATEST(
        similarity(p.name, (SELECT q FROM norm)),
        COALESCE(cs.canon_score, 0)
      )::numeric                  AS match_score
    FROM public.products p
    LEFT JOIN canonical_scores cs ON cs.product_id = p.id
    WHERE p.is_available = true
      AND (store_filter IS NULL OR p.store_id = store_filter)
      AND (
        similarity(p.name, (SELECT q FROM norm)) > 0.15
        OR COALESCE(cs.canon_score, 0) > 0.15
        OR p.name ILIKE '%' || (SELECT q FROM norm) || '%'
        OR EXISTS (
          SELECT 1 FROM tokens t
          WHERE p.name ILIKE '%' || t.word || '%'
        )
      )
  )
  SELECT product_id, store_id, product_name, brand, category,
         package_size_text, current_price_cents, image_url, product_url, match_score
  FROM scored
  WHERE match_score > 0.10
  ORDER BY match_score DESC, current_price_cents ASC NULLS LAST, product_name ASC
  LIMIT least(greatest(match_count, 1), 20);
$$;

-- ────────────────────────────────────────────────────────────────────────────
-- SEED: stores
-- ────────────────────────────────────────────────────────────────────────────

INSERT INTO public.stores (id, name, website_url) VALUES
  ('ah',    'Albert Heijn', 'https://www.ah.nl'),
  ('jumbo', 'Jumbo',        'https://www.jumbo.com'),
  ('dirk',  'Dirk',         'https://www.dirk.nl'),
  ('plus',  'PLUS',         'https://www.plus.nl')
ON CONFLICT (id) DO NOTHING;
