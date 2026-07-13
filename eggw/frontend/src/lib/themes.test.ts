import { describe, expect, it } from "vitest";
import { runInNewContext } from "node:vm";
import { useAppStore } from "./store";
import {
  DEFAULT_THEME,
  THEMES,
  THEME_METADATA,
  isThemeName,
  monacoBaseTheme,
  normalizeThemeName,
  THEME_INITIALIZATION_SCRIPT,
} from "./themes";

const UNIFORM_THEMES = [
  "dark", "cyberpunk", "forest", "ocean", "sunset", "mono", "midnight", "disney",
  "fruit", "vegetables", "coffee", "matrix", "light", "light-mono", "colorful", "colorful-light",
];

describe("EggW theme registry", () => {
  it("contains exactly 31 unique themes in stable public order", () => {
    expect(THEMES).toHaveLength(31);
    expect(new Set(THEMES).size).toBe(31);
    expect(THEMES.slice(0, 16)).toEqual(UNIFORM_THEMES);
    expect(DEFAULT_THEME).toBe("dark");
  });

  it("classifies polarity and Monaco base from metadata rather than name heuristics", () => {
    for (const theme of THEMES) {
      const metadata = THEME_METADATA[theme];
      expect(metadata.name).toBe(theme);
      expect(["dark", "light"]).toContain(metadata.polarity);
      expect(["uniform", "tinted"]).toContain(metadata.treatment);
      expect(monacoBaseTheme(theme)).toBe(metadata.polarity === "light" ? "vs" : "vs-dark");
    }
    expect(monacoBaseTheme("colorful-light-background")).toBe("vs");
    expect(monacoBaseTheme("not-a-light-theme")).toBe("vs-dark");
  });

  it("normalizes unknown values and the store starts from the same default", () => {
    expect(isThemeName("coffee-background")).toBe(true);
    expect(isThemeName("unknown")).toBe(false);
    expect(normalizeThemeName("unknown")).toBe(DEFAULT_THEME);
    expect(normalizeThemeName(null)).toBe(DEFAULT_THEME);
    expect(useAppStore.getState().theme).toBe(DEFAULT_THEME);

    useAppStore.getState().setTheme("unknown");
    expect(useAppStore.getState().theme).toBe(DEFAULT_THEME);
    useAppStore.getState().setTheme("ocean-background");
    expect(useAppStore.getState().theme).toBe("ocean-background");
    useAppStore.setState({ theme: DEFAULT_THEME });
  });

  it.each([
    ["light-background", "light-background"],
    ["unknown-theme", DEFAULT_THEME],
    [null, DEFAULT_THEME],
  ])("applies saved theme %s before paint as %s", (saved, expected) => {
    const storage = new Map<string, string>();
    if (saved) storage.set("eggw-theme", saved);
    const document = { documentElement: { dataset: {} as Record<string, string> } };
    runInNewContext(THEME_INITIALIZATION_SCRIPT, {
      Set,
      document,
      localStorage: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value),
      },
    });
    expect(document.documentElement.dataset.theme).toBe(expected);
    expect(storage.get("eggw-theme")).toBe(expected);
  });
});
