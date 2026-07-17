import type { QueryClient, QueryKey } from "@tanstack/react-query";

interface ThreadModelRecord {
  id?: string;
  model_key?: string | null;
  [key: string]: unknown;
}

interface CanonicalModelSwitch {
  eventSeq: number;
  modelKey: string;
}

const latestModelEventByClient = new WeakMap<QueryClient, Map<string, number>>();

function latestEvents(queryClient: QueryClient): Map<string, number> {
  let events = latestModelEventByClient.get(queryClient);
  if (!events) {
    events = new Map<string, number>();
    latestModelEventByClient.set(queryClient, events);
  }
  return events;
}

function patchRecord<T extends ThreadModelRecord | undefined>(
  record: T,
  threadId: string,
  modelKey: string,
): T {
  if (!record || record.id !== threadId) return record;
  return { ...record, model_key: modelKey } as T;
}

function patchRecords<T extends ThreadModelRecord[] | undefined>(
  records: T,
  threadId: string,
  modelKey: string,
): T {
  if (!records) return records;
  return records.map((record) => patchRecord(record, threadId, modelKey)) as T;
}

async function cancelModelQueries(queryClient: QueryClient, threadId: string): Promise<void> {
  const exactKeys: QueryKey[] = [
    ["threadSettings", threadId],
    ["thread", threadId],
    ["rootThreads"],
    ["threads"],
  ];
  await Promise.all([
    ...exactKeys.map((queryKey) => queryClient.cancelQueries({ queryKey, exact: true })),
    queryClient.cancelQueries({ queryKey: ["threadChildren"] }),
  ]);
}

/**
 * Apply one ordered canonical model.switch event to model-bearing query caches.
 *
 * The event feed remains the authority. The local sequence watermark only fences
 * asynchronous cancellation work so an older handler cannot finish after a newer
 * canonical event. A targeted settings refetch fills non-model settings from the
 * database without polling; a later switch cancels that request before patching.
 */
export async function applyCanonicalModelSwitch(
  queryClient: QueryClient,
  threadId: string,
  event: CanonicalModelSwitch,
): Promise<boolean> {
  const modelKey = String(event.modelKey || "").trim();
  if (!threadId || !modelKey || !Number.isSafeInteger(event.eventSeq) || event.eventSeq < 0) {
    return false;
  }

  const events = latestEvents(queryClient);
  if (event.eventSeq <= (events.get(threadId) ?? -1)) return false;
  events.set(threadId, event.eventSeq);

  await cancelModelQueries(queryClient, threadId);
  if (events.get(threadId) !== event.eventSeq) return false;

  queryClient.setQueryData<Record<string, unknown> | undefined>(
    ["threadSettings", threadId],
    (previous) => ({ ...(previous || {}), model_key: modelKey }),
  );
  queryClient.setQueryData<ThreadModelRecord | undefined>(
    ["thread", threadId],
    (previous) => patchRecord(previous, threadId, modelKey),
  );
  queryClient.setQueryData<ThreadModelRecord[] | undefined>(
    ["rootThreads"],
    (previous) => patchRecords(previous, threadId, modelKey),
  );
  queryClient.setQueryData<ThreadModelRecord[] | undefined>(
    ["threads"],
    (previous) => patchRecords(previous, threadId, modelKey),
  );
  queryClient.setQueriesData<ThreadModelRecord[] | undefined>(
    { queryKey: ["threadChildren"] },
    (previous) => patchRecords(previous, threadId, modelKey),
  );

  // The event already supplies the authoritative model. Refetch only the active
  // settings query so unrelated settings remain complete; no polling is added.
  void queryClient.invalidateQueries({
    queryKey: ["threadSettings", threadId],
    exact: true,
    refetchType: "active",
  });
  return true;
}

/** Reconcile the bounded settings snapshot whenever SSE attaches or reconnects. */
export async function reconcileThreadModelSnapshot(
  queryClient: QueryClient,
  threadId: string,
): Promise<void> {
  if (!threadId) return;
  await queryClient.cancelQueries({ queryKey: ["threadSettings", threadId], exact: true });
  await queryClient.invalidateQueries({
    queryKey: ["threadSettings", threadId],
    exact: true,
    refetchType: "active",
  });
}

/** Refresh model-bearing snapshots after a successful local canonical write. */
export async function refreshThreadModelQueries(
  queryClient: QueryClient,
  threadId: string,
): Promise<void> {
  if (!threadId) return;
  await cancelModelQueries(queryClient, threadId);
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["threadSettings", threadId], exact: true, refetchType: "active" }),
    queryClient.invalidateQueries({ queryKey: ["thread", threadId], exact: true, refetchType: "active" }),
    queryClient.invalidateQueries({ queryKey: ["rootThreads"], exact: true, refetchType: "active" }),
    queryClient.invalidateQueries({ queryKey: ["threads"], exact: true, refetchType: "active" }),
    queryClient.invalidateQueries({ queryKey: ["threadChildren"], refetchType: "active" }),
  ]);
}
