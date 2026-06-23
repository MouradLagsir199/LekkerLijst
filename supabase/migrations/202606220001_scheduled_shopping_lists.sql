alter table public.shopping_lists
add column if not exists scheduled_for date;

alter table public.shopping_list_items
add column if not exists recipe_ingredient_id uuid references public.recipe_ingredients(id) on delete set null;

create index if not exists shopping_lists_user_scheduled_for_idx
on public.shopping_lists (user_id, scheduled_for);

create unique index if not exists shopping_list_items_recipe_ingredient_idx
on public.shopping_list_items (shopping_list_id, recipe_ingredient_id)
where recipe_ingredient_id is not null;
