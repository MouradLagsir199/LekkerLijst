import { CookingPot } from "lucide-react-native";
import { useEffect, useState } from "react";
import { Image, StyleSheet, View } from "react-native";

import { colors, radii } from "../lib/theme";

export function RecipeImage({ uri, style }: { uri?: string | null; style?: any }) {
  const [failed, setFailed] = useState(false);

  useEffect(() => setFailed(false), [uri]);

  if (uri && !failed) {
    return <Image accessibilityLabel="Receptafbeelding" onError={() => setFailed(true)} source={{ uri }} style={[styles.image, style]} />;
  }

  return (
    <View accessibilityLabel="Geen receptafbeelding beschikbaar" style={[styles.placeholder, style]}>
      <CookingPot color={colors.primaryDark} size={30} strokeWidth={2.1} />
    </View>
  );
}

const styles = StyleSheet.create({
  image: {
    backgroundColor: colors.primarySoft
  },
  placeholder: {
    borderRadius: radii.md,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.primarySoft
  }
});
