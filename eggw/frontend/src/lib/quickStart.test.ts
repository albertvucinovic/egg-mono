import { beforeEach, describe, expect, it } from "vitest";
import { applyQuickStartThread } from "./quickStart";
import { useAppStore } from "./store";

const attachment = {
  type: "attachment" as const,
  input_id: "input-launch",
  owner_thread_id: "thread-launch",
  presentation: "file" as const,
  mime_type: "text/plain",
  filename: "launch file.txt",
  size_bytes: 12,
  sha256: "a".repeat(64),
  provenance: { kind: "local_path" },
  options: {},
};

describe("quick-start composer ownership", () => {
  beforeEach(() => {
    useAppStore.setState({
      composerDraftByThread: {},
      stagedAttachmentsByThread: {},
      systemLogs: [],
    });
  });

  it("loads launch text into the existing thread-scoped unsent draft", () => {
    applyQuickStartThread(
      { id: "thread-launch", initial_draft: "Tell me a story" },
      useAppStore.getState(),
    );

    expect(useAppStore.getState().composerDraftByThread["thread-launch"]).toBe("Tell me a story");
    expect(useAppStore.getState().stagedAttachmentsByThread["thread-launch"]).toBeUndefined();
  });

  it("loads a launch file into existing attachment staging and surfaces errors", () => {
    applyQuickStartThread(
      {
        id: "thread-launch",
        initial_attachment: attachment,
        initial_error: "Could not stage another file",
      },
      useAppStore.getState(),
    );

    expect(useAppStore.getState().composerDraftByThread["thread-launch"]).toBeUndefined();
    expect(useAppStore.getState().stagedAttachmentsByThread["thread-launch"]).toEqual([attachment]);
    expect(useAppStore.getState().systemLogs.at(-1)).toMatchObject({
      message: "Could not stage another file",
      type: "error",
    });
  });
});
