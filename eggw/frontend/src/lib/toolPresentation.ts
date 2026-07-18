import type { Message } from "./store";

export type HiddenDetailKind = "reasoning" | "tool_calls" | "tool_results";
export type HiddenDetailSource = "reasoning" | "tool_call" | "tool_result" | "tool_stream" | "tool_call_stream";

export interface HiddenToolDetail {
  kind: HiddenDetailKind;
  header: string;
  name?: string;
  tool_call_id?: string;
  tokens?: number;
  body?: string;
  source?: HiddenDetailSource;
}

export interface HiddenActivitySummary {
  details: HiddenToolDetail[];
  toolEntries: HiddenToolDetail[];
}

function cleanedText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function toolCallId(value: any): string {
  return cleanedText(value?.id || value?.tool_call_id);
}

export function toolCallName(value: any): string {
  return cleanedText(value?.name || value?.function?.name);
}

export function shortToolCallId(value: unknown): string {
  const id = cleanedText(value);
  return id.length > 12 ? id.slice(-12) : id;
}

export function toolDisplayName(name: unknown, id: unknown, fallback: "Tool call" | "Tool result"): string {
  const explicit = cleanedText(name);
  if (explicit) return explicit;
  const suffix = shortToolCallId(id);
  return suffix ? `${fallback} · ${suffix}` : fallback;
}

export const GET_USER_MESSAGE_TOOL_NAME = "get_user_message_while_preserving_llm_turn";

export function isGetUserMessageTool(name: unknown): boolean {
  return cleanedText(name) === GET_USER_MESSAGE_TOOL_NAME;
}

export function getUserAnswerToolCallId(message: Message): string {
  if (message.role !== "user") return "";
  if (message.consumed_by_tool_name !== GET_USER_MESSAGE_TOOL_NAME) return "";
  return cleanedText(message.consumed_by_tool_call_id);
}

export function getUserToolCallIds(message: Message): string[] {
  if (message.role !== "assistant" || !Array.isArray(message.tool_calls)) return [];
  return message.tool_calls
    .filter((call: any) => toolCallName(call) === GET_USER_MESSAGE_TOOL_NAME)
    .map((call: any) => toolCallId(call))
    .filter(Boolean);
}

/**
 * Fill missing result names only from an exact call identity in the same
 * loaded transcript. Conflicting reused IDs are deliberately left unresolved.
 */
export function resolveToolResultNames(messages: Message[]): Message[] {
  const declarations = new Map<string, Array<{ index: number; name: string }>>();
  messages.forEach((message, messageIndex) => {
    (message.tool_calls || []).forEach((call: any) => {
      const id = toolCallId(call);
      const name = toolCallName(call);
      if (!id || !name) return;
      const entries = declarations.get(id) || [];
      entries.push({ index: messageIndex, name });
      declarations.set(id, entries);
    });
  });

  let changed = false;
  const resolved = messages.map((message, messageIndex) => {
    if (message.role !== "tool" || cleanedText(message.name) || !message.tool_call_id) return message;
    const declarationsForId = declarations.get(message.tool_call_id) || [];
    if (declarationsForId.some((entry) => entry.index >= messageIndex)) return message;
    const names = new Set(declarationsForId.map((entry) => entry.name));
    if (names.size !== 1) return message;
    const name = declarationsForId.at(-1)?.name;
    if (!name) return message;
    changed = true;
    return { ...message, name };
  });
  return changed ? resolved : messages;
}

/**
 * Collapse duplicate streamed/durable representations for min-summary counts
 * while retaining every raw detail for exact popup inspection. Tool names
 * follow terminal Egg: call names when a run has calls, otherwise result names.
 */
export function summarizeHiddenActivity(details: HiddenToolDetail[]): HiddenActivitySummary {
  const structuredCallIds = new Set(
    details
      .filter((detail) => detail.source === "tool_call")
      .map((detail) => detail.tool_call_id)
      .filter(Boolean),
  );
  const counted = details.filter((detail) => {
    return !(
      detail.source === "tool_call_stream"
      && detail.tool_call_id
      && structuredCallIds.has(detail.tool_call_id)
    );
  });
  const calls = counted.filter((detail) => detail.kind === "tool_calls" && Boolean(cleanedText(detail.name)));
  const results = counted.filter((detail) => detail.kind === "tool_results" && Boolean(cleanedText(detail.name)));
  return {
    details: counted,
    toolEntries: calls.length ? calls : results,
  };
}

/**
 * Build one min-verbosity entry per call and attach results only by exact
 * tool_call_id. Name/position inference can cross-wire concurrent calls, so
 * ID-less legacy previews/results remain separately inspectable.
 */
export function correlateHiddenToolDetails(details: HiddenToolDetail[]): HiddenToolDetail[] {
  const structuredCalls = details.filter((detail) => detail.source === "tool_call");
  const structuredIds = new Set(structuredCalls.map((detail) => detail.tool_call_id).filter(Boolean));
  const calls = [
    ...structuredCalls,
    ...details.filter((detail) => (
      detail.source === "tool_call_stream"
      && (!detail.tool_call_id || !structuredIds.has(detail.tool_call_id))
    )),
  ];
  const finalResults = details.filter((detail) => detail.source === "tool_result");
  const streamResults = details.filter((detail) => detail.source === "tool_stream");
  const inspectableResult = (result: HiddenToolDetail): HiddenToolDetail => {
    const name = result.name || toolDisplayName("", result.tool_call_id, "Tool result");
    const header = [
      `Tool result: ${name}`,
      result.tool_call_id ? `tool_call_id: ${result.tool_call_id}` : "",
    ].filter(Boolean).join("\n");
    return {
      ...result,
      name,
      body: [header, "", "Result:", result.body || "(empty)"].join("\n"),
    };
  };
  if (calls.length === 0) {
    return [
      ...finalResults.map(inspectableResult),
      ...streamResults,
    ].filter((detail) => Boolean(detail.name));
  }

  const usedFinalResults = new Set<number>();
  const resultByCall = new Map<number, HiddenToolDetail>();
  const callIndexesById = new Map<string, number[]>();
  const resultIndexesById = new Map<string, number[]>();
  calls.forEach((call, callIndex) => {
    if (!call.tool_call_id) return;
    const indexes = callIndexesById.get(call.tool_call_id) || [];
    indexes.push(callIndex);
    callIndexesById.set(call.tool_call_id, indexes);
  });
  finalResults.forEach((result, resultIndex) => {
    if (!result.tool_call_id) return;
    const indexes = resultIndexesById.get(result.tool_call_id) || [];
    indexes.push(resultIndex);
    resultIndexesById.set(result.tool_call_id, indexes);
  });
  callIndexesById.forEach((callIndexes, id) => {
    const resultIndexes = resultIndexesById.get(id) || [];
    // Reused IDs are not identities. Never FIFO-pair their calls/results,
    // because ordering cannot prove which result belongs to which call.
    if (callIndexes.length !== 1 || resultIndexes.length !== 1) return;
    const callIndex = callIndexes[0];
    const resultIndex = resultIndexes[0];
    usedFinalResults.add(resultIndex);
    resultByCall.set(callIndex, finalResults[resultIndex]);
  });

  const pairedCalls = calls.map((call, callIndex) => {
    const result = resultByCall.get(callIndex);
    const callHeader = [
      `Tool call: ${call.name || toolDisplayName("", call.tool_call_id, "Tool call")}`,
      call.tool_call_id ? `tool_call_id: ${call.tool_call_id}` : "",
    ].filter(Boolean).join("\n");
    const bodyParts = [callHeader, "", "Arguments:", call.body || "(none)"];
    bodyParts.push(
      "",
      "Result:",
      result?.body || (result ? "(empty)" : "(not present in this compact run)"),
    );
    return { ...call, body: bodyParts.join("\n") };
  });

  const unmatchedResults = [
    ...finalResults.filter((_, index) => !usedFinalResults.has(index)).map(inspectableResult),
    ...streamResults,
  ].filter((detail) => Boolean(detail.name));
  return [...pairedCalls, ...unmatchedResults];
}
