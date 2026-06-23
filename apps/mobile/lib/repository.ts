import { selectLowestPricedRelevantProduct, type ParsedIngredient, type ParsedInstruction, type ParsedRecipe } from "@recipe-nl/shared";

type SupabaseLike = {
  from: (table: string) => any;
  rpc: (fn: string, args: Record<string, unknown>) => any;
};

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error && "message" in error && typeof error.message === "string") return error.message;
  return "Er ging iets mis. Probeer het opnieuw.";
}

export async function saveRecipe(input: {
  supabase: SupabaseLike;
  userId: string;
  recipeId?: string;
  recipe: ParsedRecipe;
  ingredients: ParsedIngredient[];
  instructions: ParsedInstruction[];
}) {
  const { supabase, userId, recipeId, recipe, ingredients, instructions } = input;

  const recipeValues = {
    user_id: userId,
    title: recipe.title,
    description: recipe.description,
    servings: recipe.servings,
    prep_time_minutes: recipe.prepTimeMinutes,
    cook_time_minutes: recipe.cookTimeMinutes,
    total_time_minutes: recipe.totalTimeMinutes,
    source_url: recipe.sourceUrl,
    source_platform: recipe.sourcePlatform,
    confidence_score: recipe.confidenceScore,
    image_url: recipe.imageUrl,
    tags: recipe.tags,
    completion_status: recipe.completeness.status,
    missing_fields: recipe.completeness.missingFields
  };

  const recipeRequest = recipeId
    ? supabase.from("recipes").update(recipeValues).eq("id", recipeId).select("id").single()
    : supabase.from("recipes").insert(recipeValues).select("id").single();
  const { data: recipeRow, error: recipeError } = await recipeRequest;

  if (recipeError) throw recipeError;

  if (recipeId) {
    const { error: deleteIngredientsError } = await supabase.from("recipe_ingredients").delete().eq("recipe_id", recipeRow.id);
    if (deleteIngredientsError) throw deleteIngredientsError;

    const { error: deleteInstructionsError } = await supabase.from("recipe_instructions").delete().eq("recipe_id", recipeRow.id);
    if (deleteInstructionsError) throw deleteInstructionsError;
  }

  const ingredientRows = ingredients.map((ingredient, index) => ({
    recipe_id: recipeRow.id,
    raw_text: ingredient.rawText,
    quantity: ingredient.quantity,
    unit: ingredient.unit,
    ingredient_name: ingredient.ingredientName,
    normalized_ingredient_name: ingredient.normalizedIngredientName,
    dutch_ingredient_name: ingredient.dutchIngredientName,
    preparation: ingredient.preparation,
    optional: ingredient.optional,
    ingredient_source: ingredient.ingredientSource,
    quantity_source: ingredient.quantitySource,
    sort_order: index
  }));

  const { error: ingredientsError } = await supabase.from("recipe_ingredients").insert(ingredientRows);
  if (ingredientsError) throw ingredientsError;

  const instructionRows = instructions.map((instruction, index) => ({
    recipe_id: recipeRow.id,
    step_number: index + 1,
    instruction: instruction.text,
    provenance: instruction.source
  }));

  const { error: instructionsError } = await supabase.from("recipe_instructions").insert(instructionRows);
  if (instructionsError) throw instructionsError;

  return {
    recipeId: recipeRow.id as string
  };
}

type RecipeIngredientForShopping = {
  id: string;
  ingredient_name: string;
  normalized_ingredient_name: string | null;
  dutch_ingredient_name: string | null;
  quantity: number | null;
  unit: string | null;
};

export async function addRecipeIngredientToScheduledShoppingList(input: {
  supabase: SupabaseLike;
  userId: string;
  ingredient: RecipeIngredientForShopping;
  scheduledFor: string;
  preferredStore?: string | null;
}) {
  const { supabase, userId, ingredient, scheduledFor, preferredStore } = input;
  const { data: existingList, error: existingListError } = await supabase
    .from("shopping_lists")
    .select("id,title")
    .eq("scheduled_for", scheduledFor)
    .order("created_at", { ascending: false })
    .limit(1)
    .maybeSingle();

  if (existingListError) throw existingListError;

  let shoppingListId = existingList?.id as string | undefined;
  if (!shoppingListId) {
    const { data: createdList, error: createListError } = await supabase
      .from("shopping_lists")
      .insert({
        user_id: userId,
        title: `Boodschappen ${scheduledFor}`,
        store_id: preferredStore ?? null,
        scheduled_for: scheduledFor,
        estimated_total_cents: 0
      })
      .select("id")
      .single();

    if (createListError) throw createListError;
    shoppingListId = createdList.id as string;
  }

  const { data: existingItem, error: existingItemError } = await supabase
    .from("shopping_list_items")
    .select("id")
    .eq("shopping_list_id", shoppingListId)
    .eq("recipe_ingredient_id", ingredient.id)
    .maybeSingle();

  if (existingItemError) throw existingItemError;
  if (existingItem) {
    return { shoppingListId, alreadyAdded: true };
  }

  const query = ingredient.dutch_ingredient_name || ingredient.normalized_ingredient_name || ingredient.ingredient_name;
  const { data: matches, error: matchError } = await supabase.rpc("search_products", {
    query_text: query,
    store_filter: null,
    match_count: 8
  });
  if (matchError) throw matchError;

  const match = Array.isArray(matches)
    ? selectLowestPricedRelevantProduct(
        matches.map((candidate: any) => ({
          ...candidate,
          currentPriceCents: candidate.current_price_cents,
          matchScore: candidate.match_score
        }))
      )
    : null;
  const { count, error: countError } = await supabase
    .from("shopping_list_items")
    .select("id", { count: "exact", head: true })
    .eq("shopping_list_id", shoppingListId);
  if (countError) throw countError;

  const { error: insertItemError } = await supabase.from("shopping_list_items").insert({
    shopping_list_id: shoppingListId,
    recipe_ingredient_id: ingredient.id,
    ingredient_name: ingredient.ingredient_name,
    normalized_ingredient_name: ingredient.normalized_ingredient_name,
    quantity: ingredient.quantity,
    unit: ingredient.unit,
    selected_product_id: match?.product_id ?? null,
    store_id: match?.store_id ?? preferredStore ?? null,
    estimated_price_cents: match?.current_price_cents ?? null,
    category: match?.category ?? null,
    checked: false,
    sort_order: count ?? 0
  });
  if (insertItemError) throw insertItemError;

  const { data: items, error: itemsError } = await supabase
    .from("shopping_list_items")
    .select("estimated_price_cents")
    .eq("shopping_list_id", shoppingListId);
  if (itemsError) throw itemsError;

  const estimatedTotalCents = (items ?? []).reduce(
    (total: number, item: { estimated_price_cents: number | null }) => total + (item.estimated_price_cents ?? 0),
    0
  );
  const { error: updateListError } = await supabase
    .from("shopping_lists")
    .update({ estimated_total_cents: estimatedTotalCents })
    .eq("id", shoppingListId);
  if (updateListError) throw updateListError;

  return { shoppingListId, alreadyAdded: false };
}
