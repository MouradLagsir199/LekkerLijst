import { formatQuantity } from "@recipe-nl/shared";
import DateTimePicker from "@react-native-community/datetimepicker";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { AlertTriangle, CalendarDays, Check, Pencil, Plus, ShoppingCart, Trash2 } from "lucide-react-native";
import { useMemo, useState } from "react";
import { ActivityIndicator, Alert, Platform, Pressable, StyleSheet, Text, View } from "react-native";

import { Button } from "../../components/Button";
import { RecipeImage } from "../../components/RecipeImage";
import { Screen } from "../../components/Screen";
import { useAuth } from "../../lib/auth";
import { getImportDraft } from "../../lib/importDraft";
import { addRecipeIngredientToScheduledShoppingList, errorMessage } from "../../lib/repository";
import { supabase } from "../../lib/supabase";
import { colors, radii, shadows, spacing, typography } from "../../lib/theme";

type RecipeIngredient = {
  id: string;
  raw_text: string;
  quantity: number | null;
  unit: string | null;
  ingredient_name: string;
  normalized_ingredient_name: string | null;
  dutch_ingredient_name: string | null;
  sort_order: number;
};

export default function RecipeDetailScreen() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { session } = useAuth();
  const params = useLocalSearchParams<{ id: string }>();
  const recipeId = params.id;
  const days = useMemo(() => getUpcomingDays(), []);
  const [scheduledFor, setScheduledFor] = useState(days[0].key);
  const [datePickerOpen, setDatePickerOpen] = useState(false);
  const [addingIngredientId, setAddingIngredientId] = useState<string | null>(null);
  const [addedIngredientKeys, setAddedIngredientKeys] = useState<string[]>([]);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const recipeQuery = useQuery({
    queryKey: ["recipe", recipeId],
    enabled: Boolean(recipeId),
    queryFn: async () => {
      const { data, error: queryError } = await supabase
        .from("recipes")
        .select(
          "id,title,description,servings,image_url,tags,source_platform,completion_status,missing_fields,recipe_ingredients(id,raw_text,quantity,unit,ingredient_name,normalized_ingredient_name,dutch_ingredient_name,sort_order),recipe_instructions(id,step_number,instruction)"
        )
        .eq("id", recipeId)
        .single();
      if (queryError) throw queryError;
      return data as any;
    }
  });

  async function addIngredient(ingredient: RecipeIngredient) {
    if (!session) return;
    if (recipeQuery.data?.completion_status !== "complete") {
      setError("Maak dit recept eerst compleet voordat je boodschappen toevoegt.");
      return;
    }

    setAddingIngredientId(ingredient.id);
    setFeedback(null);
    setError(null);

    try {
      const result = await addRecipeIngredientToScheduledShoppingList({
        supabase,
        userId: session.user.id,
        ingredient,
        scheduledFor
      });

      const ingredientKey = getIngredientKey(scheduledFor, ingredient.id);
      setAddedIngredientKeys((current) => [...new Set([...current, ingredientKey])]);
      setFeedback(
        result.alreadyAdded
          ? `${ingredient.ingredient_name} staat al op de lijst van ${formatDate(scheduledFor)}.`
          : `${ingredient.ingredient_name} is toegevoegd voor ${formatDate(scheduledFor)}.`
      );
      await queryClient.invalidateQueries({ queryKey: ["shopping-lists-tab"] });
    } catch (addError) {
      setError(errorMessage(addError));
    } finally {
      setAddingIngredientId(null);
    }
  }

  if (recipeQuery.isLoading) {
    return (
      <Screen style={styles.center}>
        <Stack.Screen options={{ title: "Recept" }} />
        <ActivityIndicator color={colors.primaryDark} />
      </Screen>
    );
  }

  if (recipeQuery.error || !recipeQuery.data) {
    return (
      <Screen>
        <Stack.Screen options={{ title: "Recept" }} />
        <Text style={styles.error}>{String(recipeQuery.error?.message ?? "Recept niet gevonden")}</Text>
        <Button onPress={() => router.replace("/")} title="Terug" />
      </Screen>
    );
  }

  const recipe = recipeQuery.data;
  const incomplete = recipe.completion_status !== "complete";
  const editableDraft = getImportDraft()?.recipeId === recipeId;
  const ingredients = [...(recipe.recipe_ingredients ?? [])].sort((a, b) => a.sort_order - b.sort_order) as RecipeIngredient[];
  const instructions = [...(recipe.recipe_instructions ?? [])].sort((a, b) => a.step_number - b.step_number);

  function confirmDelete() {
    Alert.alert("Recept verwijderen", `Weet je zeker dat je '${recipe.title}' wilt verwijderen?`, [
      { text: "Annuleren", style: "cancel" },
      { text: "Verwijderen", style: "destructive", onPress: () => void deleteRecipe() }
    ]);
  }

  async function deleteRecipe() {
    setDeleting(true);
    setError(null);
    const { error: deleteError } = await supabase.from("recipes").delete().eq("id", recipe.id);
    setDeleting(false);
    if (deleteError) {
      setError(errorMessage(deleteError));
      return;
    }
    await queryClient.invalidateQueries({ queryKey: ["recipes-tab"] });
    await queryClient.invalidateQueries({ queryKey: ["planning-recipes"] });
    router.replace("/recipes");
  }

  return (
    <Screen>
      <Stack.Screen options={{ title: recipe.title }} />
      <RecipeImage uri={recipe.image_url} style={styles.heroImage} />
      <View style={styles.heading}>
        <View style={styles.titleRow}>
          <Text style={styles.title}>{recipe.title}</Text>
          <Pressable accessibilityLabel="Recept verwijderen" accessibilityRole="button" disabled={deleting} onPress={confirmDelete} style={styles.deleteButton}>
            {deleting ? <ActivityIndicator color={colors.danger} size="small" /> : <Trash2 color={colors.danger} size={19} strokeWidth={2.4} />}
          </Pressable>
        </View>
        {recipe.servings ? <Text style={styles.meta}>{recipe.servings} porties</Text> : null}
        {recipe.tags?.length ? (
          <View style={styles.tagList}>
            {recipe.tags.map((tag: string) => (
              <Text key={tag} style={styles.tag}>
                {tag}
              </Text>
            ))}
          </View>
        ) : null}
      </View>

      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Ingredienten</Text>
        {incomplete ? (
          <View style={styles.incompletePanel}>
            <AlertTriangle color={colors.danger} size={22} strokeWidth={2.3} />
            <View style={styles.incompleteCopy}>
              <Text style={styles.incompleteTitle}>Recept incompleet</Text>
              <Text style={styles.incompleteText}>
                Vul ontbrekende informatie aan voordat je ingredienten aan je boodschappenlijst toevoegt.
              </Text>
              {editableDraft ? (
                <Pressable
                  accessibilityRole="button"
                  onPress={() => router.push("/review")}
                  style={({ pressed }) => [styles.editButton, pressed && styles.pressed]}
                >
                  <Pencil color={colors.primaryDark} size={17} strokeWidth={2.4} />
                  <Text style={styles.editButtonText}>Concept bewerken</Text>
                </Pressable>
              ) : null}
            </View>
          </View>
        ) : (
          <>
            <View style={styles.scheduleHeader}>
              <ShoppingCart color={colors.primaryDark} size={22} strokeWidth={2.4} />
              <View style={styles.scheduleCopy}>
                <Text style={styles.scheduleTitle}>Boodschappen doen op</Text>
                <Text style={styles.scheduleSubtitle}>Kies eerst de dag, voeg daarna de ingredienten toe.</Text>
              </View>
            </View>

            <View style={styles.dayPicker}>
              {days.map((day) => {
                const selected = day.key === scheduledFor;
                return (
                  <Pressable
                    accessibilityRole="button"
                    key={day.key}
                    onPress={() => {
                      setScheduledFor(day.key);
                      setFeedback(null);
                    }}
                    style={({ pressed }) => [styles.dayButton, selected && styles.dayButtonSelected, pressed && styles.pressed]}
                  >
                    <Text style={[styles.dayName, selected && styles.dayTextSelected]}>{day.weekday}</Text>
                    <Text style={[styles.dayNumber, selected && styles.dayTextSelected]}>{day.day}</Text>
                  </Pressable>
                );
              })}
              <Pressable
                accessibilityLabel="Andere boodschappendatum kiezen"
                accessibilityRole="button"
                onPress={() => setDatePickerOpen(true)}
                style={({ pressed }) => [
                  styles.dayButton,
                  !days.some((day) => day.key === scheduledFor) && styles.dayButtonSelected,
                  pressed && styles.pressed
                ]}
              >
                <CalendarDays color={colors.primaryDark} size={18} strokeWidth={2.4} />
                <Text style={styles.dayName}>Andere datum</Text>
              </Pressable>
            </View>

            {datePickerOpen ? (
              <View style={styles.datePickerWrap}>
                <DateTimePicker
                  minimumDate={new Date()}
                  mode="date"
                  onChange={(_, date) => {
                    if (Platform.OS !== "ios") setDatePickerOpen(false);
                    if (date) {
                      date.setHours(12, 0, 0, 0);
                      setScheduledFor(toDateKey(date));
                      setFeedback(null);
                    }
                  }}
                  value={new Date(`${scheduledFor}T12:00:00`)}
                />
                {Platform.OS === "ios" ? (
                  <Pressable accessibilityRole="button" onPress={() => setDatePickerOpen(false)} style={styles.doneButton}>
                    <Text style={styles.doneButtonText}>Klaar</Text>
                  </Pressable>
                ) : null}
              </View>
            ) : null}
          </>
        )}

        {feedback ? <Text style={styles.feedback}>{feedback}</Text> : null}
        {error ? <Text style={styles.error}>{error}</Text> : null}

        <View style={styles.ingredientList}>
          {ingredients.map((ingredient) => {
            const isAdding = addingIngredientId === ingredient.id;
            const isAdded = addedIngredientKeys.includes(getIngredientKey(scheduledFor, ingredient.id));

            return (
              <View key={ingredient.id} style={styles.ingredientRow}>
                <View style={styles.ingredientCopy}>
                  <Text style={styles.ingredientName}>{ingredient.ingredient_name}</Text>
                  <Text style={styles.meta}>{formatQuantity(ingredient.quantity, ingredient.unit)}</Text>
                </View>
                {!incomplete ? (
                  <Pressable
                    accessibilityLabel={`${ingredient.ingredient_name} toevoegen aan boodschappenlijst`}
                    accessibilityRole="button"
                    disabled={isAdding || isAdded}
                    onPress={() => addIngredient(ingredient)}
                    style={({ pressed }) => [
                      styles.addButton,
                      isAdded && styles.addButtonAdded,
                      (isAdding || isAdded) && styles.addButtonDisabled,
                      pressed && !isAdding && !isAdded && styles.pressed
                    ]}
                  >
                    {isAdding ? (
                      <ActivityIndicator color={colors.surface} size="small" />
                    ) : isAdded ? (
                      <Check color={colors.primaryDark} size={18} strokeWidth={2.8} />
                    ) : (
                      <Plus color={colors.surface} size={18} strokeWidth={2.8} />
                    )}
                    <Text style={[styles.addButtonText, isAdded && styles.addButtonTextAdded]}>{isAdded ? "Toegevoegd" : "Voeg toe"}</Text>
                  </Pressable>
                ) : null}
              </View>
            );
          })}
        </View>
      </View>

      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Bereiding</Text>
        <View style={styles.instructionList}>
          {instructions.map((step: any) => (
            <View key={step.id} style={styles.instructionRow}>
              <Text style={styles.stepNumber}>{step.step_number}</Text>
              <Text style={styles.instructionText}>{step.instruction}</Text>
            </View>
          ))}
        </View>
      </View>
    </Screen>
  );
}

function getUpcomingDays() {
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date();
    date.setHours(12, 0, 0, 0);
    date.setDate(date.getDate() + index);
    return {
      key: toDateKey(date),
      weekday: index === 0 ? "Vandaag" : new Intl.DateTimeFormat("nl-NL", { weekday: "short" }).format(date).replace(".", ""),
      day: new Intl.DateTimeFormat("nl-NL", { day: "numeric", month: "short" }).format(date).replace(".", "")
    };
  });
}

function toDateKey(date: Date) {
  const offsetDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return offsetDate.toISOString().slice(0, 10);
}

function formatDate(dateKey: string) {
  return new Intl.DateTimeFormat("nl-NL", { weekday: "long", day: "numeric", month: "long" }).format(
    new Date(`${dateKey}T12:00:00`)
  );
}

function getIngredientKey(dateKey: string, ingredientId: string) {
  return `${dateKey}:${ingredientId}`;
}

const styles = StyleSheet.create({
  center: {
    justifyContent: "center"
  },
  heading: {
    gap: spacing.xs
  },
  heroImage: {
    width: "100%",
    height: 214,
    borderRadius: radii.md
  },
  title: {
    flex: 1,
    color: colors.text,
    ...typography.title
  },
  titleRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: spacing.sm
  },
  deleteButton: {
    width: 38,
    height: 38,
    borderRadius: radii.sm,
    borderWidth: 1,
    borderColor: "#f0c7c7",
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "#fff7f7"
  },
  meta: {
    color: colors.muted,
    ...typography.label
  },
  tagList: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: spacing.xs,
    marginTop: spacing.xs
  },
  tag: {
    borderRadius: radii.pill,
    backgroundColor: colors.primarySoft,
    color: colors.primaryDark,
    overflow: "hidden",
    paddingHorizontal: spacing.sm,
    paddingVertical: 5,
    fontSize: 12,
    lineHeight: 15,
    fontWeight: "800"
  },
  section: {
    gap: spacing.md
  },
  sectionTitle: {
    color: colors.text,
    ...typography.cardTitle
  },
  scheduleHeader: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: spacing.sm,
    borderLeftWidth: 3,
    borderLeftColor: colors.primary,
    paddingLeft: spacing.sm
  },
  incompletePanel: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: spacing.sm,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: "#f0c7c7",
    backgroundColor: "#fff7f7",
    padding: spacing.md
  },
  incompleteCopy: {
    flex: 1,
    gap: 5
  },
  incompleteTitle: {
    color: colors.text,
    ...typography.label
  },
  incompleteText: {
    color: colors.muted,
    fontSize: 14,
    lineHeight: 20
  },
  editButton: {
    alignSelf: "flex-start",
    minHeight: 36,
    borderRadius: radii.sm,
    backgroundColor: colors.primarySoft,
    alignItems: "center",
    flexDirection: "row",
    gap: 6,
    marginTop: spacing.xs,
    paddingHorizontal: spacing.sm
  },
  editButtonText: {
    color: colors.primaryDark,
    fontSize: 13,
    lineHeight: 17,
    fontWeight: "800"
  },
  scheduleCopy: {
    flex: 1,
    gap: 2
  },
  scheduleTitle: {
    color: colors.text,
    ...typography.label
  },
  scheduleSubtitle: {
    color: colors.muted,
    fontSize: 13,
    lineHeight: 18
  },
  dayPicker: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: spacing.xs
  },
  datePickerWrap: {
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.border,
    alignItems: "center",
    gap: spacing.xs,
    padding: spacing.sm
  },
  doneButton: {
    minHeight: 36,
    borderRadius: radii.sm,
    backgroundColor: colors.primarySoft,
    justifyContent: "center",
    paddingHorizontal: spacing.md
  },
  doneButtonText: {
    color: colors.primaryDark,
    fontSize: 14,
    lineHeight: 18,
    fontWeight: "800"
  },
  dayButton: {
    width: "23%",
    minHeight: 62,
    borderRadius: radii.sm,
    borderWidth: 1,
    borderColor: colors.border,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.surface,
    gap: 2
  },
  dayButtonSelected: {
    borderColor: colors.primaryDark,
    backgroundColor: colors.primarySoft
  },
  dayName: {
    color: colors.muted,
    fontSize: 12,
    lineHeight: 15,
    fontWeight: "700"
  },
  dayNumber: {
    color: colors.text,
    fontSize: 13,
    lineHeight: 17,
    fontWeight: "800"
  },
  dayTextSelected: {
    color: colors.primaryDark
  },
  feedback: {
    color: colors.primaryDark,
    fontSize: 14,
    lineHeight: 20,
    fontWeight: "700"
  },
  ingredientList: {
    gap: spacing.sm
  },
  ingredientRow: {
    minHeight: 70,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    padding: spacing.md,
    ...shadows.soft
  },
  ingredientCopy: {
    flex: 1,
    gap: 3
  },
  ingredientName: {
    color: colors.text,
    ...typography.label
  },
  addButton: {
    minWidth: 112,
    minHeight: 38,
    borderRadius: radii.sm,
    backgroundColor: colors.primaryDark,
    alignItems: "center",
    justifyContent: "center",
    flexDirection: "row",
    gap: 5,
    paddingHorizontal: spacing.sm
  },
  addButtonAdded: {
    backgroundColor: colors.primarySoft,
    borderWidth: 1,
    borderColor: colors.primary
  },
  addButtonDisabled: {
    opacity: 0.85
  },
  addButtonText: {
    color: colors.surface,
    fontSize: 13,
    lineHeight: 17,
    fontWeight: "800"
  },
  addButtonTextAdded: {
    color: colors.primaryDark
  },
  instructionList: {
    gap: spacing.md
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
  instructionText: {
    flex: 1,
    color: colors.text,
    ...typography.body
  },
  pressed: {
    opacity: 0.78
  },
  error: {
    color: colors.danger,
    fontWeight: "700"
  }
});
