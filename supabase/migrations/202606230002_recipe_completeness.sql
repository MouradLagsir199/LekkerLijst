alter table public.recipes
add column if not exists completion_status text not null default 'complete'
check (completion_status in ('complete', 'incomplete'));

alter table public.recipes
add column if not exists missing_fields text[] not null default '{}';

alter table public.recipe_ingredients
add column if not exists ingredient_source text not null default 'source'
check (ingredient_source in ('source', 'ai_suggestion'));

alter table public.recipe_ingredients
add column if not exists quantity_source text not null default 'source'
check (quantity_source in ('source', 'missing', 'ai_suggestion'));

alter table public.recipe_instructions
add column if not exists provenance text not null default 'source'
check (provenance in ('source', 'ai_suggestion'));
