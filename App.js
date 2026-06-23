import { ExpoRoot } from "expo-router";

export default function App() {
  const context = require.context("./apps/mobile/app", true, /\.[jt]sx?$/);
  return <ExpoRoot context={context} />;
}
