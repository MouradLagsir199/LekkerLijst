import { ParsedRecipeSchema } from "@recipe-nl/shared";
import { useLocalSearchParams, useRouter } from "expo-router";
import { Check, LoaderCircle } from "lucide-react-native";
import { useRef, useState } from "react";
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
  const [progress, setProgress] = useState(0);
  const [stageIndex, setStageIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const progressTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  async function importRecipe() {
    const stages = getImportStages(mode);
    setBusy(true);
    setError(null);
    setStageIndex(0);
    setProgress(stages[0].progress);

    let nextStage = 0;
    progressTimer.current = setInterval(() => {
      nextStage = Math.min(nextStage + 1, stages.length - 1);
      setStageIndex(nextStage);
      setProgress(stages[nextStage].progress);
    }, 1_600);

    const body =
      mode === "url"
        ? { sourceUrl: sourceUrl.trim() }
        : { rawText: rawText.trim(), sourceUrl: null };

    try {
      const { data, error: invokeError } = await supabase.functions.invoke("import-recipe", {
        body
      });

      if (invokeError) {
        setError(await getFunctionErrorMessage(invokeError));
        return;
      }

      const parsed = ParsedRecipeSchema.safeParse(data?.recipe);
      if (!parsed.success) {
        setError(parsed.error.issues.map((issue) => issue.message).join(", "));
        return;
      }

      setStageIndex(stages.length);
      setProgress(100);
      await wait(220);

      setImportDraft({
        recipe: parsed.data,
        sourceText: typeof data?.completionSourceText === "string" ? data.completionSourceText : undefined
      });
      router.push("/review");
    } catch (importError) {
      setError(importError instanceof Error ? importError.message : "Importeren mislukt.");
    } finally {
      if (progressTimer.current) clearInterval(progressTimer.current);
      progressTimer.current = null;
      setBusy(false);
    }
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
      {busy ? <ImportProgress progress={progress} stageIndex={stageIndex} stages={getImportStages(mode)} /> : null}
      {error ? <Text style={styles.error}>{error}</Text> : null}
      <Button disabled={busy || !canSubmit(mode, rawText, sourceUrl)} onPress={importRecipe} title="Importeren" />
    </Screen>
  );
}

type ImportStage = {
  label: string;
  progress: number;
};

function getImportStages(mode: "text" | "url"): ImportStage[] {
  return mode === "url"
    ? [
        { label: "Link controleren", progress: 12 },
        { label: "Caption en receptinformatie ophalen", progress: 34 },
        { label: "Media en transcript verwerken", progress: 58 },
        { label: "Recept naar het Nederlands omzetten", progress: 78 },
        { label: "Ingrediënten en stappen controleren", progress: 94 }
      ]
    : [
        { label: "Recepttekst lezen", progress: 18 },
        { label: "Ingrediënten herkennen", progress: 42 },
        { label: "Recept naar het Nederlands omzetten", progress: 70 },
        { label: "Stappen en hoeveelheden controleren", progress: 94 }
      ];
}

function ImportProgress({ progress, stageIndex, stages }: { progress: number; stageIndex: number; stages: ImportStage[] }) {
  return (
    <View style={styles.progressPanel}>
      <View style={styles.progressHeader}>
        <Text style={styles.progressTitle}>Recept importeren</Text>
        <Text style={styles.progressPercent}>{progress}%</Text>
      </View>
      <View style={styles.progressTrack}>
        <View style={[styles.progressFill, { width: `${progress}%` }]} />
      </View>
      <View style={styles.stageList}>
        {stages.map((stage, index) => {
          const complete = stageIndex > index;
          const active = stageIndex === index;
          return (
            <View key={stage.label} style={styles.stageRow}>
              <View style={[styles.stageIndicator, complete && styles.stageIndicatorComplete, active && styles.stageIndicatorActive]}>
                {complete ? <Check color={colors.surface} size={14} strokeWidth={3} /> : active ? <LoaderCircle color={colors.primaryDark} size={15} strokeWidth={2.6} /> : null}
              </View>
              <Text style={[styles.stageText, (complete || active) && styles.stageTextActive]}>{stage.label}</Text>
            </View>
          );
        })}
      </View>
    </View>
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

function wait(milliseconds: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, milliseconds));
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
  },
  progressPanel: {
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    gap: spacing.sm,
    padding: spacing.md
  },
  progressHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between"
  },
  progressTitle: {
    color: colors.text,
    ...typography.label
  },
  progressPercent: {
    color: colors.primaryDark,
    fontSize: 13,
    lineHeight: 18,
    fontWeight: "800"
  },
  progressTrack: {
    height: 8,
    borderRadius: radii.pill,
    overflow: "hidden",
    backgroundColor: colors.border
  },
  progressFill: {
    height: "100%",
    borderRadius: radii.pill,
    backgroundColor: colors.primaryDark
  },
  stageList: {
    gap: spacing.xs
  },
  stageRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm
  },
  stageIndicator: {
    width: 20,
    height: 20,
    borderRadius: radii.pill,
    borderWidth: 1,
    borderColor: colors.border,
    alignItems: "center",
    justifyContent: "center"
  },
  stageIndicatorComplete: {
    borderColor: colors.primaryDark,
    backgroundColor: colors.primaryDark
  },
  stageIndicatorActive: {
    borderColor: colors.primary,
    backgroundColor: colors.primarySoft
  },
  stageText: {
    color: colors.muted,
    fontSize: 13,
    lineHeight: 18
  },
  stageTextActive: {
    color: colors.text,
    fontWeight: "700"
  }
});
