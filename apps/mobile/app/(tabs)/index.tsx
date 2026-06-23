import { useQuery } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { Camera, Clock3, Link2, PenLine, Plus } from "lucide-react-native";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useAuth } from "../../lib/auth";
import { RecipeImage } from "../../components/RecipeImage";
import { colors, radii, shadows, spacing, typography } from "../../lib/theme";
import { supabase } from "../../lib/supabase";

type RecipeHomeRow = {
  id: string;
  title: string;
  servings: number | null;
  total_time_minutes: number | null;
  created_at: string;
  image_url: string | null;
  estimated_total_cents: number | null;
};

export default function HomeTab() {
  const router = useRouter();
  const { session } = useAuth();
  const insets = useSafeAreaInsets();
  const displayName = getDisplayName(session?.user.email);

  const recipes = useQuery({
    queryKey: ["home-recipes", session?.user.id],
    enabled: Boolean(session),
    queryFn: async () => {
      const { data: recipeRows, error } = await supabase
        .from("recipes")
        .select("id,title,servings,total_time_minutes,created_at,image_url")
        .order("created_at", { ascending: false })
        .limit(8);
      if (error) throw error;

      const ids = (recipeRows ?? []).map((recipe) => recipe.id);
      let totals = new Map<string, number>();
      if (ids.length > 0) {
        const { data: listRows, error: listError } = await supabase
          .from("shopping_lists")
          .select("recipe_id,estimated_total_cents")
          .in("recipe_id", ids);
        if (listError) throw listError;
        totals = new Map((listRows ?? []).map((list) => [list.recipe_id as string, list.estimated_total_cents as number]));
      }

      return (recipeRows ?? []).map((recipe) => ({
        ...recipe,
        estimated_total_cents: totals.get(recipe.id) ?? null
      })) as RecipeHomeRow[];
    }
  });

  return (
    <View style={styles.screen}>
      <ScrollView
        contentContainerStyle={[styles.content, { paddingTop: Math.max(insets.top + 26, 54) }]}
        showsVerticalScrollIndicator={false}
      >
        <Text adjustsFontSizeToFit minimumFontScale={0.72} numberOfLines={1} style={styles.greeting}>
          Hoi {displayName}
        </Text>

        <Pressable onPress={() => router.push("/import")} style={({ pressed }) => [styles.hero, pressed && styles.pressed]}>
          <Text numberOfLines={1} adjustsFontSizeToFit minimumFontScale={0.82} style={styles.heroText}>
            Recept importeren
          </Text>
          <View style={styles.plusCircle}>
            <Plus color={colors.primaryDark} size={30} strokeWidth={2.2} />
          </View>
        </Pressable>

        <View style={styles.quickCard}>
          <QuickAction icon={<Link2 color={colors.text} size={26} strokeWidth={2.5} />} label="Plak link" onPress={() => router.push("/import?mode=url")} />
          <View style={styles.quickDivider} />
          <QuickAction
            icon={<PenLine color={colors.text} size={26} strokeWidth={2.5} />}
            label="Plak tekst"
            onPress={() => router.push("/import?mode=text")}
          />
          <View style={styles.quickDivider} />
          <QuickAction icon={<Camera color={colors.text} size={26} strokeWidth={2.5} />} label="Scan recept" todo />
        </View>

        <View style={styles.sectionHeader}>
          <Text style={styles.sectionTitle}>Recente recepten</Text>
        </View>

        {recipes.isLoading ? <RecipeSkeleton /> : null}
        {recipes.error ? <Text style={styles.error}>{String(recipes.error.message)}</Text> : null}

        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.recipeRail}>
          {recipes.data?.map((recipe) => (
            <RecipePreviewCard
              key={recipe.id}
              recipe={recipe}
              onPress={() => router.push(`/recipe/${recipe.id}`)}
            />
          ))}
          {recipes.data?.length === 0 ? (
            <Pressable onPress={() => router.push("/import")} style={styles.emptyCard}>
              <Text style={styles.emptyTitle}>Importeer je eerste recept</Text>
              <Text style={styles.emptyBody}>Plak een recept en plan daarna je boodschappen per ingredient.</Text>
            </Pressable>
          ) : null}
        </ScrollView>
      </ScrollView>
    </View>
  );
}

function QuickAction({
  icon,
  label,
  onPress,
  todo
}: {
  icon: React.ReactNode;
  label: string;
  onPress?: () => void;
  todo?: boolean;
}) {
  return (
    <Pressable onPress={onPress} disabled={!onPress} style={({ pressed }) => [styles.quickAction, pressed && styles.pressed]}>
      <View style={styles.quickIcon}>{icon}</View>
      <Text style={styles.quickLabel}>{label}</Text>
      {todo ? <Text style={styles.todo}>#todo</Text> : null}
    </Pressable>
  );
}

function RecipePreviewCard({
  recipe,
  onPress
}: {
  recipe: RecipeHomeRow;
  onPress: () => void;
}) {
  const minutes = recipe.total_time_minutes ?? 30;
  const price =
    recipe.estimated_total_cents && recipe.servings
      ? `€ ${(recipe.estimated_total_cents / recipe.servings / 100).toFixed(2)} p.p.`
      : "€ -- p.p.";

  return (
    <Pressable onPress={onPress} style={({ pressed }) => [styles.recipeCard, pressed && styles.pressed]}>
      <RecipeImage uri={recipe.image_url} style={styles.recipeImage} />
      <View style={styles.recipeBody}>
        <Text style={styles.recipeTitle} numberOfLines={2}>
          {recipe.title}
        </Text>
        <View style={styles.recipeMeta}>
          <View style={styles.metaItem}>
            <Clock3 color={colors.text} size={18} strokeWidth={2.2} />
            <Text style={styles.metaText}>{minutes} min.</Text>
          </View>
          <Text style={styles.metaText}>{price}</Text>
        </View>
      </View>
    </Pressable>
  );
}

function RecipeSkeleton() {
  return (
    <View style={styles.skeletonRail}>
      <View style={styles.skeletonCard} />
      <View style={styles.skeletonCard} />
    </View>
  );
}

function getDisplayName(email?: string | null) {
  if (!email) return "Eva";
  const local = email.split("@")[0] || "Eva";
  return local.charAt(0).toUpperCase() + local.slice(1).replace(/[._-].*$/, "");
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: colors.background
  },
  content: {
    paddingHorizontal: 28,
    paddingBottom: 112
  },
  greeting: {
    color: colors.text,
    marginBottom: 26,
    ...typography.title
  },
  hero: {
    minHeight: 112,
    borderRadius: radii.lg,
    backgroundColor: colors.primary,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 20,
    paddingLeft: 24,
    paddingRight: 86,
    ...shadows.soft
  },
  heroText: {
    color: colors.surface,
    fontSize: 23,
    lineHeight: 30,
    fontWeight: "800"
  },
  plusCircle: {
    position: "absolute",
    right: 22,
    width: 52,
    height: 52,
    borderRadius: radii.pill,
    backgroundColor: colors.surface,
    alignItems: "center",
    justifyContent: "center"
  },
  quickCard: {
    minHeight: 118,
    borderRadius: radii.lg,
    backgroundColor: colors.surface,
    flexDirection: "row",
    alignItems: "stretch",
    marginBottom: 34,
    paddingVertical: 14,
    ...shadows.card
  },
  quickAction: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 6
  },
  quickIcon: {
    width: 52,
    height: 52,
    borderRadius: radii.pill,
    backgroundColor: colors.primarySoft,
    alignItems: "center",
    justifyContent: "center"
  },
  quickLabel: {
    color: colors.text,
    fontSize: 16,
    lineHeight: 20,
    fontWeight: "800"
  },
  todo: {
    color: colors.muted,
    fontSize: 10,
    lineHeight: 12,
    fontWeight: "700"
  },
  quickDivider: {
    width: 1,
    marginVertical: 2,
    backgroundColor: colors.border
  },
  sectionHeader: {
    marginBottom: 16
  },
  sectionTitle: {
    color: colors.text,
    ...typography.sectionTitle
  },
  recipeRail: {
    gap: 18,
    paddingRight: 24
  },
  recipeCard: {
    width: 188,
    borderRadius: radii.md,
    backgroundColor: colors.surface,
    overflow: "hidden",
    ...shadows.card
  },
  recipeImage: {
    width: "100%",
    height: 136,
    backgroundColor: colors.primarySoft
  },
  recipeBody: {
    minHeight: 104,
    padding: 12,
    justifyContent: "space-between"
  },
  recipeTitle: {
    color: colors.text,
    ...typography.cardTitle
  },
  recipeMeta: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8
  },
  metaItem: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4
  },
  metaText: {
    color: colors.text,
    fontSize: 13,
    lineHeight: 18,
    fontWeight: "500"
  },
  emptyCard: {
    width: 260,
    minHeight: 160,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    justifyContent: "center",
    gap: spacing.sm,
    padding: spacing.lg
  },
  emptyTitle: {
    color: colors.text,
    ...typography.cardTitle
  },
  emptyBody: {
    color: colors.muted,
    ...typography.body
  },
  skeletonRail: {
    flexDirection: "row",
    gap: 18
  },
  skeletonCard: {
    width: 206,
    height: 262,
    borderRadius: radii.md,
    backgroundColor: "#f1f5f2"
  },
  pressed: {
    opacity: 0.82
  },
  error: {
    color: colors.danger,
    fontWeight: "700"
  }
});
