import { useQuery } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { ChevronLeft, ChevronRight, ListChecks } from "lucide-react-native";
import { useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { colors, radii, shadows, spacing, typography } from "../../lib/theme";
import { supabase } from "../../lib/supabase";

type ShoppingListRow = {
  id: string;
  title: string;
  estimated_total_cents: number;
  scheduled_for: string;
  created_at: string;
  shopping_list_items: { id: string }[];
};

const weekdayLabels = ["Ma", "Di", "Wo", "Do", "Vr", "Za", "Zo"];

export default function ShoppingTab() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [month, setMonth] = useState(() => startOfMonth(new Date()));
  const today = toDateKey(new Date());
  const lists = useQuery({
    queryKey: ["shopping-lists-tab"],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("shopping_lists")
        .select("id,title,estimated_total_cents,scheduled_for,created_at,shopping_list_items(id)")
        .not("scheduled_for", "is", null)
        .gte("scheduled_for", today)
        .order("scheduled_for", { ascending: true })
        .order("created_at", { ascending: true });
      if (error) throw error;
      return (data ?? []) as ShoppingListRow[];
    }
  });

  const listsByDay = useMemo(() => {
    const grouped = new Map<string, ShoppingListRow[]>();
    for (const list of lists.data ?? []) {
      const current = grouped.get(list.scheduled_for) ?? [];
      current.push(list);
      grouped.set(list.scheduled_for, current);
    }
    return grouped;
  }, [lists.data]);

  const calendarDays = useMemo(() => getCalendarDays(month), [month]);

  return (
    <View style={styles.screen}>
      <ScrollView
        contentContainerStyle={[styles.content, { paddingTop: Math.max(insets.top + 24, 54) }]}
        showsVerticalScrollIndicator={false}
      >
        <Text style={styles.title}>Boodschappen</Text>

        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Komende lijsten</Text>
          {lists.isLoading ? <Text style={styles.meta}>Lijsten laden...</Text> : null}
          {lists.error ? <Text style={styles.error}>{String(lists.error.message)}</Text> : null}
          {lists.data?.map((list) => (
            <Pressable
              key={list.id}
              onPress={() => router.push({ pathname: "/shopping/[id]", params: { id: list.id } })}
              style={({ pressed }) => [styles.listCard, pressed && styles.pressed]}
            >
              <View style={styles.listIcon}>
                <ListChecks color={colors.primaryDark} size={23} strokeWidth={2.3} />
              </View>
              <View style={styles.listCopy}>
                <Text style={styles.listDate}>{formatDate(list.scheduled_for)}</Text>
                <Text numberOfLines={1} style={styles.listTitle}>
                  {list.title}
                </Text>
                <Text style={styles.meta}>{list.shopping_list_items?.length ?? 0} producten</Text>
              </View>
              <Text style={styles.price}>EUR {(list.estimated_total_cents / 100).toFixed(2)}</Text>
            </Pressable>
          ))}
          {lists.data?.length === 0 && !lists.isLoading ? (
            <Text style={styles.meta}>Voeg ingredienten vanuit een recept toe en kies een boodschappendag.</Text>
          ) : null}
        </View>

        <View style={styles.section}>
          <View style={styles.monthHeader}>
            <Text style={styles.sectionTitle}>{formatMonth(month)}</Text>
            <View style={styles.monthControls}>
              <Pressable
                accessibilityLabel="Vorige maand"
                accessibilityRole="button"
                onPress={() => setMonth((current) => addMonths(current, -1))}
                style={({ pressed }) => [styles.iconButton, pressed && styles.pressed]}
              >
                <ChevronLeft color={colors.text} size={22} strokeWidth={2.5} />
              </Pressable>
              <Pressable
                accessibilityLabel="Volgende maand"
                accessibilityRole="button"
                onPress={() => setMonth((current) => addMonths(current, 1))}
                style={({ pressed }) => [styles.iconButton, pressed && styles.pressed]}
              >
                <ChevronRight color={colors.text} size={22} strokeWidth={2.5} />
              </Pressable>
            </View>
          </View>

          <View style={styles.calendar}>
            {weekdayLabels.map((label) => (
              <Text key={label} style={styles.weekday}>
                {label}
              </Text>
            ))}
            {calendarDays.map((day) => {
              const plannedLists = listsByDay.get(day.key) ?? [];
              const hasList = plannedLists.length > 0;
              const isToday = day.key === today;

              return (
                <Pressable
                  accessibilityLabel={hasList ? `${formatDate(day.key)}: ${plannedLists.length} boodschappenlijst` : formatDate(day.key)}
                  accessibilityRole={hasList ? "button" : undefined}
                  disabled={!hasList}
                  key={day.key}
                  onPress={() => router.push({ pathname: "/shopping/[id]", params: { id: plannedLists[0].id } })}
                  style={({ pressed }) => [
                    styles.dayCell,
                    !day.inMonth && styles.dayCellOutside,
                    isToday && styles.dayCellToday,
                    hasList && styles.dayCellPlanned,
                    pressed && hasList && styles.pressed
                  ]}
                >
                  <Text style={[styles.dayCellText, !day.inMonth && styles.dayCellTextOutside, hasList && styles.dayCellTextPlanned]}>
                    {day.date.getDate()}
                  </Text>
                  {hasList ? <View style={styles.calendarDot} /> : null}
                </Pressable>
              );
            })}
          </View>
        </View>
      </ScrollView>
    </View>
  );
}

function startOfMonth(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), 1, 12);
}

function addMonths(date: Date, count: number) {
  return new Date(date.getFullYear(), date.getMonth() + count, 1, 12);
}

function getCalendarDays(month: Date) {
  const firstWeekday = (month.getDay() + 6) % 7;
  const firstCalendarDay = new Date(month.getFullYear(), month.getMonth(), 1 - firstWeekday, 12);

  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(firstCalendarDay);
    date.setDate(firstCalendarDay.getDate() + index);
    return { date, key: toDateKey(date), inMonth: date.getMonth() === month.getMonth() };
  });
}

function toDateKey(date: Date) {
  const offsetDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return offsetDate.toISOString().slice(0, 10);
}

function formatDate(dateKey: string) {
  return new Intl.DateTimeFormat("nl-NL", { weekday: "short", day: "numeric", month: "short" })
    .format(new Date(`${dateKey}T12:00:00`))
    .replace(".", "");
}

function formatMonth(date: Date) {
  const label = new Intl.DateTimeFormat("nl-NL", { month: "long", year: "numeric" }).format(date);
  return label.charAt(0).toUpperCase() + label.slice(1);
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: colors.background
  },
  content: {
    paddingHorizontal: 24,
    paddingBottom: 126,
    gap: spacing.xl
  },
  title: {
    color: colors.text,
    ...typography.title
  },
  section: {
    gap: spacing.md
  },
  sectionTitle: {
    color: colors.text,
    ...typography.cardTitle
  },
  listCard: {
    minHeight: 86,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    alignItems: "center",
    flexDirection: "row",
    gap: spacing.sm,
    padding: spacing.md,
    ...shadows.soft
  },
  listIcon: {
    width: 44,
    height: 44,
    borderRadius: radii.md,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.primarySoft
  },
  listCopy: {
    flex: 1,
    gap: 2
  },
  listDate: {
    color: colors.primaryDark,
    fontSize: 13,
    lineHeight: 17,
    fontWeight: "800"
  },
  listTitle: {
    color: colors.text,
    ...typography.label
  },
  meta: {
    color: colors.muted,
    fontSize: 13,
    lineHeight: 18
  },
  price: {
    color: colors.text,
    fontSize: 13,
    lineHeight: 18,
    fontWeight: "800"
  },
  monthHeader: {
    alignItems: "center",
    flexDirection: "row",
    justifyContent: "space-between",
    gap: spacing.md
  },
  monthControls: {
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
    justifyContent: "center"
  },
  calendar: {
    borderTopWidth: 1,
    borderLeftWidth: 1,
    borderColor: colors.border,
    flexDirection: "row",
    flexWrap: "wrap"
  },
  weekday: {
    width: "14.2857%",
    minHeight: 30,
    borderBottomWidth: 1,
    borderRightWidth: 1,
    borderColor: colors.border,
    color: colors.muted,
    paddingTop: 7,
    textAlign: "center",
    fontSize: 11,
    lineHeight: 15,
    fontWeight: "800"
  },
  dayCell: {
    width: "14.2857%",
    aspectRatio: 1,
    borderBottomWidth: 1,
    borderRightWidth: 1,
    borderColor: colors.border,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.surface
  },
  dayCellOutside: {
    backgroundColor: "#fafafa"
  },
  dayCellToday: {
    backgroundColor: colors.primarySoft
  },
  dayCellPlanned: {
    backgroundColor: colors.tabActive
  },
  dayCellText: {
    color: colors.text,
    fontSize: 14,
    lineHeight: 18,
    fontWeight: "700"
  },
  dayCellTextOutside: {
    color: colors.muted
  },
  dayCellTextPlanned: {
    color: colors.primaryDark,
    fontWeight: "800"
  },
  calendarDot: {
    position: "absolute",
    bottom: 7,
    width: 5,
    height: 5,
    borderRadius: radii.pill,
    backgroundColor: colors.primaryDark
  },
  pressed: {
    opacity: 0.78
  },
  error: {
    color: colors.danger,
    fontWeight: "700"
  }
});
