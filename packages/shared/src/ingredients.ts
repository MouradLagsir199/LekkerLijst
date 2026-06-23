import type { ParsedIngredient } from "./schemas";

type UnitInfo = {
  unit: "g" | "ml" | "piece" | "pack" | string;
  factor: number;
};

const UNIT_ALIASES: Record<string, UnitInfo> = {
  g: { unit: "g", factor: 1 },
  gram: { unit: "g", factor: 1 },
  grams: { unit: "g", factor: 1 },
  kg: { unit: "g", factor: 1000 },
  kilogram: { unit: "g", factor: 1000 },
  ml: { unit: "ml", factor: 1 },
  milliliter: { unit: "ml", factor: 1 },
  l: { unit: "ml", factor: 1000 },
  liter: { unit: "ml", factor: 1000 },
  stuk: { unit: "piece", factor: 1 },
  stuks: { unit: "piece", factor: 1 },
  piece: { unit: "piece", factor: 1 },
  pieces: { unit: "piece", factor: 1 },
  teen: { unit: "piece", factor: 1 },
  tenen: { unit: "piece", factor: 1 },
  pak: { unit: "pack", factor: 1 },
  pakken: { unit: "pack", factor: 1 },
  package: { unit: "pack", factor: 1 }
};

export type ShoppingListDraftItem = {
  ingredientName: string;
  normalizedIngredientName: string;
  dutchIngredientName: string | null;
  quantity: number | null;
  unit: string | null;
  rawTexts: string[];
};

export function normalizeIngredientName(value: string): string {
  return value
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^\p{L}\p{N}\s-]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function normalizeUnit(unit?: string | null): UnitInfo | null {
  if (!unit) return null;
  const key = unit.toLowerCase().trim().replace(/\.$/, "");
  return UNIT_ALIASES[key] ?? { unit: key, factor: 1 };
}

export function splitEditedLines(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

export function mergeParsedIngredients(
  ingredients: ParsedIngredient[],
  options: { includeOptional?: boolean } = {}
): ShoppingListDraftItem[] {
  const includeOptional = options.includeOptional ?? true;
  const merged = new Map<string, ShoppingListDraftItem>();

  for (const ingredient of ingredients) {
    if (ingredient.optional && !includeOptional) continue;

    const canonicalName = normalizeIngredientName(
      ingredient.dutchIngredientName ||
        ingredient.normalizedIngredientName ||
        ingredient.ingredientName
    );
    const unitInfo = normalizeUnit(ingredient.unit);
    const quantity =
      typeof ingredient.quantity === "number" && unitInfo
        ? ingredient.quantity * unitInfo.factor
        : ingredient.quantity ?? null;
    const normalizedUnit = unitInfo?.unit ?? ingredient.unit ?? null;
    const key = `${canonicalName}|${normalizedUnit ?? "no-unit"}`;
    const existing = merged.get(key);

    if (!existing) {
      merged.set(key, {
        ingredientName: ingredient.ingredientName,
        normalizedIngredientName: canonicalName,
        dutchIngredientName: ingredient.dutchIngredientName ?? null,
        quantity,
        unit: normalizedUnit,
        rawTexts: [ingredient.rawText]
      });
      continue;
    }

    existing.rawTexts.push(ingredient.rawText);
    if (existing.quantity !== null && quantity !== null) {
      existing.quantity += quantity;
    } else {
      existing.quantity = existing.quantity ?? quantity;
    }
  }

  return Array.from(merged.values());
}

export function formatQuantity(quantity: number | null, unit: string | null): string {
  if (quantity === null) return "";
  const rounded = Number.isInteger(quantity) ? quantity.toString() : quantity.toFixed(1);
  return unit ? `${rounded} ${unit}` : rounded;
}
