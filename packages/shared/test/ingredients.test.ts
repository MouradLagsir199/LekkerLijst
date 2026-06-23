import { describe, expect, it } from "vitest";
import { mergeParsedIngredients, splitEditedLines } from "../src/ingredients";
import type { ParsedIngredient } from "../src/schemas";

describe("ingredient utilities", () => {
  it("merges duplicate weights after unit conversion", () => {
    const ingredients: ParsedIngredient[] = [
      {
        rawText: "200 g bloem",
        quantity: 200,
        unit: "g",
        ingredientName: "bloem",
        normalizedIngredientName: "bloem",
        dutchIngredientName: "bloem",
        optional: false,
        ingredientSource: "source",
        quantitySource: "source"
      },
      {
        rawText: "0.1 kg bloem",
        quantity: 0.1,
        unit: "kg",
        ingredientName: "flour",
        normalizedIngredientName: "flour",
        dutchIngredientName: "bloem",
        optional: false,
        ingredientSource: "source",
        quantitySource: "source"
      }
    ];

    const [item] = mergeParsedIngredients(ingredients);

    expect(item.quantity).toBe(300);
    expect(item.unit).toBe("g");
    expect(item.normalizedIngredientName).toBe("bloem");
  });

  it("keeps incompatible units separate", () => {
    const ingredients: ParsedIngredient[] = [
      {
        rawText: "2 eieren",
        quantity: 2,
        unit: "stuks",
        ingredientName: "eieren",
        normalizedIngredientName: "eieren",
        optional: false,
        ingredientSource: "source",
        quantitySource: "source"
      },
      {
        rawText: "100 g eiwit",
        quantity: 100,
        unit: "g",
        ingredientName: "eiwit",
        normalizedIngredientName: "eiwit",
        optional: false,
        ingredientSource: "source",
        quantitySource: "source"
      }
    ];

    expect(mergeParsedIngredients(ingredients)).toHaveLength(2);
  });

  it("splits review text into clean lines", () => {
    expect(splitEditedLines("  melk  \n\n bloem\r\n")).toEqual(["melk", "bloem"]);
  });
});
