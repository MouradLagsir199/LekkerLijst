import { Pressable, PressableProps, StyleSheet, Text } from "react-native";

export function Button({
  title,
  variant = "primary",
  disabled,
  ...props
}: PressableProps & { title: string; variant?: "primary" | "secondary" }) {
  return (
    <Pressable
      {...props}
      disabled={disabled}
      style={({ pressed }) => [
        styles.button,
        variant === "secondary" && styles.secondary,
        disabled && styles.disabled,
        pressed && !disabled && styles.pressed
      ]}
    >
      <Text style={[styles.text, variant === "secondary" && styles.secondaryText]}>{title}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  button: {
    minHeight: 48,
    borderRadius: 8,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "#0f766e",
    paddingHorizontal: 16
  },
  secondary: {
    backgroundColor: "#e2e8f0"
  },
  disabled: {
    opacity: 0.55
  },
  pressed: {
    opacity: 0.86
  },
  text: {
    color: "#ffffff",
    fontSize: 16,
    fontWeight: "700"
  },
  secondaryText: {
    color: "#0f172a"
  }
});
