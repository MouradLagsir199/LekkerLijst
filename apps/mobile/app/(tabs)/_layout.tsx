import { Tabs, useRouter } from "expo-router";
import { CalendarDays, Home, ReceiptText, ShoppingCart, UserRound } from "lucide-react-native";
import { useEffect } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useAuth } from "../../lib/auth";
import { colors, radii, shadows } from "../../lib/theme";

const TABS = {
  index: { label: "Home", Icon: Home },
  recipes: { label: "Recepten", Icon: ReceiptText },
  shopping: { label: "Boodschappen", Icon: ShoppingCart },
  planning: { label: "Planning", Icon: CalendarDays },
  profile: { label: "Profiel", Icon: UserRound }
};

export default function TabsLayout() {
  const router = useRouter();
  const { session, loading } = useAuth();

  useEffect(() => {
    if (!loading && !session) {
      router.replace("/login");
    }
  }, [loading, router, session]);

  if (loading || !session) {
    return (
      <View style={styles.loading}>
        <ActivityIndicator />
      </View>
    );
  }

  return (
    <Tabs
      screenOptions={{ headerShown: false }}
      tabBar={(props) => <TabBar {...props} />}
    >
      <Tabs.Screen name="index" />
      <Tabs.Screen name="recipes" />
      <Tabs.Screen name="shopping" />
      <Tabs.Screen name="planning" />
      <Tabs.Screen name="profile" />
    </Tabs>
  );
}

function TabBar({ state, navigation }: any) {
  const insets = useSafeAreaInsets();

  return (
    <View style={[styles.tabWrap, { bottom: Math.max(insets.bottom + 8, 14) }]}>
      {state.routes.map((route: any, index: number) => {
        const active = state.index === index;
        const config = TABS[route.name as keyof typeof TABS];
        if (!config) return null;

        const { Icon, label } = config;
        return (
          <Pressable
            key={route.key}
            onPress={() => navigation.navigate(route.name)}
            style={({ pressed }) => [styles.tabItem, pressed && styles.pressed]}
          >
            <View style={[styles.iconBubble, active && styles.iconBubbleActive]}>
              <Icon color={active ? colors.primaryDark : colors.text} size={25} strokeWidth={2.4} />
            </View>
            <Text style={[styles.tabLabel, active && styles.tabLabelActive]} numberOfLines={1}>
              {label}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  loading: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.background
  },
  tabWrap: {
    position: "absolute",
    left: 14,
    right: 14,
    minHeight: 72,
    borderRadius: 24,
    backgroundColor: colors.surface,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 8,
    paddingVertical: 8,
    ...shadows.card
  },
  tabItem: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 1
  },
  pressed: {
    opacity: 0.75
  },
  iconBubble: {
    width: 42,
    height: 42,
    borderRadius: radii.md,
    alignItems: "center",
    justifyContent: "center"
  },
  iconBubbleActive: {
    backgroundColor: colors.tabActive
  },
  tabLabel: {
    color: colors.text,
    fontSize: 10,
    lineHeight: 13,
    fontWeight: "500"
  },
  tabLabelActive: {
    fontWeight: "700"
  }
});
