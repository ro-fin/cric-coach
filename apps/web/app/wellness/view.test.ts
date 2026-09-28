import { describe, expect, it } from "vitest";
import { checkin } from "@/test/fixtures.ops";
import {
  bodyLabel,
  canClear,
  checkinProblems,
  emptyDraft,
  openPainCheckins,
  playerIdFromSearch,
  toCheckinIn,
  WELLNESS_ROLES,
} from "./view";

describe("wellness access", () => {
  it("serves every role but lets only adults clear pain", () => {
    expect(WELLNESS_ROLES).toEqual(["parent", "coach", "player"]);
    expect(canClear("parent")).toBe(true);
    expect(canClear("coach")).toBe(true);
    expect(canClear("player")).toBe(false);
    expect(canClear(null)).toBe(false);
  });

  it("reads the player from the query string", () => {
    expect(playerIdFromSearch("?player=p1")).toBe("p1");
    expect(playerIdFromSearch("?player=")).toBeNull();
    expect(playerIdFromSearch("")).toBeNull();
  });
});

describe("checkinProblems", () => {
  it("accepts an empty draft with a date", () => {
    expect(checkinProblems(emptyDraft("2026-09-28"))).toEqual([]);
  });

  it("accepts values on the server bounds", () => {
    const draft = {
      ...emptyDraft("2026-09-28"),
      energy: "5",
      sleepHours: "14",
      soreness: { neck: 3, groin: 0 },
    };
    expect(checkinProblems(draft)).toEqual([]);
  });

  it("names every out-of-range value in the server wording", () => {
    const draft = {
      ...emptyDraft(""),
      energy: "6",
      sleepHours: "15",
      soreness: { neck: 4 },
    };
    expect(checkinProblems(draft)).toEqual([
      "checkin_date is required",
      "soreness['neck'] must be 0..3, got 4",
      "energy must be 1..5, got 6",
      "sleep_hours must be 0.0..14.0, got 15",
    ]);
  });

  it("rejects fractional energy and non-numeric input", () => {
    expect(checkinProblems({ ...emptyDraft("d"), energy: "2.5" })).toHaveLength(1);
    expect(checkinProblems({ ...emptyDraft("d"), sleepHours: "abc" })).toHaveLength(1);
    expect(checkinProblems({ ...emptyDraft("d"), soreness: { neck: 1.5 } })).toHaveLength(1);
  });
});

describe("toCheckinIn", () => {
  it("builds the wire body from a blank draft", () => {
    expect(toCheckinIn(emptyDraft("2026-09-28"))).toEqual({
      checkin_date: "2026-09-28",
      soreness: {},
      energy: null,
      sleep_hours: null,
      pain: false,
      pain_note: null,
    });
  });

  it("drops zero soreness, parses numbers and keeps the note only with pain", () => {
    const draft = {
      checkinDate: "2026-09-28",
      energy: "3",
      sleepHours: "7.5",
      soreness: { neck: 0, elbow_right: 2 },
      pain: true,
      painNote: "  elbow after bowling  ",
    };
    expect(toCheckinIn(draft)).toEqual({
      checkin_date: "2026-09-28",
      soreness: { elbow_right: 2 },
      energy: 3,
      sleep_hours: 7.5,
      pain: true,
      pain_note: "elbow after bowling",
    });
    expect(toCheckinIn({ ...draft, pain: false }).pain_note).toBeNull();
    expect(toCheckinIn({ ...draft, painNote: "  " }).pain_note).toBeNull();
  });
});

describe("helpers", () => {
  it("picks the check-ins behind open pain flags in served order", () => {
    const a = checkin({ id: "a" });
    const b = checkin({ id: "b", pain: true });
    const c = checkin({ id: "c", pain: true });
    expect(openPainCheckins([a, b, c], ["c", "b"])).toEqual([b, c]);
  });

  it("labels body keys for people", () => {
    expect(bodyLabel("shoulder_right")).toBe("shoulder right");
  });
});
