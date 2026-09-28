# Observability, Data Quality & Drift (US-L4)

Why this exists: "random videos → random advice" must never happen silently.
Every session gets a data-quality score before any report is trusted; the
auto pipeline is audited weekly against human ground truth; and every problem
is routed to the person who can act on it. The dict shapes below are pinned
by `docs/specs/phase-5-plan.md` (cross-group contract #6).

## Per-session data quality score

`cricai_coaching.quality.score_session` computes
`{components: {sync, exposure, pose_coverage, track_coverage,
calibration_freshness → 0..1}, composite, banner}` from injected plain row
data (`cricai_worker.drift_monitor.load_quality_inputs` is the default DB
loader). Reports consume the composite; below the honesty threshold the
report leads with the banner instead of burying it.

| Component | Source rows | Meaning | Constants |
|---|---|---|---|
| `sync` | `clips` (status `cut`) | Fraction of valid balls whose per-camera clip windows agree within tolerance. Balls seen by <2 cameras count as **unverified (0)** — a single-camera session earns no sync credit. | `SYNC_TOLERANCE_MS = 50` |
| `exposure` | `videos.probe["mean_luma_samples"]` | Fraction of sampled mean-luma values inside the usable band. No samples = no evidence = 0 (never an assumed-good default). | `EXPOSURE_LUMA_RANGE = (60, 200)` |
| `pose_coverage` | `pose_tracks.availability` | Mean over valid balls of the best camera's landmark availability; a ball with no pose contributes 0. | — |
| `track_coverage` | `ball_tracks.coverage` | Mean over valid balls of the best camera's tracked-flight fraction; a ball with no track contributes 0. | — |
| `calibration_freshness` | `sessions.calibration_id` age + `calibration_suspect` | 1.0 up to 30 days old, decaying linearly to 0.0 at 120 days. No attached calibration, or the US-C4 suspect flag, scores 0 outright. | `CALIBRATION_FRESH_DAYS = 30`, `CALIBRATION_STALE_DAYS = 120` |

**Composite** = weighted mean, weights in `quality.COMPONENT_WEIGHTS`
(sync 0.20, exposure 0.15, pose 0.25, track 0.25, calibration 0.15).

**Honesty banner**: composite `< HONESTY_BANNER_THRESHOLD (0.7)` produces a
banner string naming the weakest components; the report renderer places it
first among non-safety content — an active safety warning renders above it
(safety supremacy, US-H5), then the honesty banner / day-scoped coverage note,
then the corrections. The weekly monitor also persists it as a parent-audience
`data_quality_low` alert so a low-quality session is visible even if the
report is never opened.

Known limitation (V2): no producer records `mean_luma_samples` yet, so
`exposure` reads 0 everywhere. The weights are chosen so an otherwise-perfect
session still scores 0.85 (no false banner); once probes record luma samples
the component activates with no code change here.

## Auto-vs-manual drift protocol

10 balls per week stay manually tagged **forever** (test-strategy T2). The
protocol has three legs, all in `cricai_coaching.drift` (pure) wired by the
weekly `cricai_worker.drift_monitor.run_weekly_drift_monitor` job:

1. **Sampling** — `select_weekly_sample` picks ~`WEEKLY_SAMPLE_SIZE (10)`
   untagged valid balls from the sessions of the run's own week
   (`session_date` in `[week_start, week_start + 7d)` — the week just
   ended), round-robin across strata (session × auto length zone) so no
   single session or zone dominates. Selection is seeded by the week
   (`gt-sample:{week_start}`): reproducible regardless of DB row order. The
   picks are persisted as a developer `ground_truth_sample` alert listing
   `(session_id, ball_no, stratum)`.
2. **Agreement monitor** — each run compares the **prior week's** sessions
   (`session_date` in `[week_start - 7d, week_start)`): that is the week
   whose sample the previous run posted, and whose tags landed during the
   week just ended — so the sample → tag → compare loop closes one run
   later, on exactly those balls. Manual tags with
   `ground_truth_eligible = true` are compared field-by-field against
   auto-derived values (`field_agreement`; a field pair counts only when
   both sides have a value). Per-field agreement below its threshold fires
   a developer `drift_agreement` alert whose `detail` names the run
   (`week_start`) and the window it compared (`compared_week_start`).
   Thresholds (`DEFAULT_AGREEMENT_THRESHOLDS`, from the T3 model gates):
   line ≥ 0.90, length ≥ 0.85, shot ≥ 0.75, others ≥ 0.80. Fields with
   < `MIN_COMPARED_PER_FIELD (5)` pairs stay silent — thin evidence must
   not fire or silence the alarm; instead a `drift_sample_short` alert
   reports that the compared week's sample fell below protocol. Today's
   default auto source is `bounce_estimates` line/length (US-F4); richer
   auto fields plug in via the job's `auto_fields` loader.
3. **Canary** — a frozen synthetic session (`cricai_data.synthetic`, seed
   `CANARY_SEED = 424242`, 2×15 balls) whose expected per-ball fields are
   regenerated deterministically (`canary_expectations`). **Every** weekly
   run evaluates the canary, and since Phase 6 the run RE-DERIVES it by
   default (`cricai_worker.drift_monitor.derive_canary_observed`): the
   frozen canary session is materialized as a real session (deterministic
   ids, player "Drift Canary", `session_date` 2000-01-03 so it never enters
   a monitored week), its machine-derived rows are cleared through the
   audited re-derive cascade, the events call-adapter re-segments the frozen
   probe motion-energy envelope, the frozen per-ball trajectories are
   re-seeded, and the real US-F4 bounce estimator + zone classifier + the
   same `bounce_estimate_fields` loader the agreement leg uses produce the
   observations. Pooled agreement below `CANARY_AGREEMENT_BOUND (0.98)`
   fires a **critical** `drift_canary` alert; nothing comparable — a failed
   derivation (its error is recorded on the alert as
   `detail.derivation_error`) or a pipeline that produced no comparable
   fields — fires a **critical** `drift_canary_missing` alert, once per
   week (silence is a failure, never a pass). A deliberately mislabeled
   batch must trip this alarm — that is the MV acceptance test. Explicit
   `canary_observed` observations passed to the job bypass the derivation
   (test/override seam).

All monitor writes are idempotent: every alert carries a deterministic
`detail.dedupe_key` (week- or session-scoped), so re-running a week inserts
nothing new.

### Operator note: the canary is a visible, permanent row — leave it alone

Because the canary is materialized as REAL rows in the production DB, the
player **"Drift Canary"** and its `2000-01-03` batting session are a
permanent, expected sight in Parent/Coach listings — `GET /players`,
`GET /sessions`, and the web dashboard's session list all include them. This
is by design, not a data leak:

- **Kid-safe:** the canary player is guest-flagged (`is_guest = true`), so
  the US-L3 guest scoping hides it from the Player (kid) role on every
  surface, and guest-skipping jobs (the weekly/monthly report rollup sweep)
  never manufacture reports, milestones or workload for it.
- **Contained:** the canary session pins its own dedicated calibration,
  created for and attached to the canary alone — it is never reused by or
  attached to real sessions — and its `session_date` (2000-01-03) keeps it
  out of every monitored week, trend window and baseline.
- **Permanent:** do NOT delete or privacy-purge the canary player/session to
  "clean up" the listing — the next weekly run deterministically
  re-materializes the same rows (same ids), and deleting mid-week only costs
  a re-derivation. Acknowledge its existence in runbooks instead of removing
  it.

### Weekly human workflow (runbook)

Every week (suggested: Monday, for the week just ended):

1. **Run the monitor** (or let the scheduler): `run_weekly_drift_monitor(ctx,
   week_start=<last Monday>)`. It scores the ended week's session quality,
   posts the ended week's sample, compares the **previous** week's tagged
   sample against the auto output, and evaluates the canary.
2. **Pick up the sample** — open the developer alert feed
   (`GET /alerts?acknowledged=false` with the coach token) and find the
   `ground_truth_sample` alert. It lists ~10 `(session_id, ball_no)` pairs.
3. **Tag the sampled balls** in the tagging UI (US-B4). Tag what you see —
   do **not** look at the auto values first (that is the whole point of the
   sample). Manual tags are automatically `ground_truth_eligible`.
4. **Acknowledge** the sample alert (`POST /alerts/{id}/ack`) once all ten
   are tagged. The next weekly run — the one whose `week_start` is the
   Monday after this sample's week — compares the auto output against
   exactly these balls (its `drift_agreement`/`drift_sample_short` details
   carry that week as `compared_week_start`). The loop closes one run
   later, always.
5. **On a `drift_agreement` alert**: check the disagreeing field's balls in
   the review UI. Camera/calibration cause → fix hardware, re-derive the
   sessions. Model cause → re-check recent labels, consider retraining
   (US-F5 loop); the alert's `detail` names the field, the agreement %, the
   sample size, and the compared week. Do not raise thresholds to make
   alerts go away — thresholds trace to the T3 model gates.
6. **On a `drift_canary` / `drift_canary_missing` alert**: stop trusting new
   reports until resolved — the pipeline regressed on a fixed input
   (`drift_canary`) or produced nothing comparable for it
   (`drift_canary_missing`; the alert's `detail.derivation_error` names the
   failure when the weekly re-derivation itself broke). This is a
   release-blocking event; bisect the most recent model/config change. The
   re-derivation runs through the production entrypoints (re-derive cascade
   -> events call-adapter -> bounce estimator -> zone classifier), so a
   regression anywhere along that chain trips the alarm; a healthy pipeline
   fires nothing.
7. **On a `drift_sample_short` alert**: the human protocol broke — fewer
   than 10 tagged-and-comparable balls in the compared (previous) week.
   Backfill the missing tags, then re-run the monitor with the **same**
   `week_start` — re-runs are idempotent (dedupe keys), so the re-run only
   adds whatever the fresh comparison finds. The very first run ever has an
   empty compared week and reports this shortfall once; acknowledge it.

Quarterly: double-label one week's sample with a second person and track
inter-rater agreement (T2.3); disagreements update `docs/labeling_guide.md`.

## Alert routing

`alerts` rows carry an audience (US-L4 AC: device/health → Parent,
pipeline/model → Developer). Routing is authorization, not preference:

| Audience | Who sees it (role) | Codes today |
|---|---|---|
| `parent` | Parent | `data_quality_low` (honesty banner mirror) |
| `developer` | Coach (the household developer seat) | `drift_agreement`, `drift_sample_short`, `drift_canary`, `drift_canary_missing`, `ground_truth_sample` |

- `GET /alerts?audience=&acknowledged=` — filtered within the caller's
  visibility; asking for the other audience is a 403, and the Player role
  has no alert access.
- `POST /alerts/{id}/ack` — flips the per-row flag idempotently and writes
  an `audit_log` row (`action = "alert_ack"`) on the actual state change.

Severity vocabulary: `info` (workflow items, e.g. the weekly sample),
`warning` (agreement drift, low quality, short sample), `critical` (canary
failures — release blocking).
