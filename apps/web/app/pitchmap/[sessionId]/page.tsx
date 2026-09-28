/** Route: /pitchmap/{sessionId} — the US-K2 pitch map & zone analytics view. */

import PitchMapView from "../PitchMapView";

export default async function PitchMapPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = await params;
  return <PitchMapView sessionId={sessionId} />;
}
