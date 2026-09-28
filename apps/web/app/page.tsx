import { formatBallCount } from "@/lib/format";

export default function HomePage() {
  return (
    <main>
      <h1>cricAI</h1>
      <p>Home cricket lab — sessions, per-ball analysis, daily coaching report.</p>
      <p data-testid="demo-count">{formatBallCount(500)}</p>
    </main>
  );
}
