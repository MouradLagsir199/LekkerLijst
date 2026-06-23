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

export type ScoredProductCandidate = {
  currentPriceCents: number | null;
  matchScore: number | null;
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

/**
 * Picks the cheapest product only after excluding loosely-related fuzzy matches.
 * The database returns candidates ordered by relevance; this keeps options within
 * a small score band of the best result before comparing their shelf prices.
 */
export function selectLowestPricedRelevantProduct<T extends ScoredProductCandidate>(candidates: T[]): T | null {
  const valid = candidates.filter((candidate) => Number.isFinite(candidate.matchScore) && Number.isFinite(candidate.currentPriceCents));
  if (!valid.length) return null;

  const bestScore = Math.max(...valid.map((candidate) => candidate.matchScore ?? 0));
  const relevanceFloor = Math.max(0.35, bestScore - 0.15);
  const relevant = valid.filter((candidate) => (candidate.matchScore ?? 0) >= relevanceFloor);

  return [...relevant].sort(
    (left, right) =>
      (left.currentPriceCents ?? Number.MAX_SAFE_INTEGER) - (right.currentPriceCents ?? Number.MAX_SAFE_INTEGER) ||
      (right.matchScore ?? 0) - (left.matchScore ?? 0)
  )[0] ?? null;
}
