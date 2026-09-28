"use client";

/** New-session route (US-A3/A5/B1): `/sessions/new?player=<id>` or `?session=<id>` to resume. */

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { useRole } from "@/lib/auth/role";
import NewSessionFlow from "./NewSessionFlow";

function NewSessionRoute() {
  const params = useSearchParams();
  const role = useRole();
  return <NewSessionFlow role={role} search={`?${params.toString()}`} />;
}

export default function NewSessionPage() {
  return (
    <Suspense fallback={null}>
      <NewSessionRoute />
    </Suspense>
  );
}
