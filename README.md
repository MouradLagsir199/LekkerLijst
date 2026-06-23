# Dutch Recipe Importer

Focused vertical slice:

1. Supabase Auth
2. Import pasted recipe text
3. Parse with OpenAI Structured Outputs in a Supabase Edge Function
4. Review and edit the parsed recipe
5. Save recipe, ingredients, and instructions
6. Match ingredients to seeded Dutch supermarket products
7. Schedule ingredient-level shopping lists and match them to the lowest-priced relevant product
8. Filter recipes by AI-generated Dutch tags and plan meals week by week

The existing supermarket scraper scripts are intentionally left in place for the later catalog pipeline.

See `docs/PRODUCT_IDEA.md` for the full product direction and build order.
See `ui.md` for app UI rules, colors, typography, tabs, and feature-state conventions.

## Setup

1. Create a Supabase project.
2. Apply all SQL migrations in `supabase/migrations/`.
3. Deploy the Edge Function in `supabase/functions/import-recipe`.
4. Set Edge Function secrets:

```bash
supabase secrets set OPENAI_API_KEY=...
supabase secrets set OPENAI_RECIPE_MODEL=gpt-5.4-mini
supabase secrets set APIFY_API_TOKEN=... # required for social post/transcript imports
```

5. Copy `apps/mobile/.env.example` to `apps/mobile/.env` and fill the Expo public Supabase values.
6. Install dependencies and run tests:

```bash
npm install
npm test
```

7. Start the mobile app:

```bash
npm run start --workspace apps/mobile
```

## Notes

- Product matching starts with seeded AH/Jumbo/Dirk/PLUS products and a Postgres `pg_trgm` search function. The default selected product is the lowest-priced relevant option; every item can show stores and variants.
- Link import reads public page metadata, JSON-LD Recipe data, and available TikTok/Pinterest oEmbed metadata. Pinterest follows a pin to its original recipe page and uses a dedicated Pin actor for the correct card thumbnail, direct video metadata, and native captions. If the written source is incomplete, a public Pinterest video can be transcribed only when it is at most five minutes; media is never stored. Instagram/Facebook post links use configured Apify actors for post captions/text. Instagram Reels and TikTok video links add a transcript pass after metadata; when the lower-cost Instagram transcript actor cannot resolve a Reel's audio, the cross-platform actor is used automatically. Detected Facebook video links use the same fallback route.
- Imports stay faithful to their source. Missing quantities, servings, ingredients, or cut-off steps result in an explicit incomplete recipe concept. AI can create a clearly labelled completion proposal, which the user must review and accept; incomplete concepts cannot add items to a shopping list.
- Source thumbnails are saved as recipe-card images when a source exposes them. Imports generate Dutch titles, descriptions, ingredients, preparation text, and filter tags.
- Recipe ingredients are added individually to a shopping list scheduled for a chosen day. The Boodschappen tab includes upcoming lists and a calendar; product check-off happens only in a list detail screen. Planning supports editable week-by-week lunch and dinner slots using saved recipes or manual meals.

## Catalog Pipeline

The store-specific extractors remain in `AH/`, `Jumbo/`, `Dirk/`, and `Plus/`. `scrapers/` adds a production data path:

1. Existing scraper CSV -> immutable Storage artifact plus `scrape_runs`, `bronze_artifacts`, and `bronze_products`.
2. Standardized store columns -> `silver_products` with current price, availability, package, image, and promotion fields.
3. One OpenAI Batch categorization -> `canonical_ingredients`, aliases, mapping provenance, and final `products` used by the app.

After the gold Batch has been applied, run the manual `Catalog Match Evaluation` workflow. It verifies representative Dutch ingredients against the live `search_products` RPC, confirms the auto-selection is the lowest-priced product within the strongest relevance band, and uses one structured AI review of only those visible candidates. Both reports are retained as the workflow artifact.

`Catalog Ingest` runs daily in GitHub Actions and can be started for one store. `Catalog Gold Categorization` is intentionally manual because it creates one paid Batch API job. Before enabling those workflows, add these GitHub repository secrets:

```bash
gh secret set SUPABASE_URL --repo MouradLagsir199/LekkerLijst
gh secret set SUPABASE_SERVICE_ROLE_KEY --repo MouradLagsir199/LekkerLijst
gh secret set OPENAI_API_KEY --repo MouradLagsir199/LekkerLijst
```

The service-role key is used only by CI to write bronze/silver/gold catalog data; it is never exposed to the mobile app.
