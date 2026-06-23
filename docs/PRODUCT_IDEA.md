# Dutch Recipe Importer + Supermarket Shopping Assistant

## Product Idea

Build a mobile-first recipe app for the Dutch market.

The core promise:

1. A user finds a recipe on social media, a blog, or in copied text.
2. The app imports the recipe.
3. AI turns messy text into a clean structured recipe.
4. Ingredients are normalized to Dutch supermarket terms.
5. The app matches ingredients to real AH, Jumbo, Dirk, and PLUS products.
6. The user gets a supermarket-ready shopping list with estimated prices.

The product should feel like a practical shopping assistant, not just a recipe database.

## Current Working Slice

Implemented first:

- Supabase Auth.
- Pasted text recipe import.
- Server-side OpenAI structured output parser via Supabase Edge Function.
- Recipe review and edit screen.
- Save recipe, ingredients, and instructions to Supabase.
- Seeded AH/Jumbo/Dirk/PLUS products.
- Local product matching through Postgres search.
- Scheduled ingredient-level shopping lists with selected products, estimated totals, and checkable items.
- Instagram/Facebook social-post imports through configured Apify actors.
- Instagram Reel transcript pass after caption/metadata extraction, without downloading or storing source media.
- Faithful incomplete-recipe state: missing quantities or steps stay visibly missing; optional AI completion remains a labelled proposal until the user accepts it.
- Source thumbnails on recipe cards, Dutch AI-generated recipe tags, platform attribution, and tag filtering.
- Week-by-week planning with editable lunch/dinner slots, imported recipes, and manual meals.
- Bronze/silver/gold catalog schema plus GitHub Actions ingestion and one-time OpenAI Batch categorization tooling.

Important rule:

- The product database is never sent to OpenAI. AI parses and normalizes recipe text; product matching happens locally.

## Near-Term Build Order

1. Improve matching quality.
   - Add ingredient aliases.
   - Add better Dutch normalization.
   - Store top product candidates, not only the selected match.
   - Let users replace a matched product.

2. Add cost controls.
   - Import cache.
   - AI usage logs.
   - Free-tier smart import counters.

3. Strengthen social link imports.
   - Link import uses public metadata, JSON-LD Recipe data, and TikTok/Pinterest oEmbed where available.
   - Pinterest follows the pin to its public source recipe page.
   - Instagram and Facebook post links use dedicated Apify actors for title/caption extraction before recipe parsing.
   - Instagram Reels and TikTok video links use transcript actors after caption/metadata extraction. Recipes remain incomplete when the combined source omits quantities or steps.
   - User-initiated share sheet for iOS and Android remains deferred.

4. Add paid product foundations.
   - RevenueCat entitlements.
   - Monthly smart import limits.
   - Paid import limits.

5. Add operations tooling.
   - Small admin app for failed imports, scrape runs, alias review, and match quality review.

6. Add combined shopping lists from planned meals.

## Deferred On Purpose

These are part of the larger product, but should not be built before the core slice is stable:

- Full admin app.
- RevenueCat payments.
- Social share sheet.
- Household sharing.
- Nutrition and diet calculations.
- Cart automation or in-app checkout.

## Current Technical Shape

- Mobile: Expo React Native with Expo Router.
- Backend slice: Supabase Auth, Postgres, RLS, Edge Function.
- AI: OpenAI structured output in `supabase/functions/import-recipe`.
- Shared logic: `packages/shared`.
- Existing scraper scripts remain available for later catalog work.

## Supabase Notes

Profile creation is handled two ways:

- Database trigger on `auth.users`.
- App-level profile upsert after login/session restore.

This makes profiles recoverable for users created before the trigger existed.

For development, if sign-up says it sent an email but no email arrives, either:

- Disable email confirmation in Supabase Auth settings while testing, or
- Configure SMTP in Supabase so confirmation emails are actually delivered.
