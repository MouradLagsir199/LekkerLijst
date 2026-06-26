-- Robust product grouping.
--
-- Layer 1: exact_product_groups = same physical SKU/product. Shared EAN is the
-- only auto-approved exact signal. Non-EAN exact candidates go to review.
--
-- Layer 2: catalog.gold_ingredients = substitute groups used by search and
-- cheapest-basket logic. Same EAN groups are safe substitutes; broader
-- same-spec substitutions are review/AI approved before they affect users.

-- ── Gold support tables (repair missing live tables) ─────────────────────────

ALTER TABLE catalog.gold_ingredients
  DROP CONSTRAINT IF EXISTS gold_ingredients_canonical_name_key;

ALTER TABLE catalog.gold_ingredients
  ADD COLUMN IF NOT EXISTS group_key text,
  ADD COLUMN IF NOT EXISTS group_kind text NOT NULL DEFAULT 'substitute'
    CHECK (group_kind IN ('substitute')),
  ADD COLUMN IF NOT EXISTS ean text,
  ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual',
  ADD COLUMN IF NOT EXISTS confidence numeric(5, 4) NOT NULL DEFAULT 1.0
    CHECK (confidence BETWEEN 0 AND 1),
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

UPDATE catalog.gold_ingredients
   SET group_key = 'legacy:' || id::text
 WHERE group_key IS NULL;

ALTER TABLE catalog.gold_ingredients
  ALTER COLUMN group_key SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS gold_ingredients_group_key_idx
  ON catalog.gold_ingredients (group_key);

CREATE UNIQUE INDEX IF NOT EXISTS gold_ingredients_ean_idx
  ON catalog.gold_ingredients (ean)
  WHERE ean IS NOT NULL;

CREATE TABLE IF NOT EXISTS catalog.gold_ingredient_aliases (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  ingredient_id uuid        NOT NULL REFERENCES catalog.gold_ingredients(id) ON DELETE CASCADE,
  alias         text        NOT NULL,
  language      text        NOT NULL DEFAULT 'nl',
  confidence    float       NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ingredient_id, alias)
);

CREATE INDEX IF NOT EXISTS gold_ingredient_aliases_ingredient_id_idx
  ON catalog.gold_ingredient_aliases (ingredient_id);

CREATE INDEX IF NOT EXISTS gold_ingredient_aliases_alias_idx
  ON catalog.gold_ingredient_aliases (alias);

CREATE TABLE IF NOT EXISTS catalog.gold_product_mappings (
  id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  silver_product_id uuid        NOT NULL REFERENCES catalog.silver_products(id) ON DELETE CASCADE,
  ingredient_id     uuid        NOT NULL REFERENCES catalog.gold_ingredients(id) ON DELETE CASCADE,
  confidence        float       NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  mapping_source    text        NOT NULL DEFAULT 'rule'
                                CHECK (mapping_source IN ('ai_batch','manual','rule')),
  review_status     text        NOT NULL DEFAULT 'pending'
                                CHECK (review_status IN ('pending','approved','rejected')),
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (silver_product_id, ingredient_id)
);

CREATE INDEX IF NOT EXISTS gold_product_mappings_ingredient_id_idx
  ON catalog.gold_product_mappings (ingredient_id);

-- ── Exact product grouping ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS catalog.exact_product_groups (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  group_key      text        NOT NULL UNIQUE,
  canonical_name text,
  ean            text,
  source         text        NOT NULL DEFAULT 'rule'
                              CHECK (source IN ('ean','rule','ai','manual')),
  confidence     numeric(5, 4) NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS exact_product_groups_ean_idx
  ON catalog.exact_product_groups (ean)
  WHERE ean IS NOT NULL;

CREATE TABLE IF NOT EXISTS catalog.exact_product_group_members (
  exact_product_group_id uuid NOT NULL REFERENCES catalog.exact_product_groups(id) ON DELETE CASCADE,
  silver_product_id      uuid NOT NULL REFERENCES catalog.silver_products(id) ON DELETE CASCADE,
  source                 text NOT NULL DEFAULT 'rule'
                              CHECK (source IN ('ean','rule','ai','manual')),
  confidence             numeric(5, 4) NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  created_at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (exact_product_group_id, silver_product_id),
  UNIQUE (silver_product_id)
);

CREATE INDEX IF NOT EXISTS exact_product_group_members_group_idx
  ON catalog.exact_product_group_members (exact_product_group_id);

ALTER TABLE public.products
  ADD COLUMN IF NOT EXISTS exact_product_group_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'products_exact_product_group_id_fkey'
      AND conrelid = 'public.products'::regclass
  ) THEN
    ALTER TABLE public.products
      ADD CONSTRAINT products_exact_product_group_id_fkey
      FOREIGN KEY (exact_product_group_id)
      REFERENCES catalog.exact_product_groups(id)
      ON DELETE SET NULL
      NOT VALID;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS products_exact_product_group_id_idx
  ON public.products (exact_product_group_id);

-- ── Review queue ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS catalog.group_review_candidates (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_kind text        NOT NULL CHECK (candidate_kind IN ('exact','substitute')),
  candidate_key  text        NOT NULL UNIQUE,
  status         text        NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','approved','rejected','needs_later')),
  source         text        NOT NULL DEFAULT 'rule'
                              CHECK (source IN ('rule','ai','manual')),
  canonical_name text,
  confidence     numeric(5, 4) NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  reason         text,
  ai_model       text,
  ai_decision    jsonb,
  ai_confidence  numeric(5, 4) CHECK (ai_confidence BETWEEN 0 AND 1),
  ai_reason      text,
  safety_flags   jsonb,
  reviewed_at    timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS group_review_candidates_status_idx
  ON catalog.group_review_candidates (status, candidate_kind, confidence DESC);

CREATE TABLE IF NOT EXISTS catalog.group_review_candidate_members (
  candidate_id      uuid NOT NULL REFERENCES catalog.group_review_candidates(id) ON DELETE CASCADE,
  silver_product_id uuid NOT NULL REFERENCES catalog.silver_products(id) ON DELETE CASCADE,
  position          int  NOT NULL DEFAULT 0,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (candidate_id, silver_product_id)
);

CREATE INDEX IF NOT EXISTS group_review_candidate_members_product_idx
  ON catalog.group_review_candidate_members (silver_product_id);

-- ── Helpers ─────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION catalog.catalog_norm(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT NULLIF(
    btrim(
      regexp_replace(
        regexp_replace(lower(coalesce(value, '')), '[^[:alnum:]]+', ' ', 'g'),
        '\s+',
        ' ',
        'g'
      )
    ),
    ''
  )
$$;

CREATE OR REPLACE FUNCTION catalog.catalog_variant_signature(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT concat_ws('|',
    CASE WHEN lower(coalesce(value, '')) ~ '\m(bio|biologisch|organic)\M' THEN 'organic' END,
    CASE WHEN lower(coalesce(value, '')) ~ '\m(glutenvrij|gluten free)\M' THEN 'glutenfree' END,
    CASE WHEN lower(coalesce(value, '')) ~ '\m(lactosevrij|lactose free)\M' THEN 'lactosefree' END,
    CASE WHEN lower(coalesce(value, '')) ~ '\m(vegan|vega|vegetarisch|plantaardig)\M' THEN 'plantbased' END,
    CASE WHEN lower(coalesce(value, '')) ~ '\m(halal)\M' THEN 'halal' END,
    CASE WHEN lower(coalesce(value, '')) ~ '\m(alcoholvrij|0\.0|0,0)\M' THEN 'alcoholfree' END,
    CASE WHEN lower(coalesce(value, '')) ~ '\m(baby|peuter|dreumes|zuigeling)\M' THEN 'baby' END,
    CASE WHEN lower(coalesce(value, '')) ~ '\m(hond|kat|katten|honden|huisdier)\M' THEN 'pet' END,
    CASE WHEN lower(coalesce(value, '')) ~ '\m(light|zero|suikervrij|minder suiker|dieet)\M' THEN 'diet' END
  )
$$;

-- ── Deterministic EAN grouping ───────────────────────────────────────────────

CREATE OR REPLACE FUNCTION catalog.refresh_exact_ean_groups()
RETURNS TABLE(step text, rows_affected integer)
LANGUAGE plpgsql
AS $$
DECLARE
  exact_groups int := 0;
  exact_members int := 0;
  public_exact int := 0;
  substitute_groups int := 0;
  substitute_mappings int := 0;
  public_substitute int := 0;
BEGIN
  WITH canonical AS (
    SELECT DISTINCT ON (sp.ean)
      sp.ean,
      sp.name AS canonical_name
    FROM catalog.silver_products sp
    WHERE sp.ean IS NOT NULL
      AND btrim(sp.ean) <> ''
      AND sp.name IS NOT NULL
    ORDER BY
      sp.ean,
      CASE sp.store
        WHEN 'ah' THEN 0
        WHEN 'jumbo' THEN 1
        WHEN 'dirk' THEN 2
        WHEN 'spar' THEN 3
        WHEN 'plus' THEN 4
        ELSE 5
      END,
      sp.name
  ),
  ins AS (
    INSERT INTO catalog.exact_product_groups (group_key, canonical_name, ean, source, confidence)
    SELECT 'ean:' || c.ean, c.canonical_name, c.ean, 'ean', 1.0
    FROM canonical c
    ON CONFLICT (group_key) DO UPDATE SET
      canonical_name = EXCLUDED.canonical_name,
      ean = EXCLUDED.ean,
      source = 'ean',
      confidence = 1.0,
      updated_at = now()
    RETURNING 1
  )
  SELECT count(*) INTO exact_groups FROM ins;

  WITH ins AS (
    INSERT INTO catalog.exact_product_group_members (
      exact_product_group_id,
      silver_product_id,
      source,
      confidence
    )
    SELECT eg.id, sp.id, 'ean', 1.0
    FROM catalog.silver_products sp
    JOIN catalog.exact_product_groups eg ON eg.ean = sp.ean
    WHERE sp.ean IS NOT NULL
      AND btrim(sp.ean) <> ''
    ON CONFLICT (silver_product_id) DO UPDATE SET
      exact_product_group_id = EXCLUDED.exact_product_group_id,
      source = 'ean',
      confidence = 1.0
    RETURNING 1
  )
  SELECT count(*) INTO exact_members FROM ins;

  UPDATE public.products pp
     SET exact_product_group_id = egm.exact_product_group_id,
         updated_at = now()
    FROM catalog.exact_product_group_members egm
   WHERE egm.silver_product_id = pp.silver_product_id
     AND pp.exact_product_group_id IS DISTINCT FROM egm.exact_product_group_id;
  GET DIAGNOSTICS public_exact = ROW_COUNT;

  WITH canonical AS (
    SELECT DISTINCT ON (sp.ean)
      sp.ean,
      sp.name AS canonical_name
    FROM catalog.silver_products sp
    WHERE sp.ean IS NOT NULL
      AND btrim(sp.ean) <> ''
      AND sp.name IS NOT NULL
    ORDER BY
      sp.ean,
      CASE sp.store
        WHEN 'ah' THEN 0
        WHEN 'jumbo' THEN 1
        WHEN 'dirk' THEN 2
        WHEN 'spar' THEN 3
        WHEN 'plus' THEN 4
        ELSE 5
      END,
      sp.name
  ),
  ins AS (
    INSERT INTO catalog.gold_ingredients (
      group_key,
      group_kind,
      canonical_name,
      ean,
      source,
      confidence,
      updated_at
    )
    SELECT 'ean:' || c.ean, 'substitute', c.canonical_name, c.ean, 'ean', 1.0, now()
    FROM canonical c
    ON CONFLICT (group_key) DO UPDATE SET
      canonical_name = EXCLUDED.canonical_name,
      ean = EXCLUDED.ean,
      source = 'ean',
      confidence = 1.0,
      updated_at = now()
    RETURNING 1
  )
  SELECT count(*) INTO substitute_groups FROM ins;

  INSERT INTO catalog.gold_ingredient_aliases (ingredient_id, alias, language, confidence)
  SELECT DISTINCT gi.id, sp.name, 'nl', 1.0
  FROM catalog.silver_products sp
  JOIN catalog.gold_ingredients gi ON gi.ean = sp.ean
  WHERE sp.ean IS NOT NULL
    AND btrim(sp.ean) <> ''
    AND sp.name IS NOT NULL
  ON CONFLICT (ingredient_id, alias) DO NOTHING;

  WITH ins AS (
    INSERT INTO catalog.gold_product_mappings (
      silver_product_id,
      ingredient_id,
      confidence,
      mapping_source,
      review_status
    )
    SELECT sp.id, gi.id, 1.0, 'rule', 'approved'
    FROM catalog.silver_products sp
    JOIN catalog.gold_ingredients gi ON gi.ean = sp.ean
    WHERE sp.ean IS NOT NULL
      AND btrim(sp.ean) <> ''
    ON CONFLICT (silver_product_id, ingredient_id) DO UPDATE SET
      confidence = 1.0,
      mapping_source = 'rule',
      review_status = 'approved'
    RETURNING 1
  )
  SELECT count(*) INTO substitute_mappings FROM ins;

  UPDATE public.products pp
     SET ingredient_id = gm.ingredient_id,
         updated_at = now()
    FROM catalog.gold_product_mappings gm
   WHERE gm.silver_product_id = pp.silver_product_id
     AND gm.review_status = 'approved'
     AND pp.ingredient_id IS DISTINCT FROM gm.ingredient_id;
  GET DIAGNOSTICS public_substitute = ROW_COUNT;

  RETURN QUERY VALUES
    ('exact_groups'::text, exact_groups),
    ('exact_members'::text, exact_members),
    ('public_exact_products'::text, public_exact),
    ('substitute_groups'::text, substitute_groups),
    ('substitute_mappings'::text, substitute_mappings),
    ('public_substitute_products'::text, public_substitute);
END;
$$;

-- ── Rule candidate generation for AI/admin review ───────────────────────────

CREATE OR REPLACE FUNCTION catalog.generate_rule_group_review_candidates(max_candidates integer DEFAULT 2000)
RETURNS TABLE(candidate_kind text, candidates_created integer)
LANGUAGE plpgsql
AS $$
DECLARE
  exact_created int := 0;
  substitute_created int := 0;
BEGIN
  WITH features AS (
    SELECT
      sp.id,
      sp.store,
      sp.name,
      sp.ean,
      sp.price,
      sp.base_price,
      sp.base_price_unit,
      catalog.catalog_norm(sp.name) AS norm_name,
      catalog.catalog_variant_signature(sp.name) AS variant_sig,
      CASE
        WHEN sp.price IS NOT NULL AND sp.base_price IS NOT NULL AND sp.base_price > 0
        THEN round(sp.price / sp.base_price, 3)
      END AS pack_qty
    FROM catalog.silver_products sp
    LEFT JOIN catalog.exact_product_group_members egm ON egm.silver_product_id = sp.id
    WHERE sp.name IS NOT NULL
      AND (sp.ean IS NULL OR btrim(sp.ean) = '')
      AND egm.silver_product_id IS NULL
  ),
  grouped AS (
    SELECT
      'exact:name:' || norm_name || ':variant:' || coalesce(variant_sig, '') ||
        ':unit:' || coalesce(base_price_unit, '') || ':qty:' || coalesce(pack_qty::text, '') AS candidate_key,
      min(name) AS canonical_name,
      count(*) AS member_count,
      count(DISTINCT store) AS store_count,
      array_agg(id ORDER BY store, name) AS silver_ids
    FROM features
    WHERE norm_name IS NOT NULL
    GROUP BY norm_name, coalesce(variant_sig, ''), base_price_unit, pack_qty
    HAVING count(*) BETWEEN 2 AND 10
       AND count(DISTINCT store) >= 2
    ORDER BY count(DISTINCT store) DESC, count(*) DESC, min(name)
    LIMIT max_candidates
  ),
  ins AS (
    INSERT INTO catalog.group_review_candidates (
      candidate_kind,
      candidate_key,
      source,
      canonical_name,
      confidence,
      reason
    )
    SELECT
      'exact',
      g.candidate_key,
      'rule',
      g.canonical_name,
      0.72,
      'Same normalized name, diet/quality flags, package quantity and unit; no shared EAN, so queued for review.'
    FROM grouped g
    ON CONFLICT (candidate_key) DO NOTHING
    RETURNING id
  )
  SELECT count(*) INTO exact_created FROM ins;

  WITH features AS (
    SELECT
      sp.id,
      sp.store,
      sp.name,
      sp.price,
      sp.base_price,
      sp.base_price_unit,
      catalog.catalog_variant_signature(sp.name) AS variant_sig,
      CASE
        WHEN sp.price IS NOT NULL AND sp.base_price IS NOT NULL AND sp.base_price > 0
        THEN round(sp.price / sp.base_price, 2)
      END AS pack_qty,
      btrim(
        regexp_replace(
          catalog.catalog_norm(sp.name),
          '\m(ah|albert|heijn|jumbo|aldi|dirk|plus|spar|huismerk|basic|biologisch|bio)\M',
          '',
          'g'
        )
      ) AS generic_name
    FROM catalog.silver_products sp
    WHERE sp.name IS NOT NULL
      AND sp.price IS NOT NULL
  ),
  grouped AS (
    SELECT
      'substitute:name:' || generic_name || ':variant:' || coalesce(variant_sig, '') ||
        ':unit:' || coalesce(base_price_unit, '') || ':qty:' || coalesce(pack_qty::text, '') AS candidate_key,
      min(name) AS canonical_name,
      count(*) AS member_count,
      count(DISTINCT store) AS store_count,
      array_agg(id ORDER BY store, price NULLS LAST, name) AS silver_ids
    FROM features
    WHERE generic_name IS NOT NULL
      AND generic_name <> ''
      AND base_price_unit IN ('kg','l')
      AND pack_qty IS NOT NULL
    GROUP BY generic_name, coalesce(variant_sig, ''), base_price_unit, pack_qty
    HAVING count(*) BETWEEN 2 AND 16
       AND count(DISTINCT store) >= 2
    ORDER BY count(DISTINCT store) DESC, count(*) DESC, min(name)
    LIMIT max_candidates
  ),
  ins AS (
    INSERT INTO catalog.group_review_candidates (
      candidate_kind,
      candidate_key,
      source,
      canonical_name,
      confidence,
      reason
    )
    SELECT
      'substitute',
      g.candidate_key,
      'rule',
      g.canonical_name,
      0.62,
      'Same generic normalized name, diet/quality flags, package quantity and unit; queued for AI/admin review.'
    FROM grouped g
    ON CONFLICT (candidate_key) DO NOTHING
    RETURNING id
  )
  SELECT count(*) INTO substitute_created FROM ins;

  WITH features AS (
    SELECT
      sp.id,
      sp.store,
      sp.name,
      sp.ean,
      sp.price,
      sp.base_price,
      sp.base_price_unit,
      catalog.catalog_norm(sp.name) AS norm_name,
      catalog.catalog_variant_signature(sp.name) AS variant_sig,
      CASE
        WHEN sp.price IS NOT NULL AND sp.base_price IS NOT NULL AND sp.base_price > 0
        THEN round(sp.price / sp.base_price, 3)
      END AS pack_qty
    FROM catalog.silver_products sp
    LEFT JOIN catalog.exact_product_group_members egm ON egm.silver_product_id = sp.id
    WHERE sp.name IS NOT NULL
      AND (sp.ean IS NULL OR btrim(sp.ean) = '')
      AND egm.silver_product_id IS NULL
  ),
  grouped AS (
    SELECT
      'exact:name:' || norm_name || ':variant:' || coalesce(variant_sig, '') ||
        ':unit:' || coalesce(base_price_unit, '') || ':qty:' || coalesce(pack_qty::text, '') AS candidate_key,
      array_agg(id ORDER BY store, name) AS silver_ids
    FROM features
    WHERE norm_name IS NOT NULL
    GROUP BY norm_name, coalesce(variant_sig, ''), base_price_unit, pack_qty
    HAVING count(*) BETWEEN 2 AND 10
       AND count(DISTINCT store) >= 2
  )
  INSERT INTO catalog.group_review_candidate_members (candidate_id, silver_product_id, position)
  SELECT c.id, member_id, ordinality::int
  FROM grouped g
  JOIN catalog.group_review_candidates c ON c.candidate_key = g.candidate_key
  CROSS JOIN unnest(g.silver_ids) WITH ORDINALITY AS member(member_id, ordinality)
  WHERE c.status = 'pending'
  ON CONFLICT (candidate_id, silver_product_id) DO NOTHING;

  WITH features AS (
    SELECT
      sp.id,
      sp.store,
      sp.name,
      sp.price,
      sp.base_price,
      sp.base_price_unit,
      catalog.catalog_variant_signature(sp.name) AS variant_sig,
      CASE
        WHEN sp.price IS NOT NULL AND sp.base_price IS NOT NULL AND sp.base_price > 0
        THEN round(sp.price / sp.base_price, 2)
      END AS pack_qty,
      btrim(
        regexp_replace(
          catalog.catalog_norm(sp.name),
          '\m(ah|albert|heijn|jumbo|aldi|dirk|plus|spar|huismerk|basic|biologisch|bio)\M',
          '',
          'g'
        )
      ) AS generic_name
    FROM catalog.silver_products sp
    WHERE sp.name IS NOT NULL
      AND sp.price IS NOT NULL
  ),
  grouped AS (
    SELECT
      'substitute:name:' || generic_name || ':variant:' || coalesce(variant_sig, '') ||
        ':unit:' || coalesce(base_price_unit, '') || ':qty:' || coalesce(pack_qty::text, '') AS candidate_key,
      array_agg(id ORDER BY store, price NULLS LAST, name) AS silver_ids
    FROM features
    WHERE generic_name IS NOT NULL
      AND generic_name <> ''
      AND base_price_unit IN ('kg','l')
      AND pack_qty IS NOT NULL
    GROUP BY generic_name, coalesce(variant_sig, ''), base_price_unit, pack_qty
    HAVING count(*) BETWEEN 2 AND 16
       AND count(DISTINCT store) >= 2
  )
  INSERT INTO catalog.group_review_candidate_members (candidate_id, silver_product_id, position)
  SELECT c.id, member_id, ordinality::int
  FROM grouped g
  JOIN catalog.group_review_candidates c ON c.candidate_key = g.candidate_key
  CROSS JOIN unnest(g.silver_ids) WITH ORDINALITY AS member(member_id, ordinality)
  WHERE c.status = 'pending'
  ON CONFLICT (candidate_id, silver_product_id) DO NOTHING;

  RETURN QUERY VALUES
    ('exact'::text, exact_created),
    ('substitute'::text, substitute_created);
END;
$$;

-- ── Admin approval function ─────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION catalog.apply_group_review_candidate(candidate_id uuid, action text)
RETURNS TABLE(status text, rows_affected integer)
LANGUAGE plpgsql
AS $$
DECLARE
  candidate catalog.group_review_candidates%ROWTYPE;
  group_id uuid;
  affected int := 0;
BEGIN
  SELECT *
    INTO candidate
    FROM catalog.group_review_candidates
   WHERE id = candidate_id
   FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Unknown group review candidate: %', candidate_id;
  END IF;

  IF action NOT IN ('approve','reject','needs_later') THEN
    RAISE EXCEPTION 'Unsupported review action: %', action;
  END IF;

  IF action = 'reject' THEN
    UPDATE catalog.group_review_candidates
       SET status = 'rejected', reviewed_at = now(), updated_at = now()
     WHERE id = candidate_id;
    RETURN QUERY VALUES ('rejected'::text, 0);
    RETURN;
  END IF;

  IF action = 'needs_later' THEN
    UPDATE catalog.group_review_candidates
       SET status = 'needs_later', reviewed_at = now(), updated_at = now()
     WHERE id = candidate_id;
    RETURN QUERY VALUES ('needs_later'::text, 0);
    RETURN;
  END IF;

  IF candidate.candidate_kind = 'exact' THEN
    INSERT INTO catalog.exact_product_groups (
      group_key,
      canonical_name,
      source,
      confidence,
      updated_at
    )
    VALUES (
      'review:exact:' || candidate.id::text,
      candidate.canonical_name,
      CASE WHEN candidate.source = 'ai' OR candidate.ai_decision IS NOT NULL THEN 'ai' ELSE 'manual' END,
      COALESCE(candidate.ai_confidence, candidate.confidence, 0.8),
      now()
    )
    ON CONFLICT (group_key) DO UPDATE SET
      canonical_name = EXCLUDED.canonical_name,
      source = EXCLUDED.source,
      confidence = EXCLUDED.confidence,
      updated_at = now()
    RETURNING id INTO group_id;

    INSERT INTO catalog.exact_product_group_members (
      exact_product_group_id,
      silver_product_id,
      source,
      confidence
    )
    SELECT
      group_id,
      m.silver_product_id,
      CASE WHEN candidate.source = 'ai' OR candidate.ai_decision IS NOT NULL THEN 'ai' ELSE 'manual' END,
      COALESCE(candidate.ai_confidence, candidate.confidence, 0.8)
    FROM catalog.group_review_candidate_members m
    WHERE m.candidate_id = candidate_id
    ON CONFLICT (silver_product_id) DO UPDATE SET
      exact_product_group_id = EXCLUDED.exact_product_group_id,
      source = EXCLUDED.source,
      confidence = EXCLUDED.confidence;

    UPDATE public.products pp
       SET exact_product_group_id = group_id,
           updated_at = now()
      FROM catalog.group_review_candidate_members m
     WHERE m.candidate_id = candidate_id
       AND m.silver_product_id = pp.silver_product_id
       AND pp.exact_product_group_id IS DISTINCT FROM group_id;
    GET DIAGNOSTICS affected = ROW_COUNT;
  ELSE
    INSERT INTO catalog.gold_ingredients (
      group_key,
      group_kind,
      canonical_name,
      source,
      confidence,
      updated_at
    )
    VALUES (
      'review:substitute:' || candidate.id::text,
      'substitute',
      candidate.canonical_name,
      CASE WHEN candidate.source = 'ai' OR candidate.ai_decision IS NOT NULL THEN 'ai' ELSE 'manual' END,
      COALESCE(candidate.ai_confidence, candidate.confidence, 0.8),
      now()
    )
    ON CONFLICT (group_key) DO UPDATE SET
      canonical_name = EXCLUDED.canonical_name,
      source = EXCLUDED.source,
      confidence = EXCLUDED.confidence,
      updated_at = now()
    RETURNING id INTO group_id;

    INSERT INTO catalog.gold_ingredient_aliases (ingredient_id, alias, language, confidence)
    SELECT DISTINCT
      group_id,
      sp.name,
      'nl',
      COALESCE(candidate.ai_confidence, candidate.confidence, 0.8)
    FROM catalog.group_review_candidate_members m
    JOIN catalog.silver_products sp ON sp.id = m.silver_product_id
    WHERE m.candidate_id = candidate_id
      AND sp.name IS NOT NULL
    ON CONFLICT (ingredient_id, alias) DO NOTHING;

    INSERT INTO catalog.gold_product_mappings (
      silver_product_id,
      ingredient_id,
      confidence,
      mapping_source,
      review_status
    )
    SELECT
      m.silver_product_id,
      group_id,
      COALESCE(candidate.ai_confidence, candidate.confidence, 0.8),
      CASE WHEN candidate.source = 'ai' OR candidate.ai_decision IS NOT NULL THEN 'ai_batch' ELSE 'manual' END,
      'approved'
    FROM catalog.group_review_candidate_members m
    WHERE m.candidate_id = candidate_id
    ON CONFLICT (silver_product_id, ingredient_id) DO UPDATE SET
      confidence = EXCLUDED.confidence,
      mapping_source = EXCLUDED.mapping_source,
      review_status = 'approved';

    UPDATE public.products pp
       SET ingredient_id = group_id,
           updated_at = now()
      FROM catalog.group_review_candidate_members m
     WHERE m.candidate_id = candidate_id
       AND m.silver_product_id = pp.silver_product_id
       AND pp.ingredient_id IS DISTINCT FROM group_id;
    GET DIAGNOSTICS affected = ROW_COUNT;
  END IF;

  UPDATE catalog.group_review_candidates
     SET status = 'approved', reviewed_at = now(), updated_at = now()
   WHERE id = candidate_id;

  RETURN QUERY VALUES ('approved'::text, affected);
END;
$$;

-- ── Public app RPC: offers for exact/substitute groups ───────────────────────

CREATE OR REPLACE FUNCTION public.get_product_group_offers(
  input_product_id uuid,
  group_kind text DEFAULT 'exact'
)
RETURNS TABLE (
  product_id uuid,
  store_id text,
  product_name text,
  brand text,
  category text,
  package_size_text text,
  current_price_cents int,
  unit_price_cents int,
  unit_price_unit text,
  image_url text,
  product_url text,
  exact_product_group_id uuid,
  ingredient_id uuid
)
LANGUAGE sql
STABLE
AS $$
  WITH selected AS (
    SELECT p.*
    FROM public.products p
    WHERE p.id = input_product_id
  )
  SELECT
    p.id,
    p.store_id,
    p.name,
    p.brand,
    p.category,
    p.package_size_text,
    p.current_price_cents,
    p.unit_price_cents,
    p.unit_price_unit,
    p.image_url,
    p.product_url,
    p.exact_product_group_id,
    p.ingredient_id
  FROM selected s
  JOIN public.products p ON CASE
    WHEN group_kind = 'substitute' AND s.ingredient_id IS NOT NULL
      THEN p.ingredient_id = s.ingredient_id
    WHEN s.exact_product_group_id IS NOT NULL
      THEN p.exact_product_group_id = s.exact_product_group_id
    ELSE p.id = s.id
  END
  WHERE p.is_available = true
  ORDER BY p.current_price_cents ASC NULLS LAST, p.store_id, p.name;
$$;

GRANT SELECT, INSERT, UPDATE, DELETE ON catalog.exact_product_groups TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON catalog.exact_product_group_members TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON catalog.gold_ingredients TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON catalog.gold_ingredient_aliases TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON catalog.gold_product_mappings TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON catalog.group_review_candidates TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON catalog.group_review_candidate_members TO service_role;
GRANT EXECUTE ON FUNCTION catalog.refresh_exact_ean_groups() TO service_role;
GRANT EXECUTE ON FUNCTION catalog.generate_rule_group_review_candidates(integer) TO service_role;
GRANT EXECUTE ON FUNCTION catalog.apply_group_review_candidate(uuid, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_product_group_offers(uuid, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_product_group_offers(uuid, text) TO service_role;
