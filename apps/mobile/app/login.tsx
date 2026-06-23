import { useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { ActivityIndicator, StyleSheet, Text, TextInput, View } from "react-native";

import { Button } from "../components/Button";
import { Screen } from "../components/Screen";
import { useAuth } from "../lib/auth";
import { isSupabaseConfigured, supabase } from "../lib/supabase";

export default function LoginScreen() {
  const router = useRouter();
  const { session, loading } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!loading && session) {
      router.replace("/");
    }
  }, [loading, router, session]);

  async function signIn() {
    setBusy(true);
    setMessage(null);
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    setBusy(false);
    if (error) setMessage(error.message);
  }

  async function signUp() {
    setBusy(true);
    setMessage(null);
    const { error } = await supabase.auth.signUp({ email, password });
    setBusy(false);
    if (error) {
      setMessage(error.message);
    } else {
      setMessage("Account gemaakt. Controleer je e-mail als bevestiging nodig is.");
    }
  }

  if (loading) {
    return (
      <Screen style={styles.center}>
        <ActivityIndicator />
      </Screen>
    );
  }

  return (
    <Screen>
      <View style={styles.header}>
        <Text style={styles.kicker}>Recipe NL</Text>
        <Text style={styles.title}>Log in om recepten te importeren.</Text>
      </View>

      {!isSupabaseConfigured ? (
        <Text style={styles.error}>Vul EXPO_PUBLIC_SUPABASE_URL en EXPO_PUBLIC_SUPABASE_ANON_KEY in.</Text>
      ) : null}

      <View style={styles.form}>
        <TextInput
          autoCapitalize="none"
          autoComplete="email"
          keyboardType="email-address"
          onChangeText={setEmail}
          placeholder="E-mail"
          style={styles.input}
          value={email}
        />
        <TextInput
          autoCapitalize="none"
          onChangeText={setPassword}
          placeholder="Wachtwoord"
          secureTextEntry
          style={styles.input}
          value={password}
        />
        {message ? <Text style={styles.message}>{message}</Text> : null}
      </View>

      <Button disabled={busy || !isSupabaseConfigured} onPress={signIn} title={busy ? "Bezig..." : "Inloggen"} />
      <Button disabled={busy || !isSupabaseConfigured} onPress={signUp} title="Account maken" variant="secondary" />
    </Screen>
  );
}

const styles = StyleSheet.create({
  center: {
    justifyContent: "center"
  },
  header: {
    gap: 6
  },
  kicker: {
    color: "#0f766e",
    fontWeight: "800",
    letterSpacing: 0,
    textTransform: "uppercase"
  },
  title: {
    color: "#0f172a",
    fontSize: 28,
    fontWeight: "800",
    lineHeight: 34
  },
  form: {
    gap: 10
  },
  input: {
    minHeight: 48,
    borderRadius: 8,
    borderColor: "#cbd5e1",
    borderWidth: 1,
    backgroundColor: "#ffffff",
    paddingHorizontal: 14,
    fontSize: 16
  },
  message: {
    color: "#334155"
  },
  error: {
    color: "#b91c1c",
    fontWeight: "700"
  }
});
