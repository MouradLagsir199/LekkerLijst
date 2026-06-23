import { ScrollView, StyleSheet, Text, View } from "react-native";

import { Button } from "../../components/Button";
import { TodoPanel } from "../../components/TodoPanel";
import { useAuth } from "../../lib/auth";
import { colors, spacing, typography } from "../../lib/theme";

export default function ProfileTab() {
  const { session, signOut } = useAuth();

  return (
    <View style={styles.screen}>
      <ScrollView contentContainerStyle={styles.content}>
        <Text style={styles.title}>Profiel</Text>
        <View style={styles.profileBlock}>
          <Text style={styles.label}>Ingelogd als</Text>
          <Text style={styles.email}>{session?.user.email}</Text>
        </View>
        <TodoPanel
          title="Voorkeuren"
          body="Preferred supermarket, taal, huishouden en importlimieten komen hier in de volgende productlaag."
        />
        <Button onPress={signOut} title="Uitloggen" variant="secondary" />
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: colors.background
  },
  content: {
    paddingTop: 78,
    paddingHorizontal: 24,
    paddingBottom: 126,
    gap: spacing.lg
  },
  title: {
    color: colors.text,
    ...typography.title
  },
  profileBlock: {
    gap: 6
  },
  label: {
    color: colors.muted,
    ...typography.label
  },
  email: {
    color: colors.text,
    ...typography.cardTitle
  }
});
