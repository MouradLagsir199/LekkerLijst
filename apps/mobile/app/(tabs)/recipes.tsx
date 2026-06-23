import { useQuery } from "@tanstack/react-query";
import FontAwesome6 from "@expo/vector-icons/FontAwesome6";
import { useRouter } from "expo-router";
import { Clock3, Globe2, PenLine } from "lucide-react-native";
import { useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { RecipeImage } from "../../components/RecipeImage";
import { colors, radii, shadows, spacing, typography } from "../../lib/theme";
import { supabase } from "../../lib/supabase";

type RecipeRow = {
  id: string;
  title: string;
  created_at: string;
  total_time_minutes: number | null;
  image_url: string | null;
  source_platform: string | null;
  tags: string[];
};

export default function RecipesTab() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [selectedTag, setSelectedTag] = useState<string | null>(null);
  const recipes = useQuery({
    queryKey: ["recipes-tab"],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("recipes")
        .select("id,title,created_at,total_time_minutes,image_url,source_platform,tags")
        .order("created_at", { ascending: false });
      if (error) throw error;
      return (data ?? []) as RecipeRow[];
    }
  });

  const tags = useMemo(
    () => Array.from(new Set((recipes.data ?? []).flatMap((recipe) => recipe.tags ?? []))).sort((left, right) => left.localeCompare(right, "nl")),
    [recipes.data]
  );
  const visibleRecipes = useMemo(
    () => (selectedTag ? (recipes.data ?? []).filter((recipe) => recipe.tags?.includes(selectedTag)) : recipes.data ?? []),
    [recipes.data, selectedTag]
  );

  return (
    <View style={styles.screen}>
      <ScrollView
        contentContainerStyle={[styles.content, { paddingTop: Math.max(insets.top + 24, 54) }]}
        showsVerticalScrollIndicator={false}
      >
        <Text style={styles.title}>Recepten</Text>
        {tags.length ? (
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.tagRail}>
            <TagButton active={!selectedTag} label="Alles" onPress={() => setSelectedTag(null)} />
            {tags.map((tag) => (
              <TagButton active={selectedTag === tag} key={tag} label={tag} onPress={() => setSelectedTag(tag)} />
            ))}
          </ScrollView>
        ) : null}

        <View style={styles.list}>
          {recipes.isLoading ? <Text style={styles.meta}>Recepten laden...</Text> : null}
          {recipes.error ? <Text style={styles.error}>{String(recipes.error.message)}</Text> : null}
          {visibleRecipes.map((recipe) => (
            <Pressable
              key={recipe.id}
              onPress={() => router.push(`/recipe/${recipe.id}`)}
              style={({ pressed }) => [styles.card, pressed && styles.pressed]}
            >
              <RecipeImage uri={recipe.image_url} style={styles.image} />
              <View style={styles.cardBody}>
                <View style={styles.cardHeader}>
                  <Text numberOfLines={2} style={styles.cardTitle}>
                    {recipe.title}
                  </Text>
                  <PlatformMark platform={recipe.source_platform} />
                </View>
                <Text style={styles.sourceText}>{formatImportSource(recipe.source_platform, recipe.created_at)}</Text>
                <View style={styles.cardFooter}>
                  {recipe.total_time_minutes ? (
                    <View style={styles.time}>
                      <Clock3 color={colors.muted} size={15} strokeWidth={2.3} />
                      <Text style={styles.meta}>{recipe.total_time_minutes} min.</Text>
                    </View>
                  ) : (
                    <Text style={styles.meta}>Bereidingstijd onbekend</Text>
                  )}
                </View>
                {recipe.tags?.length ? <Text numberOfLines={1} style={styles.tagText}>{recipe.tags.join("  ·  ")}</Text> : null}
              </View>
            </Pressable>
          ))}
          {!recipes.isLoading && visibleRecipes.length === 0 ? (
            <Text style={styles.meta}>{selectedTag ? "Geen recepten met deze tag." : "Nog geen recepten opgeslagen."}</Text>
          ) : null}
        </View>
      </ScrollView>
    </View>
  );
}

function TagButton({ active, label, onPress }: { active: boolean; label: string; onPress: () => void }) {
  return (
    <Pressable onPress={onPress} style={({ pressed }) => [styles.tagButton, active && styles.tagButtonActive, pressed && styles.pressed]}>
      <Text style={[styles.tagButtonText, active && styles.tagButtonTextActive]}>{label}</Text>
    </Pressable>
  );
}

function PlatformMark({ platform }: { platform: string | null }) {
  const iconProps = { color: colors.primaryDark, size: 18, strokeWidth: 2.3 };
  const brandIconProps = { color: colors.primaryDark, size: 18, brand: true };
  if (platform === "instagram") return <FontAwesome6 name="instagram" {...brandIconProps} />;
  if (platform === "facebook") return <FontAwesome6 name="facebook" {...brandIconProps} />;
  if (platform === "tiktok") return <FontAwesome6 name="tiktok" {...brandIconProps} />;
  if (platform === "pinterest") return <FontAwesome6 name="pinterest" {...brandIconProps} />;
  if (platform === "manual") return <PenLine {...iconProps} />;
  return <Globe2 {...iconProps} />;
}

function formatImportSource(platform: string | null, createdAt: string) {
  const source =
    platform === "instagram"
      ? "Instagram"
      : platform === "facebook"
        ? "Facebook"
        : platform === "tiktok"
          ? "TikTok"
          : platform === "pinterest"
            ? "Pinterest"
            : platform === "manual"
              ? "handmatig"
              : "website";
  const date = new Intl.DateTimeFormat("nl-NL", { day: "numeric", month: "short" }).format(new Date(createdAt)).replace(".", "");
  return platform === "manual" ? `Toegevoegd op ${date}` : `Geïmporteerd via ${source} · ${date}`;
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: colors.background
  },
  content: {
    paddingHorizontal: 24,
    paddingBottom: 126,
    gap: spacing.lg
  },
  title: {
    color: colors.text,
    ...typography.title
  },
  tagRail: {
    gap: spacing.xs,
    paddingRight: spacing.md
  },
  tagButton: {
    minHeight: 34,
    borderRadius: radii.pill,
    borderWidth: 1,
    borderColor: colors.border,
    justifyContent: "center",
    paddingHorizontal: spacing.md
  },
  tagButtonActive: {
    borderColor: colors.primaryDark,
    backgroundColor: colors.primarySoft
  },
  tagButtonText: {
    color: colors.muted,
    fontSize: 13,
    lineHeight: 17,
    fontWeight: "800"
  },
  tagButtonTextActive: {
    color: colors.primaryDark
  },
  list: {
    gap: spacing.md
  },
  card: {
    height: 126,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.border,
    overflow: "hidden",
    backgroundColor: colors.surface,
    flexDirection: "row",
    ...shadows.soft
  },
  image: {
    width: 112,
    height: 126,
    borderRadius: 0
  },
  cardBody: {
    flex: 1,
    justifyContent: "space-between",
    gap: 4,
    padding: spacing.md
  },
  cardHeader: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: spacing.sm
  },
  cardTitle: {
    flex: 1,
    color: colors.text,
    ...typography.cardTitle
  },
  sourceText: {
    color: colors.muted,
    fontSize: 12,
    lineHeight: 16
  },
  cardFooter: {
    flexDirection: "row",
    alignItems: "center"
  },
  time: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4
  },
  tagText: {
    color: colors.primaryDark,
    fontSize: 12,
    lineHeight: 16,
    fontWeight: "700"
  },
  meta: {
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
