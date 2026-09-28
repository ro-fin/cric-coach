"use client";

/** Today (home) route: `/?player=<id>` picks the player (default: first listed). */

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import TodayView from "./_today/TodayView";

function TodayRoute() {
  const params = useSearchParams();
  // Role arrives with the Phase 8 shell (useRole); until then Start session stays hidden.
  return <TodayView role={null} search={`?${params.toString()}`} />;
}

export default function HomePage() {
  return (
    <Suspense fallback={null}>
      <TodayRoute />
    </Suspense>
  );
}
