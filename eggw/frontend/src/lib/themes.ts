import contract from "../../../eggw/theme-contract.json";

export type ThemePolarity = "dark" | "light";
export type ThemeTreatment = "uniform" | "tinted";

export interface ThemeMetadata {
  name: string;
  family: string;
  polarity: ThemePolarity;
  treatment: ThemeTreatment;
}

const metadata = contract.themes as ThemeMetadata[];

export const DEFAULT_THEME = contract.defaultTheme;
export const THEMES = Object.freeze(metadata.map((theme) => theme.name));
export const THEME_METADATA: Readonly<Record<string, ThemeMetadata>> = Object.freeze(
  Object.fromEntries(metadata.map((theme) => [theme.name, Object.freeze(theme)])),
);

export type ThemeName = (typeof THEMES)[number];

export function isThemeName(value: unknown): value is ThemeName {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(THEME_METADATA, value);
}

export function normalizeThemeName(value: unknown): ThemeName {
  return isThemeName(value) ? value : DEFAULT_THEME;
}

export function themePolarity(theme: unknown): ThemePolarity {
  return THEME_METADATA[normalizeThemeName(theme)].polarity;
}

export function monacoBaseTheme(theme: unknown): "vs" | "vs-dark" {
  return themePolarity(theme) === "light" ? "vs" : "vs-dark";
}

/** Inline, dependency-free initializer that runs before React or the auth gate paints. */
export const THEME_INITIALIZATION_SCRIPT = `(() => {
  const names = new Set(${JSON.stringify(THEMES)});
  const fallback = ${JSON.stringify(DEFAULT_THEME)};
  let saved = null;
  try { saved = localStorage.getItem("eggw-theme"); } catch {}
  const theme = names.has(saved) ? saved : fallback;
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("eggw-theme", theme); } catch {}
})();`;
