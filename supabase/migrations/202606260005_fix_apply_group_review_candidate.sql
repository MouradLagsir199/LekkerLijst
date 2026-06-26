DROP FUNCTION IF EXISTS catalog.apply_group_review_candidate(uuid, text);

CREATE OR REPLACE FUNCTION catalog.apply_group_review_candidate(
  p_candidate_id uuid,
  p_action text
)
RETURNS TABLE(status text, rows_affected integer)
LANGUAGE plpgsql
AS $$
DECLARE
  candidate catalog.group_review_candidates%ROWTYPE;
  group_id uuid;
  affected int := 0;
BEGIN
  SELECT c.*
    INTO candidate
    FROM catalog.group_review_candidates c
   WHERE c.id = p_candidate_id
   FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Unknown group review candidate: %', p_candidate_id;
  END IF;

  IF p_action NOT IN ('approve','reject','needs_later') THEN
    RAISE EXCEPTION 'Unsupported review action: %', p_action;
  END IF;

  IF p_action = 'reject' THEN
    UPDATE catalog.group_review_candidates c
       SET status = 'rejected',
           reviewed_at = now(),
           updated_at = now()
     WHERE c.id = p_candidate_id;
    RETURN QUERY VALUES ('rejected'::text, 0);
    RETURN;
  END IF;

  IF p_action = 'needs_later' THEN
    UPDATE catalog.group_review_candidates c
       SET status = 'needs_later',
           reviewed_at = now(),
           updated_at = now()
     WHERE c.id = p_candidate_id;
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
    WHERE m.candidate_id = p_candidate_id
    ON CONFLICT (silver_product_id) DO UPDATE SET
      exact_product_group_id = EXCLUDED.exact_product_group_id,
      source = EXCLUDED.source,
      confidence = EXCLUDED.confidence;

    UPDATE public.products pp
       SET exact_product_group_id = group_id,
           updated_at = now()
      FROM catalog.group_review_candidate_members m
     WHERE m.candidate_id = p_candidate_id
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
    WHERE m.candidate_id = p_candidate_id
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
    WHERE m.candidate_id = p_candidate_id
    ON CONFLICT (silver_product_id, ingredient_id) DO UPDATE SET
      confidence = EXCLUDED.confidence,
      mapping_source = EXCLUDED.mapping_source,
      review_status = 'approved';

    UPDATE public.products pp
       SET ingredient_id = group_id,
           updated_at = now()
      FROM catalog.group_review_candidate_members m
     WHERE m.candidate_id = p_candidate_id
       AND m.silver_product_id = pp.silver_product_id
       AND pp.ingredient_id IS DISTINCT FROM group_id;
    GET DIAGNOSTICS affected = ROW_COUNT;
  END IF;

  UPDATE catalog.group_review_candidates c
     SET status = 'approved',
         reviewed_at = now(),
         updated_at = now()
   WHERE c.id = p_candidate_id;

  RETURN QUERY VALUES ('approved'::text, affected);
END;
$$;

GRANT EXECUTE ON FUNCTION catalog.apply_group_review_candidate(uuid, text) TO service_role;
