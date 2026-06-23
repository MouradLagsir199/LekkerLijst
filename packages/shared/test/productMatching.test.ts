import { describe, expect, it } from "vitest";
import { scoreProductForIngredient } from "../src/productMatching";

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
});
