import "react-native-url-polyfill/auto";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { useState } from "react";

import { AuthProvider } from "../lib/auth";
import { colors } from "../lib/theme";

export default function RootLayout() {
  const [queryClient] = useState(() => new QueryClient());

  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <StatusBar style="dark" />
        <Stack
          screenOptions={{
            headerStyle: { backgroundColor: colors.background },
            headerShadowVisible: false,
            contentStyle: { backgroundColor: colors.background },
            headerTitleStyle: {
              color: colors.text,
              fontWeight: "800"
            }
          }}
        >
          <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
          <Stack.Screen name="login" options={{ headerShown: false }} />
          <Stack.Screen name="import" options={{ title: "Recept importeren" }} />
          <Stack.Screen name="review" options={{ title: "Controleer recept" }} />
          <Stack.Screen name="recipe/[id]" options={{ title: "Recept" }} />
          <Stack.Screen name="shopping/[id]" options={{ title: "Boodschappenlijst" }} />
        </Stack>
      </AuthProvider>
    </QueryClientProvider>
  );
}
