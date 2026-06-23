import { normalizeIngredientName } from "./ingredients";

export type RankableProduct = {
  id: string;
  storeId: string;
  name: string;
  brand?: string | null;
  category?: string | null;
  currentPriceCents: number;
  isAvailable: boolean;
};

export function tokenOverlapScore(query: string, productName: string): number {
  const queryTokens = new Set(normalizeIngredientName(query).split(" ").filter(Boolean));
  const productTokens = new Set(normalizeIngredientName(productName).split(" ").filter(Boolean));
  if (queryTokens.size === 0) return 0;

  let hits = 0;
  for (const token of queryTokens) {
    if (productTokens.has(token)) hits += 1;
  }

  return hits / queryTokens.size;
}

export function scoreProductForIngredient(input: {
  query: string;
  product: RankableProduct;
  preferredStore?: string | null;
}): number {
  const { query, product, preferredStore } = input;
  let score = tokenOverlapScore(query, product.name);

  if (product.category && tokenOverlapScore(query, product.category) > 0) {
    score += 0.15;
  }

  if (preferredStore && product.storeId === preferredStore) {
    score += 0.1;
  }

  if (!product.isAvailable) {
    score -= 0.5;
  }

  return Math.max(0, Number(score.toFixed(4)));
}
