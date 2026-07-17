import { describe, expect, it } from "vitest";
import fixtureData from "../../../../eggthreads/tests/fixtures/phase6-operational-recovery-interleaved.json";

interface FixtureRecord {
  event_seq: number;
  id: string;
  role: string;
  content: string;
  recovery_notice?: boolean;
  canonical_payload: Record<string, unknown>;
  expected_presentation: {
    label: string;
    min_content: string;
    medium_max_content: string;
  };
}

const fixture = fixtureData as { records: FixtureRecord[] };

describe("operational recovery fixture contract", () => {
  it("retains literal chronology and the shared presentation expectations", () => {
    expect(fixture.records.map((record) => record.event_seq)).toEqual([10, 20, 30, 40, 50, 60, 70, 80]);
    expect(fixture.records.map((record) => record.expected_presentation.label)).toEqual([
      "User",
      "System",
      "Continue Status",
      "System",
      "Continue Status",
      "Continue Status",
      "Continue Status",
      "Assistant",
    ]);
  });

  it("does not pretend current public fields identify recovery decisions", () => {
    const notices = fixture.records.filter((record) => record.recovery_notice);
    expect(notices.map((record) => record.canonical_payload.action ?? null)).toEqual([
      "scheduled",
      "applied",
      "stopped",
      null,
    ]);
    for (const notice of notices) {
      expect(notice.role).toBe("system");
      expect(notice.recovery_notice).toBe(true);
    }
  });
});
