import type { ParsedRecipe } from "@recipe-nl/shared";

type ImportDraft = {
  recipe: ParsedRecipe;
  sourceText?: string;
  recipeId?: string;
};

let draft: ImportDraft | null = null;

export function setImportDraft(nextDraft: ImportDraft) {
  draft = nextDraft;
}

export function getImportDraft() {
  return draft;
}

export function clearImportDraft() {
  draft = null;
}
