/** US-K2: /pitchmap/{sessionId} resolves the route param into the view. */

import { describe, expect, it } from "vitest";
import PitchMapView from "../PitchMapView";
import PitchMapPage from "./page";

describe("PitchMapPage", () => {
  it("awaits the route params and mounts the pitch-map view for that session", async () => {
    const element = await PitchMapPage({ params: Promise.resolve({ sessionId: "s-9" }) });
    expect(element.type).toBe(PitchMapView);
    expect(element.props.sessionId).toBe("s-9");
  });
});
