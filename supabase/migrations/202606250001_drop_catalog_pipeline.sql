-- Drop catalog pipeline: functions, tables, storage bucket.
-- The product matching pipeline (scrape → bronze → silver → gold → products) is being
-- rebuilt from scratch. This migration wipes every artifact the old pipeline created.

-- Functions (all known overloads)
DROP FUNCTION IF EXISTS search_products(text, text, integer);
DROP FUNCTION IF EXISTS search_products(text, integer, integer);
DROP FUNCTION IF EXISTS search_products(text, integer);

-- Junction / derived tables first (FK dependents)
DROP TABLE IF EXISTS product_canonical_ingredients CASCADE;
DROP TABLE IF EXISTS ingredient_aliases CASCADE;
DROP TABLE IF EXISTS recipe_product_matches CASCADE;

-- Catalog tables
DROP TABLE IF EXISTS canonical_ingredients CASCADE;
DROP TABLE IF EXISTS silver_products CASCADE;
DROP TABLE IF EXISTS bronze_products CASCADE;
DROP TABLE IF EXISTS bronze_artifacts CASCADE;
DROP TABLE IF EXISTS scrape_runs CASCADE;

-- Core product tables
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS stores CASCADE;

-- Remove orphaned FK columns on shopping_list_items (referenced products which is now gone)
ALTER TABLE shopping_list_items
  DROP COLUMN IF EXISTS selected_product_id,
  DROP COLUMN IF EXISTS estimated_price_cents;

-- Storage bucket: cannot be deleted via SQL (storage.protect_delete() blocks it).
-- Delete the 'catalog-bronze' bucket manually via the Supabase dashboard:
-- Storage → catalog-bronze → Settings → Delete bucket.
