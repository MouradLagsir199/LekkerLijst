import { StyleSheet, Text, View } from "react-native";

import { colors, radii, spacing, typography } from "../lib/theme";

export function TodoPanel({ title, body }: { title: string; body: string }) {
  return (
    <View style={styles.panel}>
      <Text style={styles.tag}>#todo</Text>
      <Text style={styles.title}>{title}</Text>
      <Text style={styles.body}>{body}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  panel: {
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    gap: spacing.sm,
    padding: spacing.lg
  },
  tag: {
    alignSelf: "flex-start",
    borderRadius: radii.pill,
    backgroundColor: colors.primarySoft,
    color: colors.primaryDark,
    overflow: "hidden",
    paddingHorizontal: spacing.sm,
    paddingVertical: 4,
    ...typography.label
  },
  title: {
    color: colors.text,
    ...typography.cardTitle
  },
  body: {
    color: colors.muted,
    ...typography.body
  }
});
