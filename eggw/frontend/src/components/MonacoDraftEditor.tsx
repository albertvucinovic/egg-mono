"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Editor, loader, useMonaco, type BeforeMount, type Monaco, type OnMount } from "@monaco-editor/react";
import * as monacoPackage from "monaco-editor";
import type { editor as MonacoEditor } from "monaco-editor";
import { useAppStore } from "@/lib/store";
import { PlainDraftEditor, type DraftEditorProps } from "@/components/PlainDraftEditor";

loader.config({ monaco: monacoPackage as unknown as Monaco });

export type MonacoDraftEditorProps = DraftEditorProps;

function cssVariable(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const value = window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function normalizeHexColor(value: string, fallback: string): string {
  const trimmed = value.trim();
  if (/^#[0-9a-f]{3}$/i.test(trimmed)) {
    return `#${trimmed[1]}${trimmed[1]}${trimmed[2]}${trimmed[2]}${trimmed[3]}${trimmed[3]}`;
  }
  if (/^#[0-9a-f]{6}$/i.test(trimmed)) return trimmed;
  if (/^#[0-9a-f]{8}$/i.test(trimmed)) return trimmed;
  return fallback;
}

function withAlpha(hex: string, alpha: string, fallback: string): string {
  const normalized = normalizeHexColor(hex, fallback);
  if (/^#[0-9a-f]{6}$/i.test(normalized)) return `${normalized}${alpha}`;
  return normalized;
}

function monacoBaseTheme(themeName: string): "vs" | "vs-dark" {
  return themeName.includes("light") ? "vs" : "vs-dark";
}

function safeThemeName(themeName: string): string {
  return `eggw-edit-answer-${themeName.replace(/[^a-z0-9_-]/gi, "-")}`;
}

function modelPath(sourceMsgId: string): string {
  const suffix = sourceMsgId ? sourceMsgId.replace(/[^a-zA-Z0-9_-]/g, "-") : "draft";
  return `inmemory://eggw/edit-answer-${suffix}.md`;
}

export function MonacoDraftEditor(props: MonacoDraftEditorProps) {
  const { value, onChange, sourceMsgId, canSubmitShortcut, onSubmitShortcut } = props;
  const theme = useAppStore((state) => state.theme);
  const monaco = useMonaco();
  const editorRef = useRef<MonacoEditor.IStandaloneCodeEditor | null>(null);
  const submitShortcutRef = useRef(onSubmitShortcut);
  const canSubmitShortcutRef = useRef(canSubmitShortcut);
  const [isReady, setIsReady] = useState(false);
  const [useFallback, setUseFallback] = useState(false);
  const monacoThemeName = useMemo(() => safeThemeName(theme), [theme]);

  useEffect(() => {
    submitShortcutRef.current = onSubmitShortcut;
  }, [onSubmitShortcut]);

  useEffect(() => {
    canSubmitShortcutRef.current = canSubmitShortcut;
  }, [canSubmitShortcut]);

  useEffect(() => {
    if (isReady) return;
    const timeout = window.setTimeout(() => setUseFallback(true), 10_000);
    return () => window.clearTimeout(timeout);
  }, [isReady]);

  const defineEggwMonacoTheme = useCallback((monacoInstance: Monaco) => {
    const foreground = normalizeHexColor(cssVariable("--foreground", "#d1d5db"), "#d1d5db");
    const background = normalizeHexColor(cssVariable("--code-bg", cssVariable("--panel-bg", "#0d1117")), "#0d1117");
    const border = normalizeHexColor(cssVariable("--panel-border", "#30363d"), "#30363d");
    const muted = normalizeHexColor(cssVariable("--muted", "#8b949e"), "#8b949e");
    const accent = normalizeHexColor(cssVariable("--accent", "#58a6ff"), "#58a6ff");

    monacoInstance.editor.defineTheme(monacoThemeName, {
      base: monacoBaseTheme(theme),
      inherit: true,
      rules: [],
      colors: {
        "editor.background": background,
        "editor.foreground": foreground,
        "editorCursor.foreground": accent,
        "editorLineNumber.foreground": muted,
        "editorLineNumber.activeForeground": accent,
        "editor.selectionBackground": withAlpha(accent, "55", "#264f78"),
        "editor.inactiveSelectionBackground": withAlpha(accent, "33", "#3a3d41"),
        "editor.lineHighlightBackground": withAlpha(border, "55", "#1f2937"),
        "editorWidget.background": background,
        "editorWidget.border": border,
        "input.background": background,
        "input.foreground": foreground,
        "focusBorder": accent,
      },
    });
  }, [monacoThemeName, theme]);

  useEffect(() => {
    if (!monaco) return;
    const monacoInstance = monaco as unknown as Monaco;
    defineEggwMonacoTheme(monacoInstance);
    monacoInstance.editor.setTheme(monacoThemeName);
  }, [defineEggwMonacoTheme, monaco, monacoThemeName]);

  const beforeMount = useCallback<BeforeMount>((monacoInstance) => {
    defineEggwMonacoTheme(monacoInstance);
  }, [defineEggwMonacoTheme]);

  const handleMount = useCallback<OnMount>((editor, monaco) => {
    editorRef.current = editor;
    setIsReady(true);
    setUseFallback(false);
    editor.focus();
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => {
      if (canSubmitShortcutRef.current) {
        submitShortcutRef.current();
      }
    });
  }, []);

  useEffect(() => {
    if (!isReady) return;
    editorRef.current?.focus();
  }, [isReady, sourceMsgId]);

  if (useFallback) {
    return <PlainDraftEditor {...props} />;
  }

  return (
    <div
      className="overflow-hidden rounded border"
      style={{ background: "var(--code-bg)", borderColor: "var(--panel-border)" }}
      data-testid="edit-answer-draft"
      data-editor="monaco"
      aria-label="Quoted assistant markdown draft"
    >
      <Editor
        height="45vh"
        width="100%"
        language="markdown"
        path={modelPath(sourceMsgId)}
        value={value}
        theme={monacoThemeName}
        beforeMount={beforeMount}
        onMount={handleMount}
        onChange={(nextValue) => onChange(nextValue ?? "")}
        loading={<div className="flex h-full items-center justify-center text-sm" style={{ color: "var(--muted)" }}>Loading Monaco editor…</div>}
        options={{
          automaticLayout: true,
          bracketPairColorization: { enabled: true },
          contextmenu: true,
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
          fontSize: 13,
          lineNumbers: "on",
          minimap: { enabled: false },
          padding: { top: 12, bottom: 12 },
          renderLineHighlight: "line",
          renderWhitespace: "selection",
          scrollBeyondLastLine: false,
          tabSize: 2,
          wordWrap: "on",
        }}
        wrapperProps={{
          "data-testid": "edit-answer-monaco",
        }}
      />
    </div>
  );
}
