/**
 * US-J5 review page: /review — the coach's queue of gate-held draft reports.
 *
 * A static shell around the client-side queue: the ReviewQueue component
 * fetches with the device's bearer token, so the server's coach-only gate
 * decides what this page may show (client display mirrors that decision;
 * it never grants anything, US-L3).
 */

import ReviewQueue from "./ReviewQueue";

export default function ReviewPage() {
  return (
    <main>
      <h1>Report review queue</h1>
      <p>
        Draft reports held for coach review (US-J5). Publishing re-runs the safety, claims and
        evidence gates server-side; a blocked report shows its reasons here.
      </p>
      <ReviewQueue />
    </main>
  );
}
