import { PropsWithChildren } from "react";
import { ScrollView, StyleSheet, ViewStyle } from "react-native";

export function Screen({ children, style }: PropsWithChildren<{ style?: ViewStyle }>) {
  return <ScrollView contentContainerStyle={[styles.container, style]}>{children}</ScrollView>;
}

const styles = StyleSheet.create({
  container: {
    flexGrow: 1,
    padding: 20,
    gap: 16
  }
});
