-- Deterministic guardrails for high-frequency recipe ingredients.
--
-- These fix failures found by smoke-testing public.search_product_offers:
-- bloem resolved to non-food "flower" products, banana picked flavored products,
-- and fresh paprika/tomato/potato/onion were fragmented under plural/spec keys.

WITH keyed AS (
  SELECT
    p.id,
    CASE
      WHEN k IN (
        'coca_cola', 'coca_cola_regular', 'cola_original_taste',
        'cola_regular', 'cola_regulier', 'cola_regular_frisdrank',
        'pepsi_cola'
      ) THEN 'cola'
      WHEN k IN (
        'coca_cola_zero', 'cola_zero_frisdrank', 'cola_zero_sugar',
        'cola_zero_suiker'
      ) THEN 'cola_zero'
      WHEN k IN ('cola_cherry', 'cola_kers_frisdrank') THEN 'cola_kers'
      WHEN k IN ('cola_zero_cherry', 'cola_zero_kers_frisdrank') THEN 'cola_zero_kers'
      WHEN k = 'cola_vanilla' THEN 'cola_vanille'
      WHEN k IN ('tarwebloem', 'patentbloem') THEN 'bloem'
      WHEN k = 'banaan'
        AND n ~ '\m(olvarit|baby|maaza|yo to go|yoghurt|kwark|drink|drank|milk|melk|smoothie|pap|knijpfruit|danoontje|alpro|brinta|muller|müller|breaker|proteine|proteïne|ijs|chips|smaak)\M'
        THEN 'banaan_smaak'
      WHEN k IN (
        'paprika_geel', 'paprika_rood', 'rode_paprika',
        'paprika_mix', 'paprikamix', 'paprika_duo'
      ) THEN 'paprika'
      WHEN k = 'paprika'
        AND n ~ '\m(euroma|essential|paprikapoeder|poeder|kruiden|chips|heartbreakers|pringles|lays|wokkels|tuc|doritos|snack|borrel|saus|spread|roomkaas|focaccia|tortilla)\M'
        THEN 'paprika_smaak'
      WHEN k IN ('tomaten', 'trostomaten', 'trostomaat', 'cherrytomaten') THEN 'tomaat'
      WHEN k IN (
        'aardappelen', 'aardappel_kruimig', 'iets_kruimige_aardappelen',
        'vastkokende_aardappelen', 'kruimige_aardappelen'
      ) THEN 'aardappel'
      WHEN k IN ('uien', 'gele_uien', 'uien_geel') THEN 'ui'
      WHEN k IN (
        'goudse_kaas_jong', 'goudse_kaas_jong_48_plus',
        'goudse_kaas_jong_48_plus_plakken', 'goudse_kaas_mild'
      ) THEN 'jonge_kaas'
      WHEN k = 'kaas' AND n LIKE '%kaas 48%' THEN 'jonge_kaas'
      ELSE k
    END AS normalized_key
  FROM public.products p
  CROSS JOIN LATERAL (
    SELECT
      regexp_replace(
        regexp_replace(public.fold_text(coalesce(p.canonical_key, '')), '[^[:alnum:]]+', '_', 'g'),
        '^_+|_+$',
        '',
        'g'
      ) AS k,
      public.fold_text(coalesce(p.name, '')) AS n
  ) folded
  WHERE coalesce(p.canonical_key, '') <> ''
),
named AS (
  SELECT
    id,
    normalized_key,
    CASE normalized_key
      WHEN 'cola' THEN 'Cola'
      WHEN 'cola_zero' THEN 'Cola zero'
      WHEN 'cola_kers' THEN 'Cola kers'
      WHEN 'cola_zero_kers' THEN 'Cola zero kers'
      WHEN 'cola_vanille' THEN 'Cola vanille'
      WHEN 'bloem' THEN 'Bloem'
      WHEN 'banaan' THEN 'Banaan'
      WHEN 'banaan_smaak' THEN 'Banaan smaak'
      WHEN 'paprika' THEN 'Paprika'
      WHEN 'paprika_smaak' THEN 'Paprika smaak'
      WHEN 'tomaat' THEN 'Tomaat'
      WHEN 'aardappel' THEN 'Aardappel'
      WHEN 'ui' THEN 'Ui'
      WHEN 'jonge_kaas' THEN 'Jonge kaas'
      ELSE NULL
    END AS normalized_name
  FROM keyed
)
UPDATE public.products p
SET canonical_key = named.normalized_key,
    canonical_name = COALESCE(named.normalized_name, NULLIF(btrim(p.canonical_name), '')),
    updated_at = now()
FROM named
WHERE named.id = p.id
  AND (
    p.canonical_key IS DISTINCT FROM named.normalized_key
    OR (
      named.normalized_name IS NOT NULL
      AND p.canonical_name IS DISTINCT FROM named.normalized_name
    )
  );
