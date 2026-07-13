import { describe, expect, it } from "vitest";
import { eggwSyntaxTheme } from "./syntaxTheme";

const serialized = JSON.stringify(eggwSyntaxTheme);

describe("semantic syntax theme", () => {
  it("uses the EggW semantic contract instead of a fixed dark palette", () => {
    expect(eggwSyntaxTheme['pre[class*="language-"]'].background).toBe("var(--code-surface)");
    expect(eggwSyntaxTheme['code[class*="language-"]'].color).toBe("var(--code-text)");
    for (const token of ["comment", "keyword", "string", "number", "function", "variable", "operator"]) {
      expect(serialized).toContain(`--syntax-${token}`);
    }
    expect(serialized).not.toMatch(/#[0-9a-f]{3,8}/i);
  });
});
