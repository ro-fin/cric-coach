/** Session detail route (US-K1, US-B5): timeline + multi-cam player. */

import SessionDetailClient from "./SessionDetailClient";

export default async function SessionDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <SessionDetailClient sessionId={id} />;
}
