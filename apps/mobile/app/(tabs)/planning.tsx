import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, NotebookPen, Plus, Trash2, X } from "lucide-react-native";
import { useMemo, useState } from "react";
import { ActivityIndicator, Modal, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Button } from "../../components/Button";
import { useAuth } from "../../lib/auth";
import { colors, radii, shadows, spacing, typography } from "../../lib/theme";
import { supabase } from "../../lib/supabase";

type RecipeOption = {
  id: string;
  title: string;
  image_url: string | null;
};

type MealItem = {
  id: string;
  created_at: string;
  planned_for: string;
  meal_slot: "breakfast" | "lunch" | "dinner";
  recipe_id: string | null;
  custom_title: string | null;
  note: string | null;
  recipes: { title: string; image_url: string | null } | null;
};

type MealPlan = {
  id: string;
  week_start: string;
  meal_plan_items: MealItem[];
};

type EditorState = {
  dateKey: string;
  slot: "breakfast" | "lunch" | "dinner";
  existing?: MealItem;
};

const mealSlots: Array<{ key: "breakfast" | "lunch" | "dinner"; label: string }> = [
  { key: "breakfast", label: "Ontbijt" },
  { key: "lunch", label: "Lunch" },
  { key: "dinner", label: "Avondeten" }
];

export default function PlanningTab() {
  const { session } = useAuth();
  const insets = useSafeAreaInsets();
  const [weekStart, setWeekStart] = useState(() => startOfWeek(new Date()));
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [selectedRecipeId, setSelectedRecipeId] = useState<string | null>(null);
  const [customTitle, setCustomTitle] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [editorError, setEditorError] = useState<string | null>(null);

  const weekKey = toDateKey(weekStart);
  const days = useMemo(() => getWeekDays(weekStart), [weekStart]);
  const plan = useQuery({
    queryKey: ["meal-plan", session?.user.id, weekKey],
    enabled: Boolean(session),
    queryFn: async () => {
      const { data, error } = await supabase
        .from("meal_plans")
        .select("id,week_start,meal_plan_items(id,created_at,planned_for,meal_slot,recipe_id,custom_title,note,recipes(title,image_url))")
        .eq("week_start", weekKey)
        .maybeSingle();
      if (error) throw error;
      return data as MealPlan | null;
    }
  });
  const recipes = useQuery({
    queryKey: ["planning-recipes", session?.user.id],
    enabled: Boolean(session),
    queryFn: async () => {
      const { data, error } = await supabase.from("recipes").select("id,title,image_url").order("created_at", { ascending: false });
      if (error) throw error;
      return (data ?? []) as RecipeOption[];
    }
  });

  const itemsByDayAndSlot = useMemo(() => {
    const items = new Map<string, MealItem[]>();
    for (const item of plan.data?.meal_plan_items ?? []) {
      const key = `${item.planned_for}:${item.meal_slot}`;
      items.set(key, [...(items.get(key) ?? []), item]);
    }
    for (const groupedItems of items.values()) groupedItems.sort((left, right) => left.created_at.localeCompare(right.created_at));
    return items;
  }, [plan.data]);

  function openEditor(dateKey: string, slot: "breakfast" | "lunch" | "dinner", existing?: MealItem) {
    setEditor({ dateKey, slot, existing });
    setSelectedRecipeId(existing?.recipe_id ?? null);
    setCustomTitle(existing?.custom_title ?? "");
    setNote(existing?.note ?? "");
    setEditorError(null);
  }

  async function ensurePlan() {
    if (plan.data?.id) return plan.data.id;
    if (!session) throw new Error("Je bent niet ingelogd.");

    const { data, error } = await supabase
      .from("meal_plans")
      .upsert({ user_id: session.user.id, week_start: weekKey }, { onConflict: "user_id,week_start" })
      .select("id")
      .single();
    if (error) throw error;
    return data.id as string;
  }

  async function saveMeal() {
    if (!editor) return;
    if (!selectedRecipeId && !customTitle.trim()) {
      setEditorError("Kies een recept of vul zelf een maaltijd in.");
      return;
    }

    setSaving(true);
    setEditorError(null);
    try {
      const mealPlanId = await ensurePlan();
      const values = {
        meal_plan_id: mealPlanId,
        planned_for: editor.dateKey,
        meal_slot: editor.slot,
        recipe_id: selectedRecipeId,
        custom_title: selectedRecipeId ? null : customTitle.trim(),
        note: note.trim() || null
      };
      const request = editor.existing
        ? supabase.from("meal_plan_items").update(values).eq("id", editor.existing.id)
        : supabase.from("meal_plan_items").insert(values);
      const { error } = await request;
      if (error) throw error;
      setEditor(null);
      await plan.refetch();
    } catch (error) {
      setEditorError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }

  async function deleteMeal() {
    if (!editor?.existing) return;
    setSaving(true);
    setEditorError(null);
    const { error } = await supabase.from("meal_plan_items").delete().eq("id", editor.existing.id);
    setSaving(false);
    if (error) {
      setEditorError(error.message);
      return;
    }
    setEditor(null);
    await plan.refetch();
  }

  return (
    <View style={styles.screen}>
      <ScrollView
        contentContainerStyle={[styles.content, { paddingTop: Math.max(insets.top + 24, 54) }]}
        showsVerticalScrollIndicator={false}
      >
        <Text style={styles.title}>Planning</Text>

        <View style={styles.weekHeader}>
          <Pressable accessibilityLabel="Vorige week" accessibilityRole="button" onPress={() => setWeekStart(addDays(weekStart, -7))} style={styles.iconButton}>
            <ChevronLeft color={colors.text} size={22} strokeWidth={2.5} />
          </Pressable>
          <View style={styles.weekCopy}>
            <Text style={styles.weekTitle}>{formatWeek(weekStart)}</Text>
            <Text style={styles.meta}>Plan je maaltijden per dag</Text>
          </View>
          <Pressable accessibilityLabel="Volgende week" accessibilityRole="button" onPress={() => setWeekStart(addDays(weekStart, 7))} style={styles.iconButton}>
            <ChevronRight color={colors.text} size={22} strokeWidth={2.5} />
          </Pressable>
        </View>

        {plan.isLoading ? <ActivityIndicator color={colors.primaryDark} /> : null}
        {plan.error ? <Text style={styles.error}>{String(plan.error.message)}</Text> : null}

        <View style={styles.days}>
          {days.map((day) => (
            <View key={day.key} style={styles.day}>
              <View style={styles.dayHeader}>
                <Text style={styles.dayName}>{day.label}</Text>
                <Text style={styles.dayDate}>{day.dateLabel}</Text>
              </View>
              {mealSlots.map((slot) => {
                const items = itemsByDayAndSlot.get(`${day.key}:${slot.key}`) ?? [];
                return (
                  <View key={slot.key} style={styles.slotGroup}>
                    <View style={styles.slotHeader}>
                      <Text style={styles.slotText}>{slot.label}</Text>
                      {items.length ? <Text style={styles.slotCount}>{items.length} gepland</Text> : null}
                    </View>
                    {items.map((item) => (
                      <Pressable
                        accessibilityRole="button"
                        key={item.id}
                        onPress={() => openEditor(day.key, slot.key, item)}
                        style={({ pressed }) => [styles.mealRow, styles.mealRowFilled, pressed && styles.pressed]}
                      >
                        <View style={styles.mealCopy}>
                          <Text numberOfLines={1} style={styles.mealTitle}>{item.recipes?.title ?? item.custom_title}</Text>
                          {item.note ? <Text numberOfLines={1} style={styles.mealNote}>{item.note}</Text> : null}
                        </View>
                        <NotebookPen color={colors.primaryDark} size={18} strokeWidth={2.3} />
                      </Pressable>
                    ))}
                    <Pressable
                      accessibilityLabel={`${slot.label} toevoegen op ${day.label}`}
                      accessibilityRole="button"
                      onPress={() => openEditor(day.key, slot.key)}
                      style={({ pressed }) => [styles.addMealRow, pressed && styles.pressed]}
                    >
                      <Plus color={colors.primaryDark} size={18} strokeWidth={2.4} />
                      <Text style={styles.addMealText}>{items.length ? "Nog een maaltijd toevoegen" : "Maaltijd toevoegen"}</Text>
                    </Pressable>
                  </View>
                );
              })}
            </View>
          ))}
        </View>
      </ScrollView>

      <Modal animationType="slide" onRequestClose={() => setEditor(null)} transparent visible={Boolean(editor)}>
        <View style={styles.modalBackdrop}>
          <View style={styles.modal}>
            <View style={styles.modalHeader}>
              <View>
                <Text style={styles.modalTitle}>{editor ? `${formatDay(editor.dateKey)} · ${slotLabel(editor.slot)}` : "Maaltijd"}</Text>
                <Text style={styles.meta}>Kies een opgeslagen recept of vul zelf iets in.</Text>
              </View>
              <Pressable accessibilityLabel="Sluiten" accessibilityRole="button" onPress={() => setEditor(null)} style={styles.closeButton}>
                <X color={colors.text} size={21} strokeWidth={2.5} />
              </Pressable>
            </View>

            <Text style={styles.fieldLabel}>Mijn recepten</Text>
            <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.recipeChoices}>
              {recipes.data?.map((recipe) => {
                const selected = recipe.id === selectedRecipeId;
                return (
                  <Pressable
                    key={recipe.id}
                    onPress={() => {
                      setSelectedRecipeId(recipe.id);
                      setCustomTitle("");
                    }}
                    style={[styles.recipeChoice, selected && styles.recipeChoiceSelected]}
                  >
                    <Text numberOfLines={2} style={[styles.recipeChoiceText, selected && styles.recipeChoiceTextSelected]}>{recipe.title}</Text>
                  </Pressable>
                );
              })}
              {!recipes.data?.length ? <Text style={styles.meta}>Sla eerst een recept op om het hier te plannen.</Text> : null}
            </ScrollView>

            <Text style={styles.fieldLabel}>Of zelf invullen</Text>
            <TextInput
              onChangeText={(value) => {
                setCustomTitle(value);
                if (value.trim()) setSelectedRecipeId(null);
              }}
              placeholder="Bijvoorbeeld: soep met brood"
              style={styles.input}
              value={customTitle}
            />
            <Text style={styles.fieldLabel}>Notitie</Text>
            <TextInput multiline onChangeText={setNote} placeholder="Optioneel" style={[styles.input, styles.noteInput]} textAlignVertical="top" value={note} />

            {editorError ? <Text style={styles.error}>{editorError}</Text> : null}
            <Button disabled={saving} onPress={saveMeal} title={saving ? "Opslaan..." : "Maaltijd opslaan"} />
            {editor?.existing ? (
              <Pressable accessibilityRole="button" disabled={saving} onPress={deleteMeal} style={styles.deleteButton}>
                <Trash2 color={colors.danger} size={18} strokeWidth={2.3} />
                <Text style={styles.deleteText}>Uit planning verwijderen</Text>
              </Pressable>
            ) : null}
          </View>
        </View>
      </Modal>
    </View>
  );
}

function startOfWeek(date: Date) {
  const result = new Date(date);
  result.setHours(12, 0, 0, 0);
  result.setDate(result.getDate() - ((result.getDay() + 6) % 7));
  return result;
}

function addDays(date: Date, amount: number) {
  const result = new Date(date);
  result.setDate(result.getDate() + amount);
  return result;
}

function getWeekDays(weekStart: Date) {
  return Array.from({ length: 7 }, (_, index) => {
    const date = addDays(weekStart, index);
    return {
      key: toDateKey(date),
      label: new Intl.DateTimeFormat("nl-NL", { weekday: "long" }).format(date),
      dateLabel: new Intl.DateTimeFormat("nl-NL", { day: "numeric", month: "short" }).format(date).replace(".", "")
    };
  });
}

function toDateKey(date: Date) {
  const offsetDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return offsetDate.toISOString().slice(0, 10);
}

function formatWeek(date: Date) {
  const end = addDays(date, 6);
  return `${new Intl.DateTimeFormat("nl-NL", { day: "numeric", month: "short" }).format(date).replace(".", "")} - ${new Intl.DateTimeFormat("nl-NL", { day: "numeric", month: "short" }).format(end).replace(".", "")}`;
}

function formatDay(dateKey: string) {
  return new Intl.DateTimeFormat("nl-NL", { weekday: "long", day: "numeric", month: "long" }).format(new Date(`${dateKey}T12:00:00`));
}

function slotLabel(slot: "breakfast" | "lunch" | "dinner") {
  if (slot === "breakfast") return "Ontbijt";
  return slot === "lunch" ? "Lunch" : "Avondeten";
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background },
  content: { paddingHorizontal: 24, paddingBottom: 126, gap: spacing.lg },
  title: { color: colors.text, ...typography.title },
  weekHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.md },
  weekCopy: { flex: 1, alignItems: "center", gap: 2 },
  weekTitle: { color: colors.text, ...typography.cardTitle },
  iconButton: { width: 40, height: 40, borderRadius: radii.sm, borderWidth: 1, borderColor: colors.border, alignItems: "center", justifyContent: "center" },
  meta: { color: colors.muted, fontSize: 13, lineHeight: 18 },
  days: { gap: spacing.md },
  day: { gap: spacing.xs },
  dayHeader: { flexDirection: "row", alignItems: "baseline", justifyContent: "space-between", paddingHorizontal: spacing.xs },
  dayName: { color: colors.text, ...typography.cardTitle },
  dayDate: { color: colors.primaryDark, fontSize: 13, lineHeight: 18, fontWeight: "800" },
  slotGroup: { gap: spacing.xs },
  slotHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingHorizontal: spacing.xs },
  slotCount: { color: colors.muted, fontSize: 12, lineHeight: 16 },
  mealRow: { minHeight: 56, borderRadius: radii.sm, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.surface, flexDirection: "row", alignItems: "center", gap: spacing.sm, padding: spacing.sm },
  mealRowFilled: { borderColor: colors.primary },
  slotText: { color: colors.muted, fontSize: 12, lineHeight: 16, fontWeight: "800" },
  mealCopy: { flex: 1, gap: 2 },
  mealTitle: { color: colors.text, ...typography.label },
  mealNote: { color: colors.muted, fontSize: 12, lineHeight: 16 },
  addMealRow: { minHeight: 42, borderRadius: radii.sm, borderWidth: 1, borderStyle: "dashed", borderColor: colors.primary, backgroundColor: colors.primarySoft, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6, paddingHorizontal: spacing.sm },
  addMealText: { color: colors.primaryDark, fontSize: 13, lineHeight: 17, fontWeight: "800" },
  modalBackdrop: { flex: 1, justifyContent: "flex-end", backgroundColor: "rgba(5, 5, 5, 0.25)" },
  modal: { maxHeight: "86%", borderTopLeftRadius: radii.lg, borderTopRightRadius: radii.lg, backgroundColor: colors.surface, gap: spacing.md, padding: spacing.lg, ...shadows.card },
  modalHeader: { flexDirection: "row", alignItems: "flex-start", justifyContent: "space-between", gap: spacing.md },
  modalTitle: { color: colors.text, ...typography.cardTitle },
  closeButton: { width: 40, height: 40, borderRadius: radii.sm, alignItems: "center", justifyContent: "center", backgroundColor: colors.primarySoft },
  fieldLabel: { color: colors.text, ...typography.label },
  recipeChoices: { gap: spacing.sm, paddingRight: spacing.md },
  recipeChoice: { width: 142, minHeight: 58, borderRadius: radii.sm, borderWidth: 1, borderColor: colors.border, justifyContent: "center", padding: spacing.sm },
  recipeChoiceSelected: { borderColor: colors.primaryDark, backgroundColor: colors.primarySoft },
  recipeChoiceText: { color: colors.text, fontSize: 13, lineHeight: 18, fontWeight: "800" },
  recipeChoiceTextSelected: { color: colors.primaryDark },
  input: { minHeight: 50, borderRadius: radii.sm, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.surface, paddingHorizontal: spacing.md, color: colors.text, fontSize: 16 },
  noteInput: { minHeight: 72, paddingVertical: spacing.sm },
  deleteButton: { alignSelf: "center", minHeight: 40, alignItems: "center", flexDirection: "row", gap: 7, justifyContent: "center", paddingHorizontal: spacing.md },
  deleteText: { color: colors.danger, fontSize: 14, lineHeight: 18, fontWeight: "800" },
  pressed: { opacity: 0.78 },
  error: { color: colors.danger, fontWeight: "700" }
});
