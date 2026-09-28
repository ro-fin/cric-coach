# Phase 6 Plan — Leg-Spin Lab & Dashboard (Epics I + K, plus G5/J5/L5 and deferred debts)

**Stories:** US-I1–I7, US-K1–K5 (US-B5 subsumed by K1), US-G5 (weekly/monthly reports —
generation shipped here because K4 renders it), US-J5 (coach review gate), US-L5 (live-mode
hooks) + the documented Phase-6 debts: the five CV stage call-adapters, canary re-derivation,
PDF/PNG export, live fatigue nudge budget hooks. · **Branch:** `phase-6-legspin-dashboard`
**Packages:** `cricai_vision` (bowler pose, release detection, trajectory analytics,
variation classifier spine), `cricai_coaching` (bowling rules/probes/report), `cricai_worker`
(stage adapters, rollup job, capture seams), `cricai_data` (BallRecord v1.1 + Phase-6 schema),
`cricai_api` (notes, targets, labels, export, review-gate), `apps/web` (Epic K, greenfield).

Scope decisions (recorded as autonomous decisions; owner may override):

- **BallRecord bumps to v1.1 (additive MINOR).** New optional bowling fields —
  `release_height_cm`, `release_frame_offset`, `brace_state`, `falling_away_deg`, `turn_cm`,
  `apex_m`, `dip_flag`, `variation_intent`, `variation_detected`, `target_hit` — all
  nullable-with-reason, all covered by the existing per-field `confidence`/`source` maps.
  `schema_version: "1.1"`; v1 consumers keep working (unknown-MAJOR-only rejection is already
  the contract). The assembler populates them only for `mode == "bowling"` records.
- **No RPM/seam-axis claims, ever** (backlog honesty rule): turn/flight/dip are geometric
  measurements from tracked trajectories (post-bounce lateral deviation, apex height, descent
  steepening). A banned-claim lint (`rpm`, `revs`, `spin rate`) gates every bowling-facing
  string (SAF).
- **Variation classification is honest V1**: intent labels come from the bowler/coach at
  tagging time (`delivery_labels.variation_intent`); the classifier predicts from trajectory +
  release features; the report shows agreement/confusion honestly and never asserts a
  variation as fact when confidence is below the gate (≥80% 3-way agreement per T3 before the
  auto label appears anywhere kid-facing).
- **Camera roles become first-class**: `camera_configs.role` (CameraRole enum) replaces
  position-label guesswork; C5–C7 register as bowling-side roles. The registry keeps C1–C8
  pattern validation. *(Post-review amendment, 2026-07-11: as shipped, roles are registry
  metadata + the `GET /cameras?role=` filter only — no pipeline stage consumes cameras by
  role; stages select by camera id (bowling action pins C5). Role-driven stage consumption
  is a future seam; docs/camera_setup.md states the shipped truth.)*
- **Dashboard is server-data-faithful**: `apps/web` renders ONLY what the API returns — no
  client-side metric computation (US-K4 data-parity AC). Charts are hand-rolled SVG components
  (deterministic, fully testable at the 100% vitest thresholds; no charting/video deps beyond
  the HTML5 `<video>` element). `@testing-library/react` + `@testing-library/user-event` are
  the only new dev-deps. Playwright E2E defers to Phase 7 golden.
- **US-G5 ships server-side with K4**: a rollup job (`cricai_worker.rollup_reports`) assembles
  weekly/monthly bodies from daily reports + `metric_baselines` trend series (per-point n,
  qualification guards), context-normalized regression alerts (same zone; `nightly_baselines`
  gains per-zone slices), and a `milestones` log (`personal_best` finally gets its caller).
  Weekly/monthly bodies reuse the pinned Report-body-v1 shape (fatigue_note/coverage_note
  null; trends section carried in `payload`-style keys documented in contract #3 below).
  *(Post-review amendment, 2026-07-11: "from daily reports" ships as minimal provenance —
  the period's daily reports are traced by `source_daily_report_ids`, while all numeric
  content is assembled from the `metric_baselines` trend series; a narrative period recap
  digesting the week's corrections/safety history is a deferred follow-up story. This does
  not change pinned contract #3, whose body shape has no recap field.)*
- **US-J5 approval gate is a versioned setting**: `app_settings` rows (append-only, like
  safety_configs) carry `report_review: {mode: auto_publish|coach_gate, timeout_hours}`.
  In coach_gate mode a generated report stays `draft` with `review_due_at`; the publish gate
  auto-publishes at timeout (worker sweep) unless the coach blocked/edited it. Veto-rate
  analytics already exist (Phase 5) — the queue endpoint + UI land here.
  *(Post-review amendment, 2026-07-11: the review-queue **UI is deferred to Phase 7** —
  the fan-out table below assigned j5l5 only the endpoint and no k-agent the screen, and no
  UI shipped; the drop went unconfessed and is recorded here as a decision. Until Phase 7,
  coach_gate operates API-only: held drafts are listed via `GET /settings/review-queue`
  (curl/httpie with the coach token) and reports auto-publish at `timeout_hours` via the
  worker sweep — a coach who enables coach_gate without working that endpoint gets timeout
  default-publishes with no visible queue.)*
- **US-L5 is design-now/build-light**: the same `app_settings` versioning carries
  `live_mode: {enabled: false, allowlist: [...]}`; the per-ball streaming path ships as a
  documented seam (worker hook + allow-list validator + ≤30s budget test with a fake clock),
  not a production streaming stack. The live fatigue nudge implements against this seam.
- **CV call-adapters are foundation** (the keystone debt): `cricai_worker.stage_adapters`
  gives the five media stages real `(ctx, session_id)` entrypoints (motion energy from stored
  probe data, clip source resolution, pose provider + frames, detector provider from the
  production model registry with honest fallback, fuse inputs). Canary re-derivation wires
  through the same adapters, retiring the standing weekly `drift_canary_missing`.
- **Bowling ground truth mirrors batting**: `delivery_labels` rows are the leg-spin analogue
  of ball_tags (human input, backup-covered, event-dependent guard-protected via
  (session_id, ball_no)); manual labels always win over model output.

## Foundation (built inline first)

1. **Enums** (`cricai_data.enums`): `CameraRole` (batting_side|bowling_side|wrist|front_on|
   other — column), `BowlingVariation` (leg_break|top_spinner|googly|slider|flipper|unknown —
   column), `BraceState` (braced|bent|collapsed — non-column, BallRecord vocabulary),
   `MilestoneKind` (personal_best|volume|streak — column), `ReviewMode` (auto_publish|
   coach_gate — non-column, app_settings vocabulary).
2. **Schema migration** (one revision, `down_revision = "26ddf8103717"`):
   - `camera_configs.role` (CameraRole, nullable — existing rows stay null until re-registered).
   - `bowling_targets` (id, session_id FK, block_id FK nullable, line, length, description,
     created_by, created_at) — declared targets for US-I4.
   - `delivery_labels` (id, session_id FK, ball_no, variation_intent BowlingVariation,
     variation_detected BowlingVariation nullable, labeler, source manual|model, created_at,
     uq (session_id, ball_no)) — joins EVENT_DEPENDENT_TABLES + BACKUP_ENTITIES + privacy
     cascade + guard test seeders.
   - `coach_notes` (id, player_id FK, session_id FK nullable, ball_no nullable, body Text
     UNTRUSTED, author, visibility coach_only|shared, created_at) — body joins the egress
     strict-PII set; BACKUP_ENTITIES; privacy cascade for session-linked rows.
   - `milestones` (id, player_id FK, kind MilestoneKind, metric, value float, context jsonb,
     achieved_on date, created_at, uq (player_id, kind, metric, achieved_on)).
   - `reports.review_due_at` (DateTime nullable — J5 gate).
   - `app_settings` (id, version int unique, settings jsonb, approved_by, reason, created_at)
     — append-only; seed v1 {report_review: {mode: auto_publish, timeout_hours: 24},
     live_mode: {enabled: false, allowlist: [...]}} mirrored in a code constant + drift test
     (the safety_configs pattern).
3. **BallRecord v1.1**: schema file update (additive), assembler bowling-mode population from
   ball_metrics/delivery_labels/bounce+track rows, v1-consumer compatibility test, consumer
   validators accept 1.x.
4. **Registries** (immediately): BACKUP_ENTITIES += bowling_targets, delivery_labels,
   coach_notes, milestones, app_settings; privacy delete += session-linked coach_notes,
   delivery_labels, bowling_targets (+counts); egress strict += "body"; EVENT_DEPENDENT_TABLES
   += delivery_labels (with test seeder).
5. **Router placeholders**: `notes.py`, `targets.py`, `labels.py` wired in app.py
   (ROUTER_MODULES 28 → 31); docs/coaching_metrics.md bowling-checkpoint section skeleton.

## Pre-fan-out agent (parallel-safe, owns pipeline.py exclusively)

**f-adapters**: `cricai_worker.stage_adapters` (real call-adapters for events/clips/pose/
detect_track/metrics per the DEFAULT_STAGE_PATHS pending-comment), registry updates, retire
the bare-run degradation expectations to real-execution tests (SQLite fakes stay for unit;
PG integration proves a full real-media run on synthetic session), canary re-derivation in
drift_monitor via the adapters, remove the stale `drift_canary_missing`-by-design note in
docs/observability.md.

## Story fan-out (parallel worktree agents; disjoint files)

| Agent | Stories | Owns (new files + their tests) |
|---|---|---|
| i1 | US-I1 capture C5–C7 | camera-role registration flow (`routers/cameras.py` role support), session expected-camera roles, 7-camera health/sync checks, bowling-cam clip coverage (`cricai_worker` capture seams), `routers/targets.py` body (declare/list block targets) |
| i2 | US-I2 checkpoints + US-I3 release | `cricai_vision.bowler_pose` (bowler subject prior, C5/C6 zones), `cricai_vision.release` (release-frame ±2f @120fps + hand position + release height), `cricai_coaching` checkpoint evaluators (brace, falling-away, head position at release), docs/coaching_metrics.md bowling section body |
| i3 | US-I4 targets + US-I5 flight | `cricai_vision.trajectory` (bowling-end line/length vs declared target, accuracy scorecard math, post-bounce turn_cm, apex_m, dip detection — geometric only, banned-claim lint), zone-frame reuse w/ bowling-end orientation |
| i4 | US-I6 variation spine | `delivery_labels` API (`routers/labels.py` body), dataset/label round-trip for delivery-level labels (labelio extension), trainer generalization (metric/class keys parameterized), `legspin-variation` registry model type + honest eval (confusion matrix, ≥80% gate), deterministic fake classifier |
| i5 | US-I7 leg-spin report | bowling coaching rules (`packages/data/rules/bowling_*.yaml`), bowling probes in analysis path (mode branch), leg-spin session report template (accuracy scorecard, release scatter data, variation agreement — honest), Warne/Saqlain learning modules as static drill/lesson content, SAF content lint |
| k1 | US-K1 timeline (+B5) | `apps/web` session list/detail, ball-by-ball timeline, multi-cam clip player (HTML5, frame-step, anchor sync), filters; 500-ball index budget test |
| k2 | US-K2 pitch map | `apps/web` pitch-map + zone analytics view (SVG, batting+bowling end frames, handedness mirror, click-through to balls), consuming existing heatmap/bounce APIs |
| k3 | US-K3 notes + US-K5 report surface | `routers/notes.py` body (untrusted-input handling), `apps/web` notes panel, report surface (render body v1 incl. safety/coverage/fatigue sections), print CSS + PDF/PNG export endpoint (`routers/reports.py` export addition using the HTML renderer) |
| k4 | US-K4 progress + US-G5 | `cricai_worker.rollup_reports` (weekly/monthly assembly + scheduler seam), per-zone baseline slices in nightly_baselines, context-normalized regression alerts, `milestones` writer + API, `apps/web` progress dashboard (trend SVGs, milestone feed, kid-mode lint SAF) |
| j5l5 | US-J5 + US-L5 + nudge | `app_settings` API (versioned, coach-gated), report review-gate mode (draft hold + review_due_at + timeout auto-publish sweep in worker), review queue endpoint, live-mode seam (allow-list validator, per-ball hook, ≤30s fatigue-nudge budget test w/ fake clock) |

## Pinned cross-group contracts

1. **BallRecord v1.1** (foundation owns): field list above; bowling fields null for batting
   records; consumers accept any 1.x.
2. **Bowling finding kinds** (i5 produces; report/planner consume): `release_consistency`,
   `target_accuracy`, `variation_agreement`, `action_checkpoint` — same Finding wire contract
   (#2, Phase 5), free prose only from rule/probe text_data.
3. **Weekly/monthly body** (k4 produces; k3/k4 web render): Report-body-v1 + `trends:
   [{metric, zone_key, points: [{date, value, n}], direction, qualified}]` and `milestones:
   [{kind, metric, value, achieved_on}]` keys; claims[] covers every rendered number;
   fatigue_note/coverage_note present-null.
4. **Web API surface** (k-agents consume, never invent): existing routers + the three new
   ones (notes, targets, labels) + reports export endpoint + review queue endpoint. Any gap
   is a deviation report, not an ad-hoc endpoint.
5. **Delivery label vocabulary**: BowlingVariation enum; `variation_intent` is ground truth
   (manual), `variation_detected` is model output — never conflated; report shows agreement.
6. **Release metrics** (i2 produces; i3/i5 consume): per-ball `{release_frame, release_ms,
   release_height_cm, hand_xy, brace_state, falling_away_deg, confidence}` persisted via
   ball_metrics (phase=pre_release, bowling metric names) — nullable-with-reason.
7. **app_settings** (j5l5 owns): versioned append-only; `report_review.mode` consumed by
   generate_report/publish gate; `live_mode.allowlist` is the ONLY data the live seam may
   emit (SAF: technique corrections never live).

## Verification & gates

Same as Phases 1–5: per-package 100% line+branch (no pragma), mypy strict, ruff; vitest 100%
thresholds for every new web component; SAF suite (banned-claim lint, kid-mode lint, live
allow-list, variation-honesty) never waivable; integration on real temp-cluster PG; golden
demo digest updated only intentionally (bowling golden: 60-delivery target block scorecard
matches manual count — T5 #4). After merge: adversarial review → fix fan-out →
fix-verification audit (regression tests proven red on pre-fix code, mutation checks on new
validators) → full gates → PR.
