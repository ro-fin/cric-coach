# Phase 3 Plan — Events, Clips, Pose & Batting Metrics (Epics D + E)

**Stories:** US-D1–D4 (software layers), US-E1–E5 · **Branch:** `phase-3-events`
**Packages:** `cricai_vision` (events, pose, audio, overlay, ffmpeg), `cricai_coaching`
(batting metrics), `cricai_worker` (jobs), API routers `events.py`, `clips.py`, `ball_metrics.py`.

Scope split with later phases (recorded as autonomous decisions):
- D4's timeline UI and E5's comparison UI belong to the Phase 6 dashboard (Epic K); Phase 3
  builds their full data/API layer (corrections, renumbering, ground-truth export, reference
  balls, anchor-sync math) so the UI is a pure view later.
- E3 bat-path/contact metrics that require ball/bat tracking (Epic F) ship as pose-only
  proxies flagged `proxy=true` per the US-E3 AC.
- E4 V1 uses manual sources (US-B4 tags, US-C5 bounce marks) unified under one schema with
  `source` provenance; Epic F later swaps in auto values under the same columns.
- Re-detection on a pipeline-processed session is deliberately a dead end in Phase 3 (block
  beats corrupt): the US-D1 guard aborts with `DetectionBlockedError` whenever a
  to-be-replaced ball has dependent rows (`cricai_data.models.EVENT_DEPENDENT_TABLES`), and
  machine-derived dependents (clips, pose tracks, ball metrics) have no removal API — so
  once clips are cut, unblocking requires operator intervention (out-of-band dependent-row
  cleanup, or full US-L3 session deletion). The error message states this honestly rather
  than advising remediation that does not exist. The delete-dependents-and-re-derive
  cascade belongs to Phase 5 job orchestration (Epic J).
- `docs/coaching_metrics.md` metric definitions await human-coach sign-off (owner review).

## Foundation (built inline first)

1. **Enums** (`cricai_data.enums`): `EventSource` (auto|manual|corrected), `ClipStatus`
   (pending|cut|failed|gap), `AnchorPoint` (release|contact).
2. **Schema migration** (one Alembic revision):
   - `ball_events` (id, session_id FK, ball_no int, start_ms, release_ms, contact_ms nullable,
     end_ms, confidence float, source EventSource, detector_version str, valid bool,
     uq (session_id, ball_no); ms are video-timeline milliseconds on the reference camera C1)
   - `event_corrections` (id, event_id FK, action accept|adjust|reject|add-string, before jsonb,
     after jsonb, actor, at) — versioned ground truth (US-D4)
   - `clips` (id, session_id, ball_no, camera_id, object_key nullable, start_ms, end_ms,
     status ClipStatus, error nullable, uq (session_id, ball_no, camera_id)) — `gap` rows record
     missing cameras loudly (US-D2)
   - `pose_tracks` (id, session_id, ball_no, camera_id, model_name, model_version,
     landmarks_key — object-store key of the per-ball parquet/JSON payload, frame_count,
     availability float, subject_confidence float, uq (session_id, ball_no, camera_id))
   - `ball_metrics` (id, session_id, ball_no, phase pre_release|contact|flight-string,
     metrics jsonb — each value {value, unit, confidence, reason-if-null, proxy?}, schema_version,
     uq (session_id, ball_no, phase))
   - `reference_balls` (id, session_id, ball_no, label, marked_by, at) — US-E5 model examples
3. **Provider scaffolds** (`cricai_vision`):
   - `pose.py`: `PoseFrame`/`PoseTrack` dataclasses (33 landmarks, image+world xyz, visibility),
     `PoseProvider` protocol, `FakePoseProvider` (seeded, deterministic synthetic skeletons —
     the only provider tests use). Real MediaPipe adapter is story e1's (`mediapipe_pose.py`,
     lazy import, covered via injected module stub — never real inference in tests).
   - `ffmpeg.py`: pure clip-command builder (keyframe-safe seek, pre/post-roll math) +
     `run_ffmpeg` subprocess adapter mirroring `cricai_data.probe` error split
     (`FfmpegError` / `FfmpegUnavailableError`).
4. **Worker skeleton** (`cricai_worker.jobs`): idempotent plain-function jobs operating on
   (session_factory, store) pairs; RQ enqueue wrappers kept thin; crash-resume by re-running
   (status-driven row scans). No DAG infra yet (Phase 5 / Epic J).
5. **Router placeholders** `events.py`, `clips.py`, `ball_metrics.py`, `reference.py` wired
   in `app.py` (ROUTER_MODULES == 18), plus `docs/coaching_metrics.md` skeleton with
   per-story sections.

## Story fan-out (parallel worktree agents; disjoint files)

| Agent | Story | Owns (new files + their tests) |
|---|---|---|
| d1 | US-D1 event detection | `cricai_vision.events` (cadence/motion segmentation, confidence, dedupe/merge, idempotent replace of source=auto), `scripts/detect_events.py`, `cricai_worker.jobs.detect_events` |
| d2 | US-D2 clip generation | `cricai_worker.jobs.cut_clips` (all-cameras fan, gap rows, resume), `routers/clips.py` (list, per-ball camera set, gap report) |
| d3 | US-D3 audio contact | `cricai_vision.audio_onset` (energy/spectral-flux onset, contact-vs-other classes, SNR degradation, missing-audio fallback flag), refinement job hook |
| d4 | US-D4 corrections API | `routers/events.py` (list/accept/adjust/reject/add, bulk-accept, stable renumbering + relink guarantee, correction versioning, ground-truth export with audit) |
| e1 | US-E1 pose pipeline | `cricai_vision.mediapipe_pose` (adapter over stub), person-selection (crease-zone prior + track continuity), `cricai_worker.jobs.extract_pose` (parquet/JSON to store, linkage, idempotent) |
| e2 | US-E2 pre-release metrics | `cricai_coaching.pre_release` (stance width, head offset, trigger timing, stillness, pickup direction — exact formulas over synthetic skeletons), its `docs/coaching_metrics.md` section |
| e3 | US-E3+E4 contact/decision metrics | `cricai_coaching.contact_metrics` (proxy-flagged), `cricai_coaching.decision` (E4 schema unification from tags+bounce marks with provenance; decision-quality view), `routers/ball_metrics.py`, their doc sections |
| e5 | US-E5 overlays | `cricai_vision.overlay` (skeleton/head-line/contact markers ≤3px, undistortion-aware), anchor-sync math, `reference_balls` endpoints (in `routers/clips.py`? no — own file `routers/reference.py`) |

Same protocol as Phases 1–2: pinned cross-group contracts in the fan-out prompt, temp-stub +
delete before commit, no `git stash`, 100% line+branch, ruff/mypy strict, safety assertions
never weakened, sequential merge in dependency order, full gates, adversarial review workflow,
review-fix fan-out, PR to main.

## Definition of done

`make all` green; integration+safety+golden pass; adversarial review findings fixed; PR merged.
Hardware/model-benchmark ACs (recall/precision on real sessions, PCK studies, coach-label
agreement) are deferred to real-footage milestones — the software carries the benchmark
harness hooks (detector_version, model_version, schema_version columns) so those studies
plug in without schema change.
