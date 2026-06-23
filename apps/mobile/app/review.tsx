import type { ParsedIngredient, ParsedInstruction, ParsedRecipe } from "@recipe-nl/shared";
import { ParsedRecipeSchema } from "@recipe-nl/shared";
import * as ImagePicker from "expo-image-picker";
import { useRouter } from "expo-router";
import { AlertTriangle, Camera, CheckCircle2, Link2, Sparkles } from "lucide-react-native";
import { useMemo, useState, type ReactNode } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { Screen } from "../components/Screen";
import { RecipeImage } from "../components/RecipeImage";
import { useAuth } from "../lib/auth";
import { clearImportDraft, getImportDraft, setImportDraft } from "../lib/importDraft";
import { saveRecipe } from "../lib/repository";
import { supabase } from "../lib/supabase";
import { colors, radii, shadows, spacing, typography } from "../lib/theme";

export default function ReviewScreen() {
  const router = useRouter();
  const { session } = useAuth();
  const initialDraft = useMemo(() => getImportDraft(), []);
  const [recipe, setRecipe] = useState<ParsedRecipe | null>(initialDraft?.recipe ?? null);
  const [sourceText, setSourceText] = useState(initialDraft?.sourceText ?? "");
  const [recipeId, setRecipeId] = useState(initialDraft?.recipeId);
  const [bioUrl, setBioUrl] = useState("");
  const [aiProposal, setAiProposal] = useState(false);
  const [busy, setBusy] = useState<"save" | "proposal" | "bio" | null>(null);
  const [uploadingImage, setUploadingImage] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(markComplete: boolean) {
    if (!recipe || !session) return;

    const finalRecipe: ParsedRecipe = markComplete
      ? { ...recipe, completeness: { status: "complete", missingFields: [] } }
      : recipe;

    setBusy("save");
    setError(null);

    try {
      const result = await saveRecipe({
        supabase,
        userId: session.user.id,
        recipeId,
        recipe: finalRecipe,
        ingredients: finalRecipe.ingredients,
        instructions: finalRecipe.instructions
      });

      if (finalRecipe.completeness.status === "incomplete") {
        setRecipeId(result.recipeId);
        setImportDraft({ recipe: finalRecipe, sourceText, recipeId: result.recipeId });
      } else {
        clearImportDraft();
      }

      router.replace(`/recipe/${result.recipeId}`);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : String(saveError));
    } finally {
      setBusy(null);
    }
  }

  async function createAiProposal() {
    if (!recipe || !sourceText) return;

    setBusy("proposal");
    setError(null);
    const { data, error: invokeError } = await supabase.functions.invoke("import-recipe", {
      body: {
        action: "suggest_completion",
        recipe,
        sourceText,
        servings: recipe.servings
      }
    });
    setBusy(null);

    if (invokeError) {
      setError(await getFunctionErrorMessage(invokeError));
      return;
    }

    const parsed = ParsedRecipeSchema.safeParse(data?.recipe);
    if (!parsed.success) {
      setError(parsed.error.issues.map((issue) => issue.message).join(", "));
      return;
    }

    setRecipe(parsed.data);
    setAiProposal(true);
  }

  async function importBioLink() {
    if (!isHttpUrl(bioUrl)) {
      setError("Vul een geldige bio-link in.");
      return;
    }

    setBusy("bio");
    setError(null);
    const { data, error: invokeError } = await supabase.functions.invoke("import-recipe", {
      body: { sourceUrl: bioUrl.trim() }
    });
    setBusy(null);

    if (invokeError) {
      setError(await getFunctionErrorMessage(invokeError));
      return;
    }

    const parsed = ParsedRecipeSchema.safeParse(data?.recipe);
    if (!parsed.success) {
      setError(parsed.error.issues.map((issue) => issue.message).join(", "));
      return;
    }

    setRecipe(parsed.data);
    setSourceText(typeof data?.completionSourceText === "string" ? data.completionSourceText : "");
    setBioUrl("");
    setAiProposal(false);
  }

  async function pickRecipeImage() {
    if (!recipe || !session) return;
    setError(null);
    const permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
    if (!permission.granted) {
      setError("Geef fototoegang om een receptafbeelding toe te voegen.");
      return;
    }

    const result = await ImagePicker.launchImageLibraryAsync({
      mediaTypes: ["images"],
      quality: 0.82,
      allowsEditing: true,
      aspect: [4, 3]
    });
    if (result.canceled || !result.assets[0]) return;

    const asset = result.assets[0];
    setUploadingImage(true);
    try {
      const contentType = asset.mimeType || "image/jpeg";
      const extension = contentType.includes("png") ? "png" : contentType.includes("webp") ? "webp" : "jpg";
      const imageData = await fetch(asset.uri).then((response) => response.arrayBuffer());
      const storagePath = `${session.user.id}/${Date.now()}.${extension}`;
      const { error: uploadError } = await supabase.storage.from("recipe-images").upload(storagePath, imageData, { contentType, upsert: false });
      if (uploadError) throw uploadError;
      const { data } = supabase.storage.from("recipe-images").getPublicUrl(storagePath);
      updateRecipe({ imageUrl: data.publicUrl });
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : "Afbeelding uploaden mislukt.");
    } finally {
      setUploadingImage(false);
    }
  }

  function updateRecipe(values: Partial<ParsedRecipe>) {
    setRecipe((current) => (current ? { ...current, ...values } : current));
  }

  function updateIngredient(index: number, values: Partial<ParsedIngredient>) {
    setRecipe((current) => {
      if (!current) return current;
      const ingredients = [...current.ingredients];
      ingredients[index] = { ...ingredients[index], ...values };
      return { ...current, ingredients };
    });
  }

  function updateInstruction(index: number, values: Partial<ParsedInstruction>) {
    setRecipe((current) => {
      if (!current) return current;
      const instructions = [...current.instructions];
      instructions[index] = { ...instructions[index], ...values };
      return { ...current, instructions };
    });
  }

  if (!recipe) {
    return (
      <Screen>
        <Text style={styles.title}>Geen import gevonden</Text>
        <ActionButton label="Opnieuw importeren" onPress={() => router.replace("/import")} />
      </Screen>
    );
  }

  const incomplete = recipe.completeness.status === "incomplete";
  const hasMissingQuantities = recipe.ingredients.some((ingredient) => ingredient.quantity === null);
  const canMarkComplete =
    recipe.title.trim().length > 0 &&
    recipe.servings !== null &&
    recipe.servings !== undefined &&
    recipe.servings > 0 &&
    !hasMissingQuantities &&
    recipe.instructions.every((instruction) => instruction.text.trim().length > 0);
  const isSaving = busy === "save";

  return (
    <Screen>
      <Text style={styles.title}>Controleer je recept</Text>
      <Text style={styles.confidence}>AI vertrouwen: {Math.round(recipe.confidenceScore * 100)}%</Text>

      {incomplete ? (
        <View style={styles.incompletePanel}>
          <View style={styles.panelHeading}>
            <AlertTriangle color={colors.danger} size={22} strokeWidth={2.3} />
            <Text style={styles.panelTitle}>Recept incompleet</Text>
          </View>
          <Text style={styles.panelBody}>Ontbreekt: {formatMissingFields(recipe.completeness.missingFields)}.</Text>
          <View style={styles.panelActions}>
            <ActionButton
              busy={busy === "proposal"}
              disabled={!sourceText || busy !== null}
              icon={<Sparkles color={colors.surface} size={18} strokeWidth={2.4} />}
              label="AI-voorstel maken"
              onPress={createAiProposal}
            />
          </View>
          <TextInput
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
            onChangeText={setBioUrl}
            placeholder="Bio-link naar het recept"
            style={styles.input}
            value={bioUrl}
          />
          <ActionButton
            busy={busy === "bio"}
            disabled={busy !== null || !bioUrl.trim()}
            icon={<Link2 color={colors.primaryDark} size={18} strokeWidth={2.4} />}
            label="Bio-link importeren"
            onPress={importBioLink}
            variant="secondary"
          />
        </View>
      ) : null}

      {aiProposal ? (
        <View style={styles.proposalPanel}>
          <CheckCircle2 color={colors.primaryDark} size={22} strokeWidth={2.3} />
          <Text style={styles.proposalText}>AI-voorstel: controleer de gemarkeerde waarden voordat je opslaat.</Text>
        </View>
      ) : null}

      <View style={styles.imageSection}>
        <RecipeImage uri={recipe.imageUrl} style={styles.recipeImage} />
        <Pressable
          accessibilityRole="button"
          disabled={uploadingImage}
          onPress={pickRecipeImage}
          style={({ pressed }) => [styles.imageButton, uploadingImage && styles.disabled, pressed && !uploadingImage && styles.pressed]}
        >
          <Camera color={colors.primaryDark} size={18} strokeWidth={2.4} />
          <Text style={styles.imageButtonText}>{uploadingImage ? "Uploaden..." : recipe.imageUrl ? "Foto wijzigen" : "Foto toevoegen"}</Text>
        </Pressable>
      </View>

      <Text style={styles.label}>Titel</Text>
      <TextInput onChangeText={(title) => updateRecipe({ title })} style={styles.input} value={recipe.title} />

      <Text style={styles.label}>Porties</Text>
      <TextInput
        keyboardType="number-pad"
        onChangeText={(value) => updateRecipe({ servings: parsePositiveInteger(value) })}
        style={styles.input}
        value={recipe.servings?.toString() ?? ""}
      />

      <View style={styles.fieldHeader}>
        <Text style={styles.label}>Ingredienten</Text>
        {hasMissingQuantities ? <Text style={styles.missingLabel}>Hoeveelheden ontbreken</Text> : null}
      </View>
      <View style={styles.ingredientList}>
        {recipe.ingredients.map((ingredient, index) => (
          <View key={`${ingredient.ingredientName}-${index}`} style={styles.ingredientRow}>
            <View style={styles.ingredientFields}>
              <TextInput
                keyboardType="decimal-pad"
                onChangeText={(value) =>
                  updateIngredient(index, {
                    quantity: parseNumber(value),
                    quantitySource: value.trim() ? "source" : "missing",
                    rawText: buildRawText(parseNumber(value), ingredient.unit, ingredient.ingredientName)
                  })
                }
                placeholder="-"
                style={[styles.quantityInput, ingredient.quantitySource === "ai_suggestion" && styles.aiInput]}
                value={ingredient.quantity?.toString() ?? ""}
              />
              <TextInput
                onChangeText={(unit) =>
                  updateIngredient(index, {
                    unit: unit || null,
                    rawText: buildRawText(ingredient.quantity, unit || null, ingredient.ingredientName)
                  })
                }
                placeholder="eenheid"
                style={styles.unitInput}
                value={ingredient.unit ?? ""}
              />
              <TextInput
                onChangeText={(ingredientName) =>
                  updateIngredient(index, {
                    rawText: buildRawText(ingredient.quantity, ingredient.unit, ingredientName),
                    ingredientName,
                    normalizedIngredientName: ingredientName,
                    dutchIngredientName: ingredientName,
                    ingredientSource: "source"
                  })
                }
                style={[styles.ingredientInput, ingredient.ingredientSource === "ai_suggestion" && styles.aiInput]}
                value={ingredient.ingredientName}
              />
            </View>
            <ProvenanceLabel ingredient={ingredient} />
          </View>
        ))}
      </View>

      <Text style={styles.label}>Bereiding</Text>
      <View style={styles.instructionList}>
        {recipe.instructions.map((instruction, index) => (
          <View key={index} style={styles.instructionRow}>
            <Text style={styles.stepNumber}>{index + 1}</Text>
            <View style={styles.instructionInputWrap}>
              <TextInput
                multiline
                onChangeText={(text) => updateInstruction(index, { text, source: "source" })}
                style={[styles.instructionInput, instruction.source === "ai_suggestion" && styles.aiInput]}
                textAlignVertical="top"
                value={instruction.text}
              />
              {instruction.source === "ai_suggestion" ? <Text style={styles.aiLabel}>AI-voorstel</Text> : null}
            </View>
          </View>
        ))}
      </View>

      {error ? <Text style={styles.error}>{error}</Text> : null}
      {isSaving ? <ActivityIndicator color={colors.primaryDark} /> : null}

      {incomplete ? (
        <>
          <ActionButton busy={isSaving} disabled={busy !== null} label="Opslaan als concept" onPress={() => save(false)} />
          <ActionButton
            busy={isSaving}
            disabled={busy !== null || !canMarkComplete}
            icon={<CheckCircle2 color={colors.primaryDark} size={18} strokeWidth={2.4} />}
            label="Ik heb alles aangevuld"
            onPress={() => save(true)}
            variant="secondary"
          />
        </>
      ) : (
        <ActionButton
          busy={isSaving}
          disabled={busy !== null || !canMarkComplete}
          icon={aiProposal ? <CheckCircle2 color={colors.surface} size={18} strokeWidth={2.4} /> : undefined}
          label={aiProposal ? "AI-voorstel accepteren en opslaan" : "Recept opslaan"}
          onPress={() => save(true)}
        />
      )}
    </Screen>
  );
}

function ProvenanceLabel({ ingredient }: { ingredient: ParsedIngredient }) {
  if (ingredient.quantitySource === "missing") return <Text style={styles.missingLabel}>Hoeveelheid ontbreekt</Text>;
  if (ingredient.quantitySource === "ai_suggestion" || ingredient.ingredientSource === "ai_suggestion") {
    return <Text style={styles.aiLabel}>AI-voorstel</Text>;
  }
  return null;
}

function ActionButton({
  label,
  onPress,
  icon,
  variant = "primary",
  disabled,
  busy
}: {
  label: string;
  onPress: () => void;
  icon?: ReactNode;
  variant?: "primary" | "secondary";
  disabled?: boolean;
  busy?: boolean;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled || busy}
      onPress={onPress}
      style={({ pressed }) => [
        styles.actionButton,
        variant === "secondary" && styles.actionButtonSecondary,
        (disabled || busy) && styles.disabled,
        pressed && !disabled && !busy && styles.pressed
      ]}
    >
      {busy ? <ActivityIndicator color={variant === "primary" ? colors.surface : colors.primaryDark} size="small" /> : icon}
      <Text style={[styles.actionButtonText, variant === "secondary" && styles.actionButtonTextSecondary]}>{label}</Text>
    </Pressable>
  );
}

function parseNumber(value: string) {
  const normalized = value.trim().replace(",", ".");
  if (!normalized) return null;
  const number = Number(normalized);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function parsePositiveInteger(value: string) {
  const number = Number(value.trim());
  return Number.isInteger(number) && number > 0 ? number : null;
}

function buildRawText(quantity: number | null | undefined, unit: string | null | undefined, ingredientName: string) {
  return [quantity ?? "", unit ?? "", ingredientName].filter(Boolean).join(" ");
}

function formatMissingFields(fields: string[]) {
  const labels = {
    quantities: "hoeveelheden",
    ingredients: "ingredienten",
    instructions: "bereidingsstappen",
    servings: "porties"
  } as const;
  return fields.map((field) => labels[field as keyof typeof labels] ?? field).join(", ");
}

function isHttpUrl(value: string) {
  try {
    const url = new URL(value.trim());
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

async function getFunctionErrorMessage(error: unknown) {
  const fallback = error instanceof Error ? error.message : "Importeren mislukt";
  const context = (error as { context?: unknown })?.context;

  if (context && typeof (context as Response).json === "function") {
    try {
      const payload = (await (context as Response).json()) as { error?: unknown; details?: unknown };
      const message = typeof payload.error === "string" ? payload.error : null;
      const details = typeof payload.details === "string" ? payload.details : null;
      return [message, details].filter(Boolean).join(": ") || fallback;
    } catch {
      return fallback;
    }
  }

  return fallback;
}

const styles = StyleSheet.create({
  title: {
    color: colors.text,
    ...typography.sectionTitle
  },
  confidence: {
    color: colors.muted,
    ...typography.label
  },
  incompletePanel: {
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: "#fecaca",
    backgroundColor: "#fff7f7",
    gap: spacing.sm,
    padding: spacing.md
  },
  panelHeading: {
    alignItems: "center",
    flexDirection: "row",
    gap: spacing.sm
  },
  panelTitle: {
    color: colors.text,
    ...typography.cardTitle
  },
  panelBody: {
    color: colors.muted,
    ...typography.body
  },
  panelActions: {
    marginTop: spacing.xs
  },
  proposalPanel: {
    borderLeftWidth: 3,
    borderLeftColor: colors.primary,
    backgroundColor: colors.primarySoft,
    flexDirection: "row",
    alignItems: "flex-start",
    gap: spacing.sm,
    padding: spacing.md
  },
  proposalText: {
    flex: 1,
    color: colors.text,
    ...typography.label
  },
  imageSection: {
    gap: spacing.sm
  },
  recipeImage: {
    width: "100%",
    height: 196,
    borderRadius: radii.md
  },
  imageButton: {
    alignSelf: "flex-start",
    minHeight: 38,
    borderRadius: radii.sm,
    backgroundColor: colors.primarySoft,
    alignItems: "center",
    flexDirection: "row",
    gap: 7,
    justifyContent: "center",
    paddingHorizontal: spacing.md
  },
  imageButtonText: {
    color: colors.primaryDark,
    fontSize: 13,
    lineHeight: 17,
    fontWeight: "800"
  },
  label: {
    color: colors.text,
    ...typography.label
  },
  fieldHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.sm
  },
  input: {
    minHeight: 48,
    borderRadius: radii.sm,
    borderColor: colors.border,
    borderWidth: 1,
    backgroundColor: colors.surface,
    paddingHorizontal: spacing.sm,
    fontSize: 16
  },
  ingredientList: {
    gap: spacing.sm
  },
  ingredientRow: {
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    gap: spacing.xs,
    padding: spacing.sm,
    ...shadows.soft
  },
  ingredientFields: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs
  },
  quantityInput: {
    width: 58,
    minHeight: 42,
    borderRadius: radii.sm,
    borderColor: colors.border,
    borderWidth: 1,
    paddingHorizontal: 8,
    textAlign: "center",
    fontSize: 15
  },
  unitInput: {
    width: 78,
    minHeight: 42,
    borderRadius: radii.sm,
    borderColor: colors.border,
    borderWidth: 1,
    paddingHorizontal: 8,
    fontSize: 14
  },
  ingredientInput: {
    flex: 1,
    minHeight: 42,
    borderRadius: radii.sm,
    borderColor: colors.border,
    borderWidth: 1,
    paddingHorizontal: 8,
    fontSize: 15
  },
  aiInput: {
    borderColor: colors.primaryDark,
    backgroundColor: colors.primarySoft
  },
  missingLabel: {
    color: colors.danger,
    fontSize: 12,
    lineHeight: 16,
    fontWeight: "800"
  },
  aiLabel: {
    color: colors.primaryDark,
    fontSize: 12,
    lineHeight: 16,
    fontWeight: "800"
  },
  instructionList: {
    gap: spacing.sm
  },
  instructionRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: spacing.sm
  },
  stepNumber: {
    width: 26,
    height: 26,
    borderRadius: radii.pill,
    backgroundColor: colors.primarySoft,
    color: colors.primaryDark,
    overflow: "hidden",
    textAlign: "center",
    paddingTop: 4,
    fontSize: 13,
    lineHeight: 17,
    fontWeight: "800"
  },
  instructionInputWrap: {
    flex: 1,
    gap: 3
  },
  instructionInput: {
    minHeight: 74,
    borderRadius: radii.sm,
    borderColor: colors.border,
    borderWidth: 1,
    padding: spacing.sm,
    color: colors.text,
    fontSize: 15,
    lineHeight: 21
  },
  actionButton: {
    minHeight: 48,
    borderRadius: radii.sm,
    backgroundColor: colors.primaryDark,
    alignItems: "center",
    justifyContent: "center",
    flexDirection: "row",
    gap: spacing.xs,
    paddingHorizontal: spacing.md
  },
  actionButtonSecondary: {
    borderWidth: 1,
    borderColor: colors.primary,
    backgroundColor: colors.primarySoft
  },
  actionButtonText: {
    color: colors.surface,
    fontSize: 15,
    lineHeight: 20,
    fontWeight: "800"
  },
  actionButtonTextSecondary: {
    color: colors.primaryDark
  },
  disabled: {
    opacity: 0.5
  },
  pressed: {
    opacity: 0.78
  },
  error: {
    color: colors.danger,
    fontWeight: "700"
  }
});
