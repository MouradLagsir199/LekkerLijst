import { filterRelevantProductCandidates, formatQuantity } from "@recipe-nl/shared";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { PackageCheck, Pencil, Trash2, X } from "lucide-react-native";
import { useState } from "react";
import { ActivityIndicator, Alert, Modal, Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { Button } from "../../components/Button";
import { errorMessage } from "../../lib/repository";
import { Screen } from "../../components/Screen";
import { supabase } from "../../lib/supabase";
import { colors, radii, shadows, spacing, typography } from "../../lib/theme";

export default function ShoppingListDetailScreen() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const params = useLocalSearchParams<{ id: string }>();
  const listId = params.id;
  const [expandedItemId, setExpandedItemId] = useState<string | null>(null);
  const [editor, setEditor] = useState<{ kind: "list" } | { kind: "item"; item: any } | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editIngredientName, setEditIngredientName] = useState("");
  const [editQuantity, setEditQuantity] = useState("");
  const [editUnit, setEditUnit] = useState("");
  const [editorError, setEditorError] = useState<string | null>(null);
  const [savingEditor, setSavingEditor] = useState(false);
  const [deletingList, setDeletingList] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: ["shopping-list", listId],
    enabled: Boolean(listId),
    queryFn: async () => {
      const { data, error } = await supabase
        .from("shopping_lists")
        .select(
          "id,title,scheduled_for,estimated_total_cents,shopping_list_items(id,ingredient_name,normalized_ingredient_name,quantity,unit,estimated_price_cents,checked,category,selected_product_id,sort_order,products:selected_product_id(name,brand,store_id,package_size_text,current_price_cents,product_url))"
        )
        .eq("id", listId)
        .single();
      if (error) throw error;
      return data as any;
    }
  });

  async function toggleItem(item: any) {
    const { error } = await supabase.from("shopping_list_items").update({ checked: !item.checked }).eq("id", item.id);
    if (error) {
      setActionError(errorMessage(error));
      return;
    }
    await listQuery.refetch();
  }

  async function selectProduct(item: any, product: any) {
    if (!listQuery.data) return;

    const { error } = await supabase
      .from("shopping_list_items")
      .update({
        selected_product_id: product.product_id,
        store_id: product.store_id,
        estimated_price_cents: product.current_price_cents,
        category: product.category
      })
      .eq("id", item.id);
    if (error) {
      setActionError(errorMessage(error));
      return;
    }

    const nextItems = listQuery.data.shopping_list_items.map((existing: any) =>
      existing.id === item.id ? { ...existing, estimated_price_cents: product.current_price_cents } : existing
    );
    const nextTotal = nextItems.reduce((sum: number, existing: any) => sum + (existing.estimated_price_cents ?? 0), 0);
    await supabase.from("shopping_lists").update({ estimated_total_cents: nextTotal }).eq("id", listQuery.data.id);

    setExpandedItemId(null);
    await listQuery.refetch();
  }

  function openListEditor() {
    if (!listQuery.data) return;
    setEditor({ kind: "list" });
    setEditTitle(listQuery.data.title);
    setEditorError(null);
  }

  function openItemEditor(item: any) {
    setEditor({ kind: "item", item });
    setEditIngredientName(item.ingredient_name);
    setEditQuantity(item.quantity?.toString() ?? "");
    setEditUnit(item.unit ?? "");
    setEditorError(null);
  }

  async function refreshShoppingLists() {
    await listQuery.refetch();
    await queryClient.invalidateQueries({ queryKey: ["shopping-lists-tab"] });
  }

  async function refreshTotal() {
    if (!listQuery.data) return;
    const { data, error } = await supabase
      .from("shopping_list_items")
      .select("estimated_price_cents")
      .eq("shopping_list_id", listQuery.data.id);
    if (error) throw error;
    const total = (data ?? []).reduce((sum: number, item: { estimated_price_cents: number | null }) => sum + (item.estimated_price_cents ?? 0), 0);
    const { error: totalError } = await supabase.from("shopping_lists").update({ estimated_total_cents: total }).eq("id", listQuery.data.id);
    if (totalError) throw totalError;
  }

  async function saveEditor() {
    if (!editor || !listQuery.data) return;
    const isList = editor.kind === "list";
    const value = isList ? editTitle.trim() : editIngredientName.trim();
    if (!value) {
      setEditorError(isList ? "Geef de lijst een naam." : "Vul een ingrediënt in.");
      return;
    }

    setSavingEditor(true);
    setEditorError(null);
    try {
      if (isList) {
        const { error } = await supabase.from("shopping_lists").update({ title: value }).eq("id", listQuery.data.id);
        if (error) throw error;
      } else {
        const quantity = parseQuantity(editQuantity);
        const { error } = await supabase
          .from("shopping_list_items")
          .update({
            ingredient_name: value,
            normalized_ingredient_name: value.toLowerCase(),
            quantity,
            unit: editUnit.trim() || null,
            selected_product_id: null,
            store_id: null,
            estimated_price_cents: null,
            category: null
          })
          .eq("id", editor.item.id);
        if (error) throw error;
        await refreshTotal();
      }
      setEditor(null);
      await refreshShoppingLists();
    } catch (saveError) {
      setEditorError(errorMessage(saveError));
    } finally {
      setSavingEditor(false);
    }
  }

  function confirmDeleteItem(item: any) {
    Alert.alert("Ingrediënt verwijderen", `Wil je '${item.ingredient_name}' van deze lijst verwijderen?`, [
      { text: "Annuleren", style: "cancel" },
      { text: "Verwijderen", style: "destructive", onPress: () => void deleteItem(item) }
    ]);
  }

  async function deleteItem(item: any) {
    try {
      const { error } = await supabase.from("shopping_list_items").delete().eq("id", item.id);
      if (error) throw error;
      if (expandedItemId === item.id) setExpandedItemId(null);
      await refreshTotal();
      await refreshShoppingLists();
    } catch (deleteError) {
      setActionError(errorMessage(deleteError));
    }
  }

  function confirmDeleteList() {
    if (!listQuery.data) return;
    Alert.alert("Boodschappenlijst verwijderen", `Wil je '${listQuery.data.title}' met alle ingrediënten verwijderen?`, [
      { text: "Annuleren", style: "cancel" },
      { text: "Verwijderen", style: "destructive", onPress: () => void deleteList() }
    ]);
  }

  async function deleteList() {
    if (!listQuery.data) return;
    setDeletingList(true);
    const { error } = await supabase.from("shopping_lists").delete().eq("id", listQuery.data.id);
    setDeletingList(false);
    if (error) {
      setActionError(errorMessage(error));
      return;
    }
    await queryClient.invalidateQueries({ queryKey: ["shopping-lists-tab"] });
    router.replace("/shopping");
  }

  if (listQuery.isLoading) {
    return (
      <Screen style={styles.center}>
        <Stack.Screen options={{ title: "Boodschappenlijst" }} />
        <ActivityIndicator color={colors.primaryDark} />
      </Screen>
    );
  }

  if (listQuery.error || !listQuery.data) {
    return (
      <Screen>
        <Stack.Screen options={{ title: "Boodschappenlijst" }} />
        <Text style={styles.error}>{String(listQuery.error?.message ?? "Lijst niet gevonden")}</Text>
        <Button onPress={() => router.replace("/shopping")} title="Terug" />
      </Screen>
    );
  }

  const list = listQuery.data;
  const items = [...(list.shopping_list_items ?? [])].sort((a, b) => a.sort_order - b.sort_order);
  const checkedCount = items.filter((item: any) => item.checked).length;

  return (
    <Screen>
      <Stack.Screen options={{ title: "Boodschappenlijst" }} />
      <View style={styles.headingRow}>
        <View style={styles.heading}>
          <Text style={styles.title}>{list.title}</Text>
          <Text style={styles.meta}>{formatDate(list.scheduled_for)}</Text>
          <Text style={styles.meta}>{checkedCount}/{items.length} producten - EUR {(list.estimated_total_cents / 100).toFixed(2)}</Text>
        </View>
        <View style={styles.headerActions}>
          <Pressable accessibilityLabel="Boodschappenlijst bewerken" accessibilityRole="button" onPress={openListEditor} style={styles.iconButton}>
            <Pencil color={colors.primaryDark} size={18} strokeWidth={2.4} />
          </Pressable>
          <Pressable accessibilityLabel="Boodschappenlijst verwijderen" accessibilityRole="button" disabled={deletingList} onPress={confirmDeleteList} style={[styles.iconButton, styles.deleteIconButton]}>
            {deletingList ? <ActivityIndicator color={colors.danger} size="small" /> : <Trash2 color={colors.danger} size={18} strokeWidth={2.4} />}
          </Pressable>
        </View>
      </View>

      <View style={styles.progressTrack}>
        <View style={[styles.progressFill, { width: `${items.length ? (checkedCount / items.length) * 100 : 0}%` }]} />
      </View>

      <View style={styles.items}>
        {items.map((item: any) => {
          const product = item.products;
          const expanded = expandedItemId === item.id;
          return (
            <View key={item.id} style={[styles.item, item.checked && styles.itemDone]}>
              <View style={styles.itemHeader}>
                <View style={styles.itemTitleRow}>
                  <Pressable accessibilityLabel={`${item.ingredient_name} ${item.checked ? "afstrepen ongedaan maken" : "afstrepen"}`} accessibilityRole="checkbox" accessibilityState={{ checked: item.checked }} onPress={() => toggleItem(item)} style={({ pressed }) => [styles.checkboxButton, pressed && styles.pressed]}>
                    <ShoppingCheckBox checked={item.checked} />
                  </Pressable>
                  <View style={styles.itemCopy}>
                    <Text style={[styles.itemName, item.checked && styles.itemNameDone]}>{item.ingredient_name}</Text>
                    <Text style={styles.meta}>{formatQuantity(item.quantity, item.unit)}</Text>
                  </View>
                </View>
                <View style={styles.itemEnd}>
                  <Text style={styles.price}>{item.estimated_price_cents ? `EUR ${(item.estimated_price_cents / 100).toFixed(2)}` : "geen match"}</Text>
                  <Pressable accessibilityLabel={`${item.ingredient_name} bewerken`} accessibilityRole="button" onPress={() => openItemEditor(item)} style={styles.smallIconButton}>
                    <Pencil color={colors.primaryDark} size={17} strokeWidth={2.4} />
                  </Pressable>
                  <Pressable accessibilityLabel={`${item.ingredient_name} verwijderen`} accessibilityRole="button" onPress={() => confirmDeleteItem(item)} style={styles.smallIconButton}>
                    <Trash2 color={colors.danger} size={17} strokeWidth={2.4} />
                  </Pressable>
                </View>
              </View>
              {product ? (
                <Text style={styles.product}>
                  {product.store_id.toUpperCase()} - {product.brand ? `${product.brand} ` : ""}
                  {product.name} {product.package_size_text ? `(${product.package_size_text})` : ""}
                </Text>
              ) : null}
              <Pressable
                accessibilityRole="button"
                onPress={(event) => {
                  event.stopPropagation();
                  setExpandedItemId(expanded ? null : item.id);
                }}
                style={({ pressed }) => [styles.storeButton, pressed && styles.pressed]}
              >
                <Text style={styles.storeButtonText}>{expanded ? "Winkels sluiten" : "Winkels en varianten"}</Text>
              </Pressable>
              {expanded ? <ProductAlternatives item={item} onSelect={(nextProduct) => selectProduct(item, nextProduct)} /> : null}
            </View>
          );
        })}
      </View>

      {actionError ? <Text style={styles.error}>{actionError}</Text> : null}

      {items.length === 0 ? (
        <View style={styles.emptyState}>
          <PackageCheck color={colors.primaryDark} size={28} strokeWidth={2.2} />
          <Text style={styles.meta}>Deze lijst heeft nog geen producten.</Text>
        </View>
      ) : null}

      <Modal animationType="slide" onRequestClose={() => setEditor(null)} transparent visible={Boolean(editor)}>
        <View style={styles.modalBackdrop}>
          <View style={styles.modal}>
            <View style={styles.modalHeader}>
              <Text style={styles.modalTitle}>{editor?.kind === "list" ? "Lijst bewerken" : "Ingrediënt bewerken"}</Text>
              <Pressable accessibilityLabel="Sluiten" accessibilityRole="button" onPress={() => setEditor(null)} style={styles.closeButton}>
                <X color={colors.text} size={20} strokeWidth={2.5} />
              </Pressable>
            </View>
            {editor?.kind === "list" ? (
              <>
                <Text style={styles.fieldLabel}>Naam van de lijst</Text>
                <TextInput autoFocus onChangeText={setEditTitle} style={styles.input} value={editTitle} />
              </>
            ) : (
              <>
                <Text style={styles.fieldLabel}>Ingrediënt</Text>
                <TextInput autoFocus onChangeText={setEditIngredientName} style={styles.input} value={editIngredientName} />
                <View style={styles.quantityFields}>
                  <View style={styles.quantityField}>
                    <Text style={styles.fieldLabel}>Hoeveelheid</Text>
                    <TextInput keyboardType="decimal-pad" onChangeText={setEditQuantity} placeholder="Bijv. 2" style={styles.input} value={editQuantity} />
                  </View>
                  <View style={styles.unitField}>
                    <Text style={styles.fieldLabel}>Eenheid</Text>
                    <TextInput onChangeText={setEditUnit} placeholder="stuks" style={styles.input} value={editUnit} />
                  </View>
                </View>
                <Text style={styles.modalHint}>Na aanpassen kies je opnieuw een winkelproduct dat bij dit ingrediënt past.</Text>
              </>
            )}
            {editorError ? <Text style={styles.error}>{editorError}</Text> : null}
            <Button disabled={savingEditor} onPress={saveEditor} title={savingEditor ? "Opslaan..." : "Opslaan"} />
          </View>
        </View>
      </Modal>
    </Screen>
  );
}

function ShoppingCheckBox({ checked }: { checked: boolean }) {
  return (
    <View style={[styles.checkbox, checked && styles.checkboxChecked]}>
      {checked ? <View style={styles.checkboxTick} /> : null}
    </View>
  );
}

function ProductAlternatives({ item, onSelect }: { item: any; onSelect: (product: any) => void }) {
  const query = item.normalized_ingredient_name || item.ingredient_name;
  const alternatives = useQuery({
    queryKey: ["product-alternatives", item.id, query],
    queryFn: async () => {
      const { data, error } = await supabase.rpc("search_products", {
        query_text: query,
        store_filter: null,
        match_count: 8
      });
      if (error) throw error;
      return filterRelevantProductCandidates(
        (data ?? []).map((product: any) => ({
          ...product,
          currentPriceCents: product.current_price_cents,
          matchScore: product.match_score
        }))
      );
    }
  });

  if (alternatives.isLoading) return <ActivityIndicator color={colors.primaryDark} />;
  if (alternatives.error) return <Text style={styles.error}>{String(alternatives.error.message)}</Text>;
  const products = alternatives.data ?? [];

  return (
    <View style={styles.alternativeList}>
      <Text style={styles.alternativeTitle}>Beschikbaar bij deze winkels</Text>
      {products.map((product: any) => (
        <Pressable key={product.product_id} onPress={() => onSelect(product)} style={({ pressed }) => [styles.alternativeRow, pressed && styles.pressed]}>
          <View style={styles.alternativeHeader}>
            <Text style={styles.alternativeName}>{product.product_name}</Text>
            <Text style={styles.price}>EUR {(product.current_price_cents / 100).toFixed(2)}</Text>
          </View>
          <Text style={styles.meta}>
            {product.store_id.toUpperCase()}
            {product.brand ? ` - ${product.brand}` : ""}
            {product.package_size_text ? ` - ${product.package_size_text}` : ""}
          </Text>
        </Pressable>
      ))}
      {products.length === 0 ? <Text style={styles.meta}>Geen passende winkelvarianten gevonden.</Text> : null}
    </View>
  );
}

function formatDate(dateKey: string) {
  return new Intl.DateTimeFormat("nl-NL", { weekday: "long", day: "numeric", month: "long" }).format(
    new Date(`${dateKey}T12:00:00`)
  );
}

function parseQuantity(value: string) {
  const normalized = value.trim().replace(",", ".");
  if (!normalized) return null;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

const styles = StyleSheet.create({
  center: {
    justifyContent: "center"
  },
  headingRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: spacing.sm
  },
  heading: {
    flex: 1,
    gap: spacing.xs
  },
  headerActions: {
    flexDirection: "row",
    gap: spacing.xs
  },
  iconButton: {
    width: 38,
    height: 38,
    borderRadius: radii.sm,
    borderWidth: 1,
    borderColor: colors.border,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.surface
  },
  deleteIconButton: {
    borderColor: "#f0c7c7",
    backgroundColor: "#fff7f7"
  },
  title: {
    color: colors.text,
    ...typography.title
  },
  meta: {
    color: colors.muted,
    fontSize: 13,
    lineHeight: 18
  },
  progressTrack: {
    height: 7,
    borderRadius: radii.pill,
    overflow: "hidden",
    backgroundColor: colors.border
  },
  progressFill: {
    height: "100%",
    borderRadius: radii.pill,
    backgroundColor: colors.primaryDark
  },
  items: {
    gap: spacing.sm
  },
  item: {
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    gap: spacing.sm,
    padding: spacing.md,
    ...shadows.soft
  },
  itemDone: {
    opacity: 0.62
  },
  itemHeader: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "flex-start",
    gap: spacing.sm
  },
  itemTitleRow: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm
  },
  checkboxButton: {
    width: 28,
    height: 28,
    alignItems: "center",
    justifyContent: "center"
  },
  itemCopy: {
    flex: 1,
    gap: 2
  },
  itemName: {
    color: colors.text,
    ...typography.label
  },
  itemNameDone: {
    textDecorationLine: "line-through"
  },
  price: {
    color: colors.primaryDark,
    fontSize: 13,
    lineHeight: 18,
    fontWeight: "800"
  },
  itemEnd: {
    alignItems: "flex-end",
    gap: 4
  },
  smallIconButton: {
    width: 30,
    height: 30,
    borderRadius: radii.sm,
    alignItems: "center",
    justifyContent: "center"
  },
  product: {
    color: colors.muted,
    fontSize: 13,
    lineHeight: 18
  },
  storeButton: {
    alignSelf: "flex-start",
    minHeight: 34,
    borderRadius: radii.sm,
    backgroundColor: colors.primarySoft,
    justifyContent: "center",
    paddingHorizontal: spacing.sm
  },
  storeButtonText: {
    color: colors.primaryDark,
    fontSize: 13,
    lineHeight: 17,
    fontWeight: "800"
  },
  alternativeList: {
    borderTopWidth: 1,
    borderTopColor: colors.border,
    gap: spacing.sm,
    marginTop: spacing.xs,
    paddingTop: spacing.sm
  },
  alternativeTitle: {
    color: colors.text,
    fontSize: 13,
    lineHeight: 18,
    fontWeight: "800"
  },
  alternativeRow: {
    borderRadius: radii.sm,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: "#fafafa",
    gap: 3,
    padding: spacing.sm
  },
  alternativeHeader: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: spacing.sm
  },
  alternativeName: {
    flex: 1,
    color: colors.text,
    ...typography.label
  },
  checkbox: {
    width: 22,
    height: 22,
    borderRadius: 6,
    borderWidth: 2,
    borderColor: colors.primaryDark,
    backgroundColor: colors.surface
  },
  checkboxChecked: {
    backgroundColor: colors.primaryDark
  },
  checkboxTick: {
    position: "absolute",
    left: 6,
    top: 2,
    width: 7,
    height: 12,
    borderBottomWidth: 2,
    borderRightWidth: 2,
    borderColor: colors.surface,
    transform: [{ rotate: "45deg" }]
  },
  emptyState: {
    alignItems: "center",
    gap: spacing.sm,
    paddingVertical: spacing.xl
  },
  modalBackdrop: {
    flex: 1,
    justifyContent: "flex-end",
    backgroundColor: "rgba(5, 5, 5, 0.25)"
  },
  modal: {
    borderTopLeftRadius: radii.lg,
    borderTopRightRadius: radii.lg,
    backgroundColor: colors.surface,
    gap: spacing.md,
    padding: spacing.lg,
    ...shadows.card
  },
  modalHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.sm
  },
  modalTitle: {
    color: colors.text,
    ...typography.cardTitle
  },
  closeButton: {
    width: 38,
    height: 38,
    borderRadius: radii.sm,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.primarySoft
  },
  fieldLabel: {
    color: colors.text,
    ...typography.label
  },
  input: {
    minHeight: 48,
    borderRadius: radii.sm,
    borderWidth: 1,
    borderColor: colors.border,
    color: colors.text,
    fontSize: 16,
    paddingHorizontal: spacing.sm
  },
  quantityFields: {
    flexDirection: "row",
    gap: spacing.sm
  },
  quantityField: {
    flex: 1,
    gap: spacing.xs
  },
  unitField: {
    flex: 1,
    gap: spacing.xs
  },
  modalHint: {
    color: colors.muted,
    fontSize: 13,
    lineHeight: 18
  },
  pressed: {
    opacity: 0.78
  },
  error: {
    color: colors.danger,
    fontWeight: "700"
  }
});
