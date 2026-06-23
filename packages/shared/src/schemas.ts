import { z } from "zod";

export const SourcePlatformSchema = z.enum([
  "instagram",
  "tiktok",
  "youtube",
  "facebook",
  "pinterest",
  "blog",
  "manual"
]);

export const FieldSourceSchema = z.enum(["source", "ai_suggestion"]);
export const QuantitySourceSchema = z.enum(["source", "missing", "ai_suggestion"]);
export const MissingRecipeFieldSchema = z.enum(["quantities", "ingredients", "instructions", "servings"]);

export const RecipeCompletenessSchema = z.object({
  status: z.enum(["complete", "incomplete"]),
  missingFields: z.array(MissingRecipeFieldSchema).default([])
});

export const ParsedIngredientSchema = z.object({
  rawText: z.string().min(1),
  quantity: z.number().nullable().optional(),
  unit: z.string().nullable().optional(),
  ingredientName: z.string().min(1),
  normalizedIngredientName: z.string().nullable().optional(),
  dutchIngredientName: z.string().nullable().optional(),
  preparation: z.string().nullable().optional(),
  optional: z.boolean().default(false),
  ingredientSource: FieldSourceSchema.default("source"),
  quantitySource: QuantitySourceSchema.default("source")
});

export const ParsedInstructionSchema = z.object({
  text: z.string().min(1),
  source: FieldSourceSchema.default("source")
});

export const ParsedRecipeSchema = z.object({
  title: z.string().min(1),
  description: z.string().nullable().optional(),
  servings: z.number().int().positive().nullable().optional(),
  prepTimeMinutes: z.number().int().nonnegative().nullable().optional(),
  cookTimeMinutes: z.number().int().nonnegative().nullable().optional(),
  totalTimeMinutes: z.number().int().nonnegative().nullable().optional(),
  ingredients: z.array(ParsedIngredientSchema).min(1),
  instructions: z.array(ParsedInstructionSchema).min(1),
  tags: z.array(z.string().trim().min(1).max(32)).max(8).default([]),
  imageUrl: z.string().url().nullable().optional(),
  sourceUrl: z.string().url().nullable().optional(),
  sourcePlatform: SourcePlatformSchema.nullable().optional(),
  confidenceScore: z.number().min(0).max(1),
  completeness: RecipeCompletenessSchema
});

export type SourcePlatform = z.infer<typeof SourcePlatformSchema>;
export type FieldSource = z.infer<typeof FieldSourceSchema>;
export type QuantitySource = z.infer<typeof QuantitySourceSchema>;
export type RecipeCompleteness = z.infer<typeof RecipeCompletenessSchema>;
export type ParsedIngredient = z.infer<typeof ParsedIngredientSchema>;
export type ParsedInstruction = z.infer<typeof ParsedInstructionSchema>;
export type ParsedRecipe = z.infer<typeof ParsedRecipeSchema>;

export type ProductMatch = {
  productId: string;
  storeId: "ah" | "jumbo" | "dirk" | "plus";
  productName: string;
  brand: string | null;
  category: string | null;
  packageSizeText: string | null;
  currentPriceCents: number;
  imageUrl: string | null;
  productUrl: string | null;
  matchScore: number;
};
