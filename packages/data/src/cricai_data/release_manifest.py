"""Release audit manifest (Phase 7 release audit; T6/DoD): the honest story inventory.

Every ``US-*`` story id in ``cricket-ai-coach-user-stories.md`` appears here exactly
once with its shipped status and a pointer to the shipped surface (or, for residue,
to where the gap is recorded). ``packages/data/tests/test_release_manifest.py``
asserts the manifest matches the backlog id-for-id, so a story can never silently
drop out of the closing inventory.

Statuses are deliberately blunt:

- ``shipped`` — the story's surfaces exist, are tested, and passed the phase gates.
  Hardware/model-quality ACs that cannot run on the build machine are tracked on the
  real-hardware milestone register in ``docs/release_checklist.md`` (DEPLOY-BLOCKED),
  not hidden behind this status.
- ``shipped-partial`` — the story's core shipped, but a named acceptance criterion
  did not; the pointer says exactly which (e.g. US-D4's drag-to-adjust review UI).
- ``shipped-as-seam`` — design-now/build-later stories (US-L5): the documented,
  tested seam ships; the production transport does not.
- ``deferred`` — nothing of the story shipped; the pointer records where the
  deferral decision lives. (Empty at release: every backlog story has a surface.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Story shipped whole (hardware-blocked ACs live on the milestone register).
SHIPPED: Final = "shipped"
#: Story core shipped; a named AC did not — the pointer names the residue.
SHIPPED_PARTIAL: Final = "shipped-partial"
#: Design-now/build-later: the documented seam ships, the production build does not.
SHIPPED_AS_SEAM: Final = "shipped-as-seam"
#: Nothing shipped; pointer records the deferral decision.
DEFERRED: Final = "deferred"

#: The only statuses the release audit accepts.
VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {SHIPPED, SHIPPED_PARTIAL, SHIPPED_AS_SEAM, DEFERRED}
)


@dataclass(frozen=True)
class StoryStatus:
    """One backlog story's closing status: id, status bucket, and evidence pointer."""

    story_id: str
    status: str
    pointer: str


MANIFEST: Final[tuple[StoryStatus, ...]] = (
    # EPIC A — Facility, Cameras & Capture (Phase 1: software surfaces).
    StoryStatus("US-A1", SHIPPED, "apps/api routers/cameras.py; docs/camera_setup.md"),
    StoryStatus(
        "US-A2",
        SHIPPED,
        "cricai_vision.capture_qc (completeness, sync offset, flicker FFT); real-rig "
        "120fps recording ACs on the hardware register (docs/release_checklist.md)",
    ),
    StoryStatus(
        "US-A3", SHIPPED, "apps/api routers/lifecycle.py (atomic start/stop, degraded verdicts)"
    ),
    StoryStatus(
        "US-A4",
        SHIPPED,
        "apps/api routers/health_checks.py + routers/alerts.py; cricai_data.healthcheck",
    ),
    StoryStatus("US-A5", SHIPPED, "apps/api routers/checklists.py (machine session gated on ack)"),
    # EPIC B — Session Ingestion & Data Management (Phase 1).
    StoryStatus("US-B1", SHIPPED, "apps/api routers/sessions.py"),
    StoryStatus(
        "US-B2",
        SHIPPED,
        "apps/api routers/videos.py (resumable parts, checksums); cricai_data.probe",
    ),
    StoryStatus("US-B3", SHIPPED, "apps/api routers/blocks.py (no-overlap, gaps, assignment)"),
    StoryStatus("US-B4", SHIPPED, "apps/api routers/tags.py (audited, export/import round-trip)"),
    StoryStatus(
        "US-B5", SHIPPED, "subsumed by US-K1: apps/web/app/sessions (list, detail, player)"
    ),
    StoryStatus(
        "US-B6",
        SHIPPED,
        "cricai_data.retention; apps/api routers/storage_admin.py (forecast, backup "
        "manifest/verify); docs/runbooks/backup_restore.md (quarterly restore drill)",
    ),
    # EPIC C — Calibration & Pitch Mapping (Phase 2).
    StoryStatus("US-C1", SHIPPED, "cricai_vision.intrinsics; scripts/calibrate_cameras.py"),
    StoryStatus("US-C2", SHIPPED, "cricai_vision.extrinsics; apps/api routers/calibration.py"),
    StoryStatus(
        "US-C3",
        SHIPPED,
        "POST /sessions/{id}/calibration/verify (routers/calibration.py); "
        "cricai_worker.calibrate_check",
    ),
    StoryStatus(
        "US-C4",
        SHIPPED,
        "cricai_vision.drift; POST /sessions/{id}/drift-check + backfill flow (Phase 2)",
    ),
    StoryStatus(
        "US-C5",
        SHIPPED,
        "apps/api routers/bounce.py; services/bounce_mapping (era-consistent map_click)",
    ),
    StoryStatus("US-C6", SHIPPED, "cricai_vision.heatmap + zones; apps/api routers/heatmap.py"),
    # EPIC D — Ball Event Detection & Per-Ball Clips (Phase 3).
    StoryStatus(
        "US-D1",
        SHIPPED,
        "cricai_vision.events; cricai_worker.detect_events; recall/precision gate on the "
        "real-footage register (docs/release_checklist.md)",
    ),
    StoryStatus("US-D2", SHIPPED, "cricai_worker.cut_clips; apps/api routers/clips.py"),
    StoryStatus("US-D3", SHIPPED, "cricai_vision.audio_onset; cricai_worker.fuse_contacts"),
    StoryStatus(
        "US-D4",
        SHIPPED_PARTIAL,
        "corrections API whole: routers/events.py (accept/adjust/reject/add, bulk-accept, "
        "stable renumbering, ground-truth export). RESIDUE: the drag-to-adjust web review "
        "UI AC never shipped — apps/web/app/sessions timeline is read-only; corrections "
        "run through the API",
    ),
    # EPIC E — Pose Estimation & Batting Technique Metrics (Phase 3).
    StoryStatus(
        "US-E1",
        SHIPPED,
        "cricai_vision.pose + mediapipe_pose (provider adapter); cricai_worker.extract_pose",
    ),
    StoryStatus(
        "US-E2", SHIPPED, "cricai_coaching.pre_release; definitions in docs/coaching_metrics.md"
    ),
    StoryStatus("US-E3", SHIPPED, "cricai_coaching.contact_metrics"),
    StoryStatus(
        "US-E4",
        SHIPPED,
        "cricai_coaching.decision; GET /sessions/{id}/decision-quality (ball_metrics router)",
    ),
    StoryStatus(
        "US-E5",
        SHIPPED_PARTIAL,
        "cricai_vision.overlay (skeleton/head-line/contact overlays); reference-ball "
        "library via routers/reference.py. RESIDUE: the frame-locked before/after "
        "comparison web view AC never shipped",
    ),
    # EPIC F — Ball / Bat Detection & Tracking (Phase 4).
    StoryStatus(
        "US-F1",
        SHIPPED,
        "apps/api routers/labels.py + routers/datasets.py; scripts/export_label_tasks.py; "
        "docs/labeling_guide.md; label-volume/agreement audits on the real-footage register",
    ),
    StoryStatus(
        "US-F2",
        SHIPPED,
        "cricai_vision.train; scripts/train_detector.py; routers/models.py promotion "
        "gates; benchmark-quality ACs on the real-footage register",
    ),
    StoryStatus(
        "US-F3",
        SHIPPED,
        "cricai_vision.track + trajectory; cricai_worker.track_balls; routers/tracks.py",
    ),
    StoryStatus(
        "US-F4",
        SHIPPED,
        "cricai_vision.bounce_estimate; cricai_worker.estimate_bounces; bounce MAE "
        "(<=15cm -> 10cm) study on the hardware register (docs/release_checklist.md)",
    ),
    StoryStatus(
        "US-F5",
        SHIPPED,
        "cricai_coaching.contact_fusion; cricai_worker.refine_contacts (edge/miss, bat path)",
    ),
    StoryStatus(
        "US-F6",
        SHIPPED,
        "Phase 4: cricai_vision.triangulate (stereo extrinsics pairing, DLT, reprojection "
        "QC vs synthetic stereo scenes; honest limits — no RPM/seam-axis claims). "
        "Surveyed-rig RMS <=5cm + release-height +/-3cm on the hardware register",
    ),
    # EPIC G — AI Coaching Reports & Feedback (Phase 5).
    StoryStatus("US-G1", SHIPPED, "cricai_data.ballrecord (BallRecord v1.1, one language)"),
    StoryStatus(
        "US-G2",
        SHIPPED,
        "cricai_coaching.rules; apps/api routers/rules.py; scripts/rules_io.py; "
        "docs/coaching_rules.md",
    ),
    StoryStatus(
        "US-G3",
        SHIPPED,
        "cricai_coaching.report; cricai_worker.generate_report; publish gate + claims "
        "recompute (routers/reports.py)",
    ),
    StoryStatus(
        "US-G4",
        SHIPPED,
        "cricai_coaching.llm_writer + llm + llm_anthropic (numeric-claim validator, "
        "safety-verbatim insertion, rule-based fallback, injection SAF suite)",
    ),
    StoryStatus(
        "US-G5",
        SHIPPED,
        "cricai_worker.rollup_reports (weekly/monthly); apps/web/app/progress report cards",
    ),
    StoryStatus(
        "US-G6",
        SHIPPED,
        "cricai_coaching.evidence; verdict endpoints + analytics (routers/reports.py)",
    ),
    # EPIC H — Workload, Fatigue & Safety (cross-cutting; enforced from V1).
    StoryStatus(
        "US-H1", SHIPPED, "cricai_coaching.workload + safety_config; apps/api routers/workload.py"
    ),
    StoryStatus(
        "US-H2",
        SHIPPED,
        "batting quality-split reconciliation in cricai_coaching.workload "
        "(reconcile_batting_split) + planner constraints (cricai_coaching.planner_agent); the "
        "plan-vs-actual report AC (>25% deviation flagged) ships as the additive report-body "
        "'batting_split' block (cricai_worker.generate_report over the day's tagged blocks vs "
        "the governing safety config) rendered in cricai_coaching.report.render_report_html and "
        "apps/web ReportView",
    ),
    StoryStatus(
        "US-H3",
        SHIPPED,
        "cricai_coaching.fatigue (post-session; report fatigue_note always present); the "
        "live nudge rides the US-L5 seam within its <=30s budget",
    ),
    StoryStatus(
        "US-H4",
        SHIPPED,
        "cricai_coaching.wellness; apps/api routers/wellness.py (adult clearance flow)",
    ),
    StoryStatus(
        "US-H5",
        SHIPPED,
        "cricai_coaching.safety_agent; server-side verdicts in apps/api "
        "services/safety_state; safety SAF suite (never waived)",
    ),
    # EPIC I — Leg-Spin Analysis Module (Phase 6).
    StoryStatus(
        "US-I1",
        SHIPPED,
        "camera roles (routers/cameras.py) + 7-camera health/sync; routers/targets.py; "
        "role auto-detection recorded as a future seam (phase-6-plan amendment)",
    ),
    StoryStatus("US-I2", SHIPPED, "cricai_coaching.checkpoints; cricai_worker.bowling_action"),
    StoryStatus(
        "US-I3",
        SHIPPED_PARTIAL,
        "cricai_vision.release (release-frame detection + per-ball consistency) shipped; the "
        "release-scatter block ships in the report body and web ReportView and pins the honest "
        "empty-but-correct contract (null stats, per-ball null-with-reason). RESIDUE: production "
        "run_bowling_action_stage wires no px_per_cm/stereo seam, so release heights are "
        "null-with-reason and the scatter ships zero POINTS on any deployment — the numeric "
        "scatter (and the seam wiring it needs) is DEPLOY-BLOCKED on register R3 "
        "(docs/release_checklist.md)",
    ),
    StoryStatus(
        "US-I4",
        SHIPPED,
        "accuracy scorecard vs declared targets: cricai_coaching.bowling_analysis + bowling_report",
    ),
    StoryStatus(
        "US-I5",
        SHIPPED,
        "cricai_worker.bowling_flight over cricai_vision.trajectory (geometric only; "
        "no RPM/seam-axis claims)",
    ),
    StoryStatus("US-I6", SHIPPED, "cricai_vision.variation; cricai_worker.classify_variations"),
    StoryStatus(
        "US-I7",
        SHIPPED,
        "leg-spin report section (cricai_worker.generate_report) + learning modules, "
        "rendered by apps/web/app/reports/ReportView.tsx",
    ),
    # EPIC J — Multi-Agent AI Coach Orchestration (Phases 5-6).
    StoryStatus(
        "US-J1",
        SHIPPED,
        "cricai_coaching.contracts; cricai_worker.agent_stages + stage_adapters "
        "(NON_RESUMABLE safety stages)",
    ),
    StoryStatus(
        "US-J2", SHIPPED, "cricai_coaching.analysis_agent + bowling_analysis (findings, not prose)"
    ),
    StoryStatus(
        "US-J3", SHIPPED, "cricai_coaching.planner_agent; apps/api routers/drills.py (plans)"
    ),
    StoryStatus(
        "US-J4",
        SHIPPED,
        "cricai_coaching.progress_agent; cricai_worker.nightly_baselines; milestones API",
    ),
    StoryStatus(
        "US-J5",
        SHIPPED,
        "cricai_coaching.review_gate; cricai_worker.review_sweep; GET /settings/review-queue; "
        "coach review-queue UI apps/web/app/review (Phase 7)",
    ),
    # EPIC K — Dashboard & Video Review UI (all phases; web shipped Phase 6).
    StoryStatus(
        "US-K1",
        SHIPPED,
        "apps/web/app/sessions/[id] (BallTimeline, MultiCamPlayer, TimelineFilters, "
        "500-ball index budget test)",
    ),
    StoryStatus(
        "US-K2",
        SHIPPED,
        "apps/web/app/pitchmap (SVG map, zone analytics, handedness mirror, click-through)",
    ),
    StoryStatus(
        "US-K3",
        SHIPPED_PARTIAL,
        "text notes whole: routers/notes.py (untrusted-context handling, visibility) + "
        "apps/web/app/notes. RESIDUE: the voice-note capture/local-transcription AC "
        "never shipped — notes are text-only",
    ),
    StoryStatus(
        "US-K4",
        SHIPPED,
        "apps/web/app/progress (workload panel never below the fold, trend charts with "
        "n/confidence, kid mode lint)",
    ),
    StoryStatus(
        "US-K5",
        SHIPPED,
        "apps/web/app/reports (print CSS, export buttons, per-report offline cache); "
        "GET /reports/{id}/export; evidence entries deep-link to the session player at that "
        "ball (/sessions/{id}?ball=N, the US-K2 URL contract) so a clip is playable inline "
        "from the report",
    ),
    # EPIC L — Platform, Data & Non-Functional (all phases).
    StoryStatus(
        "US-L1",
        SHIPPED,
        "cricai_worker.pipeline + context (RQ); apps/api routers/pipeline.py (runs, "
        "resume, rederive)",
    ),
    StoryStatus(
        "US-L2",
        SHIPPED_PARTIAL,
        "monorepo + gates whole: Makefile (per-package 100% line+branch, ruff, mypy, "
        "SAF/golden) + .github/workflows/ci.yml. RESIDUE: hosted CI cannot run — GitHub "
        "Actions startup_failure (account billing); the identical local suite is the "
        "merge gate (docs/release_checklist.md)",
    ),
    StoryStatus(
        "US-L3",
        SHIPPED,
        "cricai_api.auth (role tokens) + egress allow-list; routers/privacy.py "
        "(deletion round-trip, watermark-logged exports, audit)",
    ),
    StoryStatus(
        "US-L4",
        SHIPPED,
        "cricai_coaching.quality (session data-quality score + honesty banner); "
        "cricai_worker.drift_monitor (weekly canary/agreement); docs/observability.md",
    ),
    StoryStatus(
        "US-L5",
        SHIPPED_AS_SEAM,
        "the seam ships, the transport does not: cricai_worker.live_hooks + "
        "cricai_coaching.live_allowlist (flag-gated, allow-listed per-ball hook, <=30s "
        "nudge budget test); no production streaming stack (design-now/build-later)",
    ),
)
