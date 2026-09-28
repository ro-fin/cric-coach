"use client";

/** Today (home) route: `/?player=<id>` picks the player (default: first listed). */

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { useRole } from "@/lib/auth/role";
import TodayView from "./_today/TodayView";

function TodayRoute() {
  const params = useSearchParams();
  const role = useRole();
  return <TodayView role={role} search={`?${params.toString()}`} />;
}

export default function HomePage() {
  return (
    <Suspense fallback={null}>
      <TodayRoute />
    </Suspense>
  );
}
