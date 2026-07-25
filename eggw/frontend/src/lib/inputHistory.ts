export const INPUT_HISTORY_LIMIT = 200;
export const INPUT_SUBMITTED_EVENT_TYPE = "input.submitted";

export function inputHistoryQueryKey(threadId: string) {
  return ["inputHistory", threadId] as const;
}

export type InputHistoryDirection = "older" | "newer";

export interface InputHistoryState {
  entries: readonly string[];
  position: number | null;
  draft: string;
}

export interface TextareaSelection {
  value: string;
  selectionStart: number;
  selectionEnd: number;
}

export function initialInputHistoryState(entries: readonly string[] = []): InputHistoryState {
  return {
    entries: entries.filter((entry): entry is string => typeof entry === "string" && Boolean(entry)),
    position: null,
    draft: "",
  };
}

export function resetInputHistory(state: InputHistoryState): InputHistoryState {
  return { ...state, position: null, draft: "" };
}

export function replaceInputHistoryEntries(
  state: InputHistoryState,
  entries: readonly string[],
): InputHistoryState {
  return initialInputHistoryState(entries);
}

export function navigateInputHistory(
  state: InputHistoryState,
  direction: InputHistoryDirection,
  currentDraft: string,
): { state: InputHistoryState; value: string } | null {
  if (direction === "older") {
    if (!state.entries.length) return null;
    if (state.position === null) {
      const position = state.entries.length - 1;
      return {
        state: { ...state, position, draft: currentDraft },
        value: state.entries[position],
      };
    }
    const position = Math.max(0, state.position - 1);
    return { state: { ...state, position }, value: state.entries[position] };
  }

  if (state.position === null) return null;
  if (state.position < state.entries.length - 1) {
    const position = state.position + 1;
    return { state: { ...state, position }, value: state.entries[position] };
  }
  return { state: resetInputHistory(state), value: state.draft };
}

export function isTextareaHistoryBoundary(
  textarea: TextareaSelection,
  direction: InputHistoryDirection,
): boolean {
  if (textarea.selectionStart !== textarea.selectionEnd) return false;
  const cursor = textarea.selectionStart;
  if (direction === "older") return !textarea.value.slice(0, cursor).includes("\n");
  return !textarea.value.slice(cursor).includes("\n");
}
