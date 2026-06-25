-- Extend the catalog store domain to include SPAR and Aldi.
-- The original medallion schema (202606250003) constrained store to
-- ('ah','jumbo','dirk','plus'). The scraper fleet now also covers SPAR and Aldi,
-- so the bronze/silver CHECK constraints must accept them.

DO $$
DECLARE
  allowed text := 'store IN (''ah'',''jumbo'',''dirk'',''plus'',''spar'',''aldi'')';
BEGIN
  -- catalog.scrape_runs.store
  ALTER TABLE catalog.scrape_runs   DROP CONSTRAINT IF EXISTS scrape_runs_store_check;
  EXECUTE format('ALTER TABLE catalog.scrape_runs ADD CONSTRAINT scrape_runs_store_check CHECK (%s)', allowed);

  -- catalog.bronze_products.store
  ALTER TABLE catalog.bronze_products DROP CONSTRAINT IF EXISTS bronze_products_store_check;
  EXECUTE format('ALTER TABLE catalog.bronze_products ADD CONSTRAINT bronze_products_store_check CHECK (%s)', allowed);

  -- catalog.silver_products.store
  ALTER TABLE catalog.silver_products DROP CONSTRAINT IF EXISTS silver_products_store_check;
  EXECUTE format('ALTER TABLE catalog.silver_products ADD CONSTRAINT silver_products_store_check CHECK (%s)', allowed);
END $$;

-- Seed the two new stores into the app-facing store table (idempotent).
INSERT INTO public.stores (id, name, website_url) VALUES
  ('spar', 'SPAR', 'https://www.spar.nl'),
  ('aldi', 'ALDI', 'https://www.aldi.nl')
ON CONFLICT (id) DO NOTHING;

-- Allow trusted backend/catalog jobs using the Supabase service-role database
-- role to load and validate bronze scrape data without exposing catalog tables
-- to anon/authenticated PostgREST clients.
GRANT USAGE ON SCHEMA catalog TO service_role;
GRANT SELECT, INSERT, UPDATE ON catalog.scrape_runs TO service_role;
GRANT SELECT, INSERT ON catalog.bronze_products TO service_role;
