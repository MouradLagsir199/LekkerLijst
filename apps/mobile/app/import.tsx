import { ParsedRecipeSchema } from "@recipe-nl/shared";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { Button } from "../components/Button";
import { Screen } from "../components/Screen";
import { setImportDraft } from "../lib/importDraft";
import { supabase } from "../lib/supabase";
import { colors, radii, spacing, typography } from "../lib/theme";

export default function ImportScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ mode?: string }>();
  const [mode, setMode] = useState<"text" | "url">(params.mode === "url" ? "url" : "text");
  const [rawText, setRawText] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function importRecipe() {
    setBusy(true);
    setError(null);

    const body =
      mode === "url"
        ? { sourceUrl: sourceUrl.trim() }
        : { rawText: rawText.trim(), sourceUrl: null };

    const { data, error: invokeError } = await supabase.functions.invoke("import-recipe", {
      body
    });

    setBusy(false);

    if (invokeError) {
      setError(await getFunctionErrorMessage(invokeError));
      return;
    }

    const parsed = ParsedRecipeSchema.safeParse(data?.recipe);
    if (!parsed.success) {
      setError(parsed.error.issues.map((issue) => issue.message).join(", "));
      return;
    }

    setImportDraft({
      recipe: parsed.data,
      sourceText: typeof data?.completionSourceText === "string" ? data.completionSourceText : undefined
    });
    router.push("/review");
  }

  return (
    <Screen>
      <Text style={styles.title}>{mode === "url" ? "Plak een link" : "Plak je recept"}</Text>

      <View style={styles.segmented}>
        <ModeButton active={mode === "text"} label="Tekst" onPress={() => setMode("text")} />
        <ModeButton active={mode === "url"} label="Link" onPress={() => setMode("url")} />
      </View>

      {mode === "url" ? (
        <>
          <Text style={styles.help}>We lezen openbare recepttitels, captions en bronpagina's. Links zonder openbare recepttekst kunnen niet worden geimporteerd.</Text>
          <TextInput
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
            onChangeText={setSourceUrl}
            placeholder="https://www.tiktok.com/@..."
            style={styles.input}
            value={sourceUrl}
          />
        </>
      ) : (
        <TextInput
          multiline
          onChangeText={setRawText}
          placeholder="Bijvoorbeeld: 200g bloem, 2 eieren..."
          style={styles.textarea}
          textAlignVertical="top"
          value={rawText}
        />
      )}
      {error ? <Text style={styles.error}>{error}</Text> : null}
      {busy ? <ActivityIndicator /> : null}
      <Button disabled={busy || !canSubmit(mode, rawText, sourceUrl)} onPress={importRecipe} title="Importeren en parseren" />
    </Screen>
  );
}

function ModeButton({ active, label, onPress }: { active: boolean; label: string; onPress: () => void }) {
  return (
    <Pressable onPress={onPress} style={[styles.modeButton, active && styles.modeButtonActive]}>
      <Text style={[styles.modeButtonText, active && styles.modeButtonTextActive]}>{label}</Text>
    </Pressable>
  );
}

function canSubmit(mode: "text" | "url", rawText: string, sourceUrl: string) {
  if (mode === "text") return rawText.trim().length >= 20;
  try {
    const url = new URL(sourceUrl.trim());
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

async function getFunctionErrorMessage(error: unknown) {
  const fallback = error instanceof Error ? error.message : "Importeren mislukt";
  const context = (error as { context?: unknown })?.context;

  if (context && typeof (context as Response).json === "function") {
    try {
      const payload = (await (context as Response).json()) as { error?: unknown; details?: unknown };
      const message = typeof payload.error === "string" ? payload.error : null;
      const details = typeof payload.details === "string" ? payload.details : null;
      return [message, details].filter(Boolean).join(": ") || fallback;
    } catch {
      return fallback;
    }
  }

  return fallback;
}

const styles = StyleSheet.create({
  title: {
    color: colors.text,
    ...typography.sectionTitle
  },
  segmented: {
    borderRadius: radii.md,
    backgroundColor: colors.primarySoft,
    flexDirection: "row",
    padding: 4
  },
  modeButton: {
    flex: 1,
    minHeight: 42,
    borderRadius: radii.sm,
    alignItems: "center",
    justifyContent: "center"
  },
  modeButtonActive: {
    backgroundColor: colors.surface
  },
  modeButtonText: {
    color: colors.muted,
    fontWeight: "800"
  },
  modeButtonTextActive: {
    color: colors.text
  },
  help: {
    color: colors.muted,
    ...typography.body
  },
  input: {
    minHeight: 54,
    borderRadius: radii.sm,
    borderColor: colors.border,
    borderWidth: 1,
    backgroundColor: colors.surface,
    paddingHorizontal: spacing.md,
    fontSize: 16
  },
  textarea: {
    minHeight: 280,
    borderRadius: radii.sm,
    borderColor: colors.border,
    borderWidth: 1,
    backgroundColor: colors.surface,
    padding: 14,
    fontSize: 16,
    lineHeight: 22
  },
  error: {
    color: colors.danger,
    fontWeight: "700"
  }
});
