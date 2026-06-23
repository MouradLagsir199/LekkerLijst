import { describe, expect, it } from "vitest";
import { filterRelevantProductCandidates, scoreProductForIngredient, selectLowestPricedRelevantProduct } from "../src/productMatching";

describe("product matching", () => {
  it("rewards lexical overlap and preferred store", () => {
    const ahScore = scoreProductForIngredient({
      query: "halfvolle melk",
      preferredStore: "ah",
      product: {
        id: "1",
        storeId: "ah",
        name: "Zaanse Hoeve Halfvolle melk",
        category: "Zuivel",
        currentPriceCents: 119,
        isAvailable: true
      }
    });
    const otherScore = scoreProductForIngredient({
      query: "halfvolle melk",
      preferredStore: "ah",
      product: {
        id: "2",
        storeId: "jumbo",
        name: "Jumbo Halfvolle melk",
        category: "Zuivel",
        currentPriceCents: 125,
        isAvailable: true
      }
    });

    expect(ahScore).toBeGreaterThan(otherScore);
  });

  it("penalizes unavailable products", () => {
    const score = scoreProductForIngredient({
      query: "bloem",
      product: {
        id: "1",
        storeId: "dirk",
        name: "Patentbloem",
        category: "Bakken",
        currentPriceCents: 99,
        isAvailable: false
      }
    });

    expect(score).toBeLessThan(1);
  });

  it("chooses the lowest price only among close relevance matches", () => {
    const selected = selectLowestPricedRelevantProduct([
      { currentPriceCents: 349, matchScore: 1.08, name: "kipfilet" },
      { currentPriceCents: 299, matchScore: 0.98, name: "kipfilet aanbieding" },
      { currentPriceCents: 119, matchScore: 0.44, name: "kippenbouillon" }
    ]);

    expect(selected?.name).toBe("kipfilet aanbieding");
  });

  it("returns no automatic match without a score and shelf price", () => {
    expect(selectLowestPricedRelevantProduct([{ currentPriceCents: null, matchScore: 0.9 }])).toBeNull();
  });

  it("hides weak fuzzy matches from visible store variants", () => {
    const visible = filterRelevantProductCandidates([
      { id: "kipfilet", currentPriceCents: 420, matchScore: 2.1 },
      { id: "boter", currentPriceCents: 135, matchScore: 0.44 }
    ]);

    expect(visible.map((candidate) => candidate.id)).toEqual(["kipfilet"]);
  });
});
