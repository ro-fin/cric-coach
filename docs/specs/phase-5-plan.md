# Phase 5 Plan — Coaching Reports, Workload Safety, Agents, DAG Infra (Epics G + H + J, L1/L4)

**Stories:** US-G1–G6, US-H1–H5, US-J1–J4, US-L1 (DAG runner), US-L4 (observability), plus the
Phase-3-deferred re-derive cascade · **Branch:** `phase-5-coaching`
**Packages:** `cricai_coaching` (rules, reports, LLM writer, workload, wellness, safety agent,
analysis/planner/progress agents, quality), `cricai_data` (ball record + Phase-5 schema),
`cricai_worker` (pipeline DAG, report/backfill/baseline/drift jobs), API routers `rules.py`,
`reports.py`, `workload.py`, `wellness.py`, `pipeline.py`, `drills.py`, `alerts.py`.

Scope decisions (recorded as autonomous decisions; owner may override):

> **Post-review amendments (2026-07-11).** The adversarial review overrode two decisions
> below. (1) *Safety inputs are server-derived, not injected*: the drills and reports API
> endpoints compute the H1 allowance and H5 verdict server-side from the player's real
> ledger/wellness/config (`cricai_api.services.safety_state`); client-supplied verdicts are
> ignored and client allowances can only restrict, never expand (review findings 11/20/12/21 —
> a client-trusted verdict was a bypass). Worker-internal seams (`generate_report`,
> `planner_agent.build_plan`) remain injection-based for testability; their production callers
> compute inputs from DB state. (2) *Agent pipeline stages never digest-resume*: analysis,
> progress, planner, safety and report read mutable DB state, so they re-execute on every run
> (`cricai_worker.pipeline.NON_RESUMABLE`); only media stages short-circuit by input digest
> (findings 0/5/19/32/33 — digest-resume replayed stale safety verdicts). Additionally,
> re-derive cascades (not blocks on) `evidence_verdicts` since they hard-FK regenerable
> findings, while plain pipeline re-runs BLOCK when coach verdicts exist
> (`AnalysisBlockedError`) — block-beats-corrupt applies to the unconfirmed path.

- **LLM is a wording layer only** (design doc §3): provider adapter (`LLMProvider` protocol,
  deterministic `FakeLLMProvider`, Anthropic client behind lazy import, stub-tested — the
  FakePoseProvider pattern). The LLM sees findings JSON, history summary, rules, tone guide —
  never raw video, never object-store keys. Numeric-claim validator + template conformance +
  injection red-team are release-gating SAF tests.
- **Safety supremacy is structural** (US-H5, invariant #1): the safety agent is topologically
  last in the pipeline DAG (asserted by a topology test); its verdict text is inserted verbatim
  post-LLM and SHA-256-verified in the final artifact; publish-time validator rejects any
  plan/report violating H1/H4 state. Warning texts live in `docs/safety_workload.md` + code
  constants, not in LLM-reachable content.
- **Rule/threshold config is data with approvals**: one versioned `safety_configs` table holds
  H1 thresholds + H2 split defaults (append-only versions; raising any ceiling requires
  `approved_by` with coach role; every change writes AuditLog). Coaching rules are data
  (`coaching_rules` rows, YAML import/export CLI) with `id, author, approved_by, version,
  rationale`; per-player `rule_overrides` logged.
- **Bowling ledger is entry-based, not column-aggregated**: `bowling_ledger_entries`
  (player, date, balls, intensity spin|pace_intent|throwdown, optional session link, source
  auto-backfill|manual, audited). A backfill job derives entries from tagged bowling sessions
  (`session_type == bowling`; MIXED sessions contribute only via explicit manual entries in V2 —
  documented limitation). Rolling-7 windows, age bands (from `Player.birthdate`, birthday
  boundary honored), and violations (`workload_ceiling`, `day_pattern_violation`) are computed
  pure-functionally in `cricai_coaching.workload` from entries + config — no denormalized
  weekly columns to drift.
- **Reports ship as JSON + printable HTML in Phase 5.** Dashboard rendering and PDF/PNG export
  land with Epic K (Phase 6); the report body JSON is the pinned artifact, the HTML renderer is
  a pure function over it. Weekly/monthly (G5) reuse the same `reports` table with `kind`.
- **US-H3 fatigue is a scorer + report note in Phase 5**; the ≤30 s live-dashboard nudge is
  Phase 6 (K). False-positive guard (block-context change) implemented now.
- **US-L4 drift protocol**: 10-balls/week stay manually tagged forever — shipped as the
  `ground_truth_eligible` sampling job + agreement monitor + canary test; the human workflow is
  a runbook section in `docs/observability.md`.
- **Findings/reports are machine-derived and regenerable** — they reference balls via evidence
  JSON (`ball_ids`), not `(session_id, ball_no)` columns, so they deliberately stay OUT of
  `EVENT_DEPENDENT_TABLES` (no new guard params). Staleness is handled by the re-derive
  cascade: any event mutation for a session marks its findings/reports/pipeline runs stale and
  the pipeline regenerates them. Guard semantics for manual data are unchanged.
- **Re-derive cascade (Phase-3 debt)**: `cricai_worker.rederive` deletes machine-derived
  dependents (clips, pose_tracks, machine-source ball_metrics values, ball_tracks,
  bounce_estimates, findings, reports, pipeline runs) for a session and re-enqueues the DAG.
  It REFUSES (409, listing rows) when manual rows exist that renumbering would corrupt
  (ball_tags, bounce_marks, reference_balls, event_corrections) unless
  `confirm_manual_invalidation=true` — then those are deleted too and the deletion audited.
  This replaces the Phase-3 dead-end while keeping block-beats-corrupt for unconfirmed calls.
- **DAG runner is our thin layer over RQ** (design decision #2): per-stage status rows,
  retry with backoff, resume-from-failure by re-running the session pipeline (completed stages
  short-circuit via input digests), concurrent-session safety via per-session advisory lock.
  In-process synchronous runner for tests; real Redis for IT. Stage SLO timings recorded
  (`pipeline_stages.started/finished_at`); the 4 h SLO + soak are real-hardware milestones.

## Foundation (built inline first)

1. **Enums** (`cricai_data.enums`, all wired into `_ENUM_TYPES`): `DeliveryIntensity`
   (spin|pace_intent|throwdown), `ReportKind` (daily|weekly|monthly), `ReportStatus`
   (draft|published|blocked), `StageStatus` (pending|running|succeeded|failed|skipped),
   `AlertAudience` (parent|developer), `EvidenceVerdict` (confirms|not_supported).
   Non-column StrEnums: `SafetyCode` (workload_ceiling|day_pattern_violation|pain_flag),
   `FindingSeverity` (info|minor|major).
2. **Schema migration** (one Alembic revision, `down_revision = "b7e2f9a4c1d3"`):
   - `coaching_rules` (id, rule_key, version int, author, approved_by, rationale,
     definition jsonb, enabled bool, created_at, uq (rule_key, version)).
   - `rule_overrides` (id, rule_key, player_id FK, action, params jsonb, reason, actor,
     created_at).
   - `safety_configs` (id, version int unique, config jsonb, approved_by, reason, created_at)
     — append-only; latest version wins; seed v1 from `docs/safety_workload.md` defaults.
   - `findings` (id, session_id FK, run_id nullable, agent, rule_key nullable, kind,
     severity FindingSeverity-string, metric, condition jsonb, n int, effect_size nullable,
     confidence, ball_ids jsonb, evidence jsonb, payload jsonb, created_at).
   - `reports` (id, player_id FK, session_id FK nullable, kind ReportKind, period_start,
     period_end, status ReportStatus, body jsonb, safety_sha256 nullable, quality jsonb
     nullable, created_at, uq (player_id, kind, period_start, period_end)).
   - `report_llm_audits` (id, report_id FK, prompt_key, request jsonb, response jsonb,
     accepted bool, reason nullable, created_at).
   - `drills` (id, name unique, setup, machine_settings jsonb, ball_count int, target_metric,
     intent BlockIntent, author, enabled bool, created_at).
   - `drill_plans` (id, player_id FK, plan_date date, blocks jsonb, finding_ids jsonb,
     safety jsonb, safety_sha256 nullable, created_by, created_at, uq (player_id, plan_date)).
   - `bowling_ledger_entries` (id, player_id FK, entry_date date, balls int,
     intensity DeliveryIntensity, session_id FK nullable, source, note nullable, created_by,
     created_at).
   - `wellness_checkins` (id, player_id FK, session_id FK nullable, checkin_date date,
     soreness jsonb, energy int nullable, sleep_hours float nullable, pain bool,
     pain_note nullable, created_by, created_at).
   - `pain_clearances` (id, player_id FK, checkin_id FK, cleared_by, role, note, created_at).
   - `pipeline_runs` (id, session_id FK, status StageStatus, detector_context jsonb,
     started_at, finished_at nullable).
   - `pipeline_stages` (id, run_id FK, stage, status StageStatus, attempt int,
     input_digest nullable, output jsonb nullable, error nullable, started_at nullable,
     finished_at nullable, uq (run_id, stage, attempt)).
   - `metric_baselines` (id, player_id FK, metric, zone_key, window, snapshot_date date,
     value float, n int, payload jsonb, created_at,
     uq (player_id, metric, zone_key, window, snapshot_date)).
   - `alerts` (id, audience AlertAudience, code, severity, detail jsonb,
     session_id FK nullable, acknowledged bool, created_at).
   - `evidence_verdicts` (id, finding_id FK, verdict EvidenceVerdict, actor, note nullable,
     created_at).
3. **Registries** (immediately, per Phase-3/4 lesson): BACKUP_ENTITIES += coaching_rules,
   rule_overrides, safety_configs, reports, drills, drill_plans, bowling_ledger_entries,
   wellness_checkins, pain_clearances, evidence_verdicts (human-input/artifact tables;
   findings/pipeline_*/metric_baselines/alerts/report_llm_audits are regenerable or audit-class
   — excluded, documented). Privacy session-delete += findings, session reports, pipeline
   runs/stages, session-linked ledger entries and check-ins (with `*_deleted` counts);
   `wellness_checkins.pain_note`, `soreness` + `drill_plans` free text added to egress PII set.
   No EVENT_DEPENDENT_TABLES additions (decision above).
4. **Provider scaffold** (`cricai_coaching.llm`): `LLMRequest`/`LLMResponse` dataclasses,
   `LLMProvider` protocol, `LLMError`, `FakeLLMProvider` (seeded, deterministic, template-echo
   with injectable canned outputs — the only provider tests use). Anthropic adapter is story
   g3's (`llm_anthropic.py`, lazy import, stub-tested).
5. **Router placeholders** `rules.py`, `reports.py`, `workload.py`, `wellness.py`,
   `pipeline.py`, `drills.py`, `alerts.py` wired in `app.py` (ROUTER_MODULES 21 → 28), plus
   `docs/observability.md` skeleton and `docs/coaching_rules.md` skeleton (rule DSL reference).

## Story fan-out (parallel worktree agents; disjoint files)

| Agent | Stories | Owns (new files + their tests) |
|---|---|---|
| g1 | US-G1 record + US-G2 rules | `cricai_data.ballrecord` (canonical per-ball assembler + versioned JSON Schema at `packages/data/src/cricai_data/schemas/ball_record-v1.json`, producer validation, unknown-major rejection, v1→v1.1 migration demo), `cricai_coaching.rules` (DSL parse/validate, runner with min-sample gate, overrides, finding emission), `routers/rules.py` body, seed rules in `packages/data/rules/*.yaml` + import/export CLI `scripts/rules_io.py`, SAF lint over all rule texts |
| g2 | US-G3 daily report + US-G6 evidence | `cricai_coaching.report` (finding ranking severity×frequency×trend, one-correction/one-drill/one-goal template, honesty path, secondary≤3 + positive, printable HTML renderer), `cricai_coaching.evidence` (evidence objects, ≥2-clips rule, link-integrity crawler), `cricai_worker.generate_report`, `routers/reports.py` body (incl. coach evidence verdicts + claim-recomputation endpoint used by IT) |
| g3 | US-G4 LLM writer | `cricai_coaching.llm_writer` (ReportWriter protocol impl over `cricai_coaching.llm`, template conformance, numeric-claim validator with whitelisted arithmetic, safety-verbatim insertion, rule-based fallback, audit rows), `cricai_coaching.llm_anthropic` (lazy-import adapter over stub), injection red-team SAF suite |
| h1 | US-H1 ledger + US-H2 split | `cricai_coaching.workload` (rolling-7 windows, age bands + birthday boundary, overs math, pace-intent weighting, violations, H2 plan-vs-actual reconciliation + >25% deviation + fun-block protection predicate), `cricai_worker.backfill_workload`, `routers/workload.py` body (ledger CRUD w/ audits, config versions w/ coach-approval gate), exhaustive SAF boundary matrix (BDD Gherkin ACs as tests) |
| h2 | US-H4 wellness + US-H5 supremacy + US-H3 fatigue | `cricai_coaching.wellness` (check-in validation, pain state machine, ≥2-in-14-day escalation, absence visibility), `cricai_coaching.safety_agent` (verdict object {active codes, verbatim text, sha256}, publish-time validator, hash verification), `cricai_coaching.fatigue` (rolling window vs baseline, context-change guard), `routers/wellness.py` body, red-team SAF suite (adversarial notes/rule-text/player-request injections → 100% blocked) |
| j1 | US-J1 pipeline + US-L1 DAG + re-derive | `cricai_coaching.contracts` (typed agent I/O schemas + validators), `cricai_worker.pipeline` (DAG runner over RQ: stage registry, retry/backoff, resume via input digests, per-session advisory lock, trace persistence, degraded-but-honest paths, topology assertion incl. safety-last), `cricai_worker.rederive` (cascade per scope decision), `routers/pipeline.py` body, fault-injection IT matrix + golden trace |
| j2 | US-J2 analysis + US-J3 planner + US-J4 progress | `cricai_coaching.analysis_agent` (rules run + pattern probes: zone contrasts, first-50/last-50; effect-size floor + top-k discipline; seeded determinism), `cricai_coaching.planner_agent` (findings→drill mapping, H2 split + H1 allowance + pain suppression constraints, machine-settings validation, traceability), `cricai_coaching.progress_agent` (baselines, trend/plateau/regression detectors with guardrails, frozen snapshot, size-bounded typed history), `cricai_worker.nightly_baselines`, `routers/drills.py` body |
| l4 | US-L4 observability | `cricai_coaching.quality` (per-session data-quality score: sync, exposure proxy, pose coverage, track coverage, calibration freshness; honesty-banner threshold), `cricai_coaching.drift` (weekly auto-vs-manual agreement, canary), `cricai_worker.drift_monitor`, `routers/alerts.py` body (audience routing, ack), `docs/observability.md` body |

## Pinned cross-group contracts

1. **BallRecord v1** (g1 produces; g2/j2 consume): JSON Schema at
   `packages/data/src/cricai_data/schemas/ball_record-v1.json`; assembled shape mirrors US-G1's
   example: identity (ball_id=ball_no, session_id, block_id, mode from session_type/block),
   context (bowler=BowlerSource, speed_kph), delivery (line, length, bounce_xy), technique
   (footwork, shot, front_foot_direction_cm, head_stability_score, bat_path, contact_quality),
   outcome (outcome, control), `confidence: {field→float}`, `source: {field→str}`,
   `clips: {camera_id→clip_id}`. Nullable-with-reason inherited from MetricValue payloads.
   `schema_version: "1.0"`; consumers reject unknown MAJOR.
2. **Finding** (g1 rules + j2 probes produce; g2/g3/j2-planner consume): `{finding_id, agent,
   rule_key|probe, kind, severity: info|minor|major, metric, condition: {zone filters…}, n,
   effect_size|null, confidence, ball_ids: [int], evidence: {ball_no→{camera_id→clip_id}},
   text_data: {…rule-authored strings only…}}`. Free prose NEVER originates in findings (J2 AC).
3. **SafetyVerdict** (h1/h2 produce via `safety_agent.evaluate`; g2/g3/j1/j2 consume):
   `{active: bool, codes: [SafetyCode], text: str, sha256: hex}` — `text` is inserted VERBATIM
   into any report/plan; `sha256 = SHA-256(text)`; publish validator recomputes and rejects
   mismatch. Planner treats `workload_ceiling`/`pain_flag` as hard bowling blocks.
4. **Report body v1** (g2 owns; g3 words it; j1 publishes): `{kind, period, main_correction:
   {finding_id, text, evidence}, drill: {drill_id, text, machine_settings, success_metric},
   goal: {metric, target, condition}, secondary: [≤3], positive, safety: SafetyVerdict|null,
   honesty_banner: str|null, coverage_note: str|null, fatigue_note: {text, window,
   control_drop_points, degrading_metrics}|null, claims: [{value, metric, recompute_key}]}` —
   `coverage_note` and `fatigue_note` are always present (post-review amendments: US-H3 fatigue,
   the day-scoped degraded-day coverage note); every number in any text also appears in `claims`
   for the recomputation test.
5. **DrillPlan blocks** (j2 produces; h1/h2 validate): `[{intent: BlockIntent, balls: int,
   drill_id|null, machine_settings, success_metric, finding_id|null}]`; totals within daily
   range; fun block present and unconverted; bowling balls ≤ remaining H1 allowance (0 when
   ceiling/pain active).
6. **QualityScore** (l4 produces; g2 consumes): `{components: {sync, exposure, pose_coverage,
   track_coverage, calibration_freshness → float 0..1}, composite: float, banner: str|null}`.
7. **ProgressSnapshot** (j4 produces; g2/g5 consume): `{metric, zone_key, window,
   points: [{session_id, date, value, n}], baseline, direction: improving|flat|regressing,
   qualified: bool}` — `qualified` false unless ≥3 sessions and ≥30 balls/point.
8. **Pipeline stages** (j1 owns registry): fixed order `probe → calibrate_check → events →
   clips → pose → detect_track → metrics → analysis → progress → planner → safety → report`;
   stage names are the `pipeline_stages.stage` vocabulary; safety is asserted last-before-report.
9. **Writer seam** (g2↔g3): `cricai_worker.generate_report(ctx, session_id, *, writer:
   ReportWriter | None = None)`; `ReportWriter` protocol lives in `cricai_coaching.report`;
   rule-based writer is the default and the fallback on any `LLMError`/validation rejection.

## Verification & gates

Same as Phases 1–4: per-package 100% line+branch (no pragma), mypy strict, ruff; SAF marker
suite (workload BDD matrix, red-team injection, content lints, planner constraints, hash
verification) never waivable; integration on real temp-cluster PG (+ real Redis for DAG IT);
golden demo digest updated only intentionally. After merge: 12-dimension adversarial review →
fix fan-out → fix-verification audit → full gates → PR.
