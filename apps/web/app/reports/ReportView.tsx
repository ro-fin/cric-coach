/**
 * US-K5 report surface: renders the pinned Report-body-v1 in the exact
 * section order of the server's printable HTML (`render_report_html`) so the
 * two surfaces never diverge: safety first, honesty banner, coverage note,
 * main correction, drill, goal, fatigue check, secondary, what went well —
 * then the claims ledger, the US-I7 bowling section when the body carries
 * one (scorecard, scatter summary + per-ball release points (T5 #4: the
 * release scatter renders), agreement, legend modules, workload vs
 * ceiling — the workload view always ships) and (weekly/monthly)
 * trends/milestones. An unqualified trend never prints a direction verdict
 * (US-G5: trend claims need >= 3 sessions and >= 30 balls per point).
 *
 * Every string is rendered as a React text child, so untrusted or malformed
 * body content is escaped inert. All numbers come verbatim from the body —
 * nothing is computed client-side (US-K4 data parity).
 */

import { BattingSplit, BowlingSection, CorrectionItem, EvidenceMap, ReportBody } from "./api";

/** Null percentages/heights are unknowable, shown honestly — never as 0. */
function pctText(value: number | null): string {
  return value === null ? "—" : `${value}%`;
}

function cmText(value: number | null): string {
  return value === null ? "—" : `${value} cm`;
}

/**
 * US-K5 AC "evidence clips playable inline from the report": each evidence
 * entry deep-links to the session player at that ball (`/sessions/{id}?ball=N`
 * — the US-K2 URL contract; the player opens on the ball and plays its clips).
 * Weekly/monthly bodies carry no session id, so the link is omitted and the
 * clip key renders as inert text (nothing to open).
 */
function Evidence({ evidence, sessionId }: { evidence: EvidenceMap; sessionId: string | null }) {
  const balls = Object.keys(evidence).sort();
  if (balls.length === 0) {
    return null;
  }
  return (
    <ul className="evidence">
      {balls.flatMap((ball) =>
        Object.keys(evidence[ball])
          .sort()
          .map((camera) => (
            <li key={`${ball}-${camera}`}>
              {sessionId !== null ? (
                <a href={`/sessions/${sessionId}?ball=${ball}`}>
                  ball {ball} - {camera}
                </a>
              ) : (
                <>
                  ball {ball} - {camera}
                </>
              )}
              : <code>{evidence[ball][camera]}</code>
            </li>
          )),
      )}
    </ul>
  );
}

function Correction({
  title,
  item,
  sessionId,
}: {
  title: string;
  item: CorrectionItem;
  sessionId: string | null;
}) {
  return (
    <section>
      <h2>{title}</h2>
      <p>{item.text}</p>
      <Evidence evidence={item.evidence} sessionId={sessionId} />
    </section>
  );
}

/** US-H2 plan-vs-actual: per-block planned/actual/deviation, > threshold
 * flagged, plus the kid-first fun-block predicate. Numbers verbatim (US-K4). */
function BattingSplitTable({ split }: { split: BattingSplit }) {
  return (
    <section data-testid="batting-split">
      <h2>Plan vs actual</h2>
      <table>
        <thead>
          <tr>
            <th>Block</th>
            <th>Planned</th>
            <th>Actual</th>
            <th>Deviation %</th>
            <th>Flagged</th>
          </tr>
        </thead>
        <tbody>
          {split.intents.map((row) => (
            <tr key={row.intent} data-flagged={row.flagged}>
              <td>{row.intent}</td>
              <td>{row.planned_balls}</td>
              <td>{row.actual_balls}</td>
              <td>{row.deviation_pct}%</td>
              <td>{row.flagged ? "flagged" : "on plan"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>
        Planned {split.planned_total} balls · {split.actual_total} logged · fun block{" "}
        {split.fun_block_intact ? "intact" : "missing"}.
      </p>
      <p>{split.note}</p>
    </section>
  );
}

/** US-I7 leg-spin section: every number verbatim from the body (US-K4). */
function Bowling({ bowling }: { bowling: BowlingSection }) {
  const scorecard = bowling.accuracy_scorecard;
  const scatter = bowling.release_scatter;
  const agreement = bowling.variation_agreement;
  const workload = bowling.workload;
  return (
    <section data-testid="bowling">
      <h2>Leg-spin session</h2>
      <section data-testid="bowling-accuracy">
        <h3>Accuracy scorecard</h3>
        <table>
          <thead>
            <tr>
              <th>Variation</th>
              <th>Hits</th>
              <th>Hit %</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>overall</td>
              <td>
                {scorecard.overall.hits}/{scorecard.overall.n}
              </td>
              <td>{pctText(scorecard.overall.pct)}</td>
            </tr>
            {scorecard.by_variation.map((cell) => (
              <tr key={cell.variation}>
                <td>{cell.variation}</td>
                <td>
                  {cell.hits}/{cell.n}
                </td>
                <td>{pctText(cell.pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p>
          {scorecard.counted} of {scorecard.total} deliveries had a confident target call.
        </p>
        <p>{scorecard.note}</p>
      </section>
      <section data-testid="bowling-scatter">
        <h3>Release scatter</h3>
        <p>
          {scatter.n} measured · mean {cmText(scatter.mean_cm)} · sigma {cmText(scatter.sigma_cm)}{" "}
          · range {cmText(scatter.min_cm)} to {cmText(scatter.max_cm)}
        </p>
        {scatter.points.length > 0 && (
          <ul data-testid="bowling-scatter-points">
            {scatter.points.map((point) => (
              <li key={point.ball_id}>
                ball {point.ball_id}: {cmText(point.release_height_cm)} ({point.variation_intent})
              </li>
            ))}
          </ul>
        )}
        {scatter.by_variation.length > 0 && (
          <ul>
            {scatter.by_variation.map((cell) => (
              <li key={cell.variation}>
                {cell.variation}: {cell.n} measured · mean {cmText(cell.mean_cm)} · sigma{" "}
                {cmText(cell.sigma_cm)}
              </li>
            ))}
          </ul>
        )}
        <p>{scatter.note}</p>
      </section>
      <section data-testid="bowling-agreement">
        <h3>Variation agreement</h3>
        <p>
          {agreement.agreement_pct === null
            ? "Not enough compared deliveries for an agreement number"
            : `Agreement ${agreement.agreement_pct}% over ${agreement.compared} compared`}
          {" · "}
          {agreement.unclear} unclear of {agreement.labeled} declared.
        </p>
        <table>
          <thead>
            <tr>
              <th>Intent</th>
              <th>Detected</th>
              <th>Count</th>
            </tr>
          </thead>
          <tbody>
            {agreement.matrix.map((cell) => (
              <tr key={`${cell.intent}-${cell.detected}`}>
                <td>{cell.intent}</td>
                <td>{cell.detected}</td>
                <td>{cell.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p>{agreement.note}</p>
      </section>
      {bowling.learning_modules.length > 0 && (
        <section data-testid="bowling-modules">
          <h3>Legend lessons</h3>
          {bowling.learning_modules.map((module) => (
            <article key={module.kind}>
              <h4>
                {module.title} — {module.legend}
              </h4>
              <p>{module.principle}</p>
              <p>{module.lesson}</p>
              <p>{module.drill}</p>
              {module.example_ball_ids.length > 0 && (
                <p>Your example balls: {module.example_ball_ids.join(", ")}</p>
              )}
            </article>
          ))}
        </section>
      )}
      <section data-testid="bowling-workload">
        <h3>Workload vs ceiling</h3>
        {workload === null ? (
          <p>Workload data unavailable for this session.</p>
        ) : (
          <>
            <p>
              {workload.weighted_overs} overs bowled {workload.window.start} to{" "}
              {workload.window.end}
              {workload.ceiling_overs === null
                ? " · no overs ceiling applies for this age band"
                : ` of ${workload.ceiling_overs} allowed`}
              {workload.remaining_balls !== null && ` · ${workload.remaining_balls} balls left`}
            </p>
            {workload.violations.length > 0 && (
              <ul data-testid="workload-violations">
                {workload.violations.map((violation) => (
                  <li key={violation}>{violation}</li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>
    </section>
  );
}

export default function ReportView({
  body,
  sessionId = null,
}: {
  body: ReportBody;
  sessionId?: string | null;
}) {
  return (
    <article className="report-view" data-kind={body.kind}>
      <header>
        <h1>{body.kind} report</h1>
        <p>
          {body.period.start} to {body.period.end}
        </p>
      </header>
      {body.safety !== null && body.safety.active && (
        <section data-testid="safety">
          <h2>Safety first</h2>
          <p>{body.safety.text}</p>
        </section>
      )}
      {body.honesty_banner !== null && (
        <p className="honesty-banner" data-testid="honesty-banner">
          {body.honesty_banner}
        </p>
      )}
      {body.coverage_note !== null && (
        <p className="coverage-note" data-testid="coverage-note">
          {body.coverage_note}
        </p>
      )}
      {body.main_correction !== null && (
        <Correction title="Main correction" item={body.main_correction} sessionId={sessionId} />
      )}
      {body.drill !== null && (
        <section data-testid="drill">
          <h2>Tomorrow&apos;s drill</h2>
          <p>{body.drill.text}</p>
        </section>
      )}
      {body.goal !== null && (
        <section data-testid="goal">
          <h2>Goal</h2>
          <p>
            {body.goal.metric}: reach {body.goal.target ?? "coach-set"} next session.
          </p>
        </section>
      )}
      {body.fatigue_note !== null && (
        <section data-testid="fatigue">
          <h2>Fatigue check</h2>
          <p>{body.fatigue_note.text}</p>
          <p>
            Last {body.fatigue_note.window} balls · control drop{" "}
            {body.fatigue_note.control_drop_points} points
            {body.fatigue_note.degrading_metrics.length > 0 &&
              ` · watching: ${body.fatigue_note.degrading_metrics.join(", ")}`}
          </p>
        </section>
      )}
      {body.batting_split !== undefined && <BattingSplitTable split={body.batting_split} />}
      {body.secondary.length > 0 && (
        <details className="secondary">
          <summary>Also worth a look</summary>
          <ul>
            {body.secondary.map((item) => (
              <li key={item.finding_id}>
                {item.text}
                <Evidence evidence={item.evidence} sessionId={sessionId} />
              </li>
            ))}
          </ul>
        </details>
      )}
      <section data-testid="positive">
        <h2>What went well</h2>
        <p>{body.positive}</p>
      </section>
      {body.claims.length > 0 && (
        <section data-testid="claims">
          <h2>Numbers in this report</h2>
          <table>
            <thead>
              <tr>
                <th>Metric</th>
                <th>Value</th>
                <th>Recompute key</th>
              </tr>
            </thead>
            <tbody>
              {body.claims.map((claim) => (
                <tr key={claim.recompute_key}>
                  <td>{claim.metric}</td>
                  <td>{claim.value}</td>
                  <td>
                    <code>{claim.recompute_key}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      {body.bowling !== undefined && <Bowling bowling={body.bowling} />}
      {body.trends !== undefined && body.trends.length > 0 && (
        <section data-testid="trends">
          <h2>Trends</h2>
          <ul>
            {body.trends.map((trend) => (
              <li key={`${trend.metric}-${trend.zone_key ?? "all"}`}>
                {trend.metric}
                {trend.zone_key !== null && ` (${trend.zone_key})`}:{" "}
                {trend.qualified
                  ? `${trend.direction} over ${trend.points.length} points`
                  : "not enough data yet for a trend claim"}
              </li>
            ))}
          </ul>
        </section>
      )}
      {body.milestones !== undefined && body.milestones.length > 0 && (
        <section data-testid="milestones">
          <h2>Milestones</h2>
          <ul>
            {body.milestones.map((milestone) => (
              <li key={`${milestone.kind}-${milestone.metric}-${milestone.achieved_on}`}>
                {milestone.kind}: {milestone.metric} {milestone.value} on {milestone.achieved_on}
              </li>
            ))}
          </ul>
        </section>
      )}
    </article>
  );
}
