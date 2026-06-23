import { formatQuantity } from "@recipe-nl/shared";
import { useQuery } from "@tanstack/react-query";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { PackageCheck } from "lucide-react-native";
import { useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { Button } from "../../components/Button";
import { Screen } from "../../components/Screen";
import { supabase } from "../../lib/supabase";
import { colors, radii, shadows, spacing, typography } from "../../lib/theme";

export default function ShoppingListDetailScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ id: string }>();
  const listId = params.id;
  const [expandedItemId, setExpandedItemId] = useState<string | null>(null);

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
    if (error) return;
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
    if (error) return;

    const nextItems = listQuery.data.shopping_list_items.map((existing: any) =>
      existing.id === item.id ? { ...existing, estimated_price_cents: product.current_price_cents } : existing
    );
    const nextTotal = nextItems.reduce((sum: number, existing: any) => sum + (existing.estimated_price_cents ?? 0), 0);
    await supabase.from("shopping_lists").update({ estimated_total_cents: nextTotal }).eq("id", listQuery.data.id);

    setExpandedItemId(null);
    await listQuery.refetch();
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
      <View style={styles.heading}>
        <Text style={styles.title}>{formatDate(list.scheduled_for)}</Text>
        <Text style={styles.meta}>
          {checkedCount}/{items.length} producten - EUR {(list.estimated_total_cents / 100).toFixed(2)}
        </Text>
      </View>

      <View style={styles.progressTrack}>
        <View style={[styles.progressFill, { width: `${items.length ? (checkedCount / items.length) * 100 : 0}%` }]} />
      </View>

      <View style={styles.items}>
        {items.map((item: any) => {
          const product = item.products;
          const expanded = expandedItemId === item.id;
          return (
            <Pressable
              key={item.id}
              onPress={() => toggleItem(item)}
              style={({ pressed }) => [styles.item, item.checked && styles.itemDone, pressed && styles.pressed]}
            >
              <View style={styles.itemHeader}>
                <View style={styles.itemTitleRow}>
                  <ShoppingCheckBox checked={item.checked} />
                  <View style={styles.itemCopy}>
                    <Text style={[styles.itemName, item.checked && styles.itemNameDone]}>{item.ingredient_name}</Text>
                    <Text style={styles.meta}>{formatQuantity(item.quantity, item.unit)}</Text>
                  </View>
                </View>
                <Text style={styles.price}>
                  {item.estimated_price_cents ? `EUR ${(item.estimated_price_cents / 100).toFixed(2)}` : "geen match"}
                </Text>
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
            </Pressable>
          );
        })}
      </View>

      {items.length === 0 ? (
        <View style={styles.emptyState}>
          <PackageCheck color={colors.primaryDark} size={28} strokeWidth={2.2} />
          <Text style={styles.meta}>Deze lijst heeft nog geen producten.</Text>
        </View>
      ) : null}
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
      return data ?? [];
    }
  });

  if (alternatives.isLoading) return <ActivityIndicator color={colors.primaryDark} />;
  if (alternatives.error) return <Text style={styles.error}>{String(alternatives.error.message)}</Text>;

  return (
    <View style={styles.alternativeList}>
      <Text style={styles.alternativeTitle}>Beschikbaar bij deze winkels</Text>
      {alternatives.data.map((product: any) => (
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
      {alternatives.data.length === 0 ? <Text style={styles.meta}>Geen alternatieven gevonden.</Text> : null}
    </View>
  );
}

function formatDate(dateKey: string) {
  return new Intl.DateTimeFormat("nl-NL", { weekday: "long", day: "numeric", month: "long" }).format(
    new Date(`${dateKey}T12:00:00`)
  );
}

const styles = StyleSheet.create({
  center: {
    justifyContent: "center"
  },
  heading: {
    gap: spacing.xs
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
  pressed: {
    opacity: 0.78
  },
  error: {
    color: colors.danger,
    fontWeight: "700"
  }
});
