# Phase 4 Plan — Ball/Bat Detection & Tracking (Epic F)

**Stories:** US-F1–F6 (software layers) · **Branch:** `phase-4-tracking`
**Packages:** `cricai_vision` (detect, track, triangulate), `cricai_data` (datasets/registry/tracks
schema), `cricai_worker` (jobs), API routers `datasets.py`, `models.py`, `tracks.py`.

Scope split with real-footage milestones (recorded as autonomous decisions):

- Every model-benchmark AC (ball mAP@0.5 ≥ 0.85, bounce MAE ≤ 15 cm, zone agreement ≥ 90/85%,
  IoU ≥ 0.8 inter-annotator, ≥ 5,000 labeled instances, contact accuracy ≥ 85%, triangulation
  RMS ≤ 5 cm) needs real labeled footage and trained weights — deferred to real-footage
  milestones. The software ships the full benchmark harness now: frozen session-disjoint splits,
  eval-report artifacts with pinned metric names, promotion gates keyed on those exact metrics,
  and agreement-audit tooling, so the studies plug in without schema change.
- Real ultralytics/torch stays behind a `DetectionProvider`/trainer adapter with deterministic
  fakes (the `FakePoseProvider` pattern); adapter innards covered via injected module stubs that
  mirror the real API surface — no `pragma: no cover`, no real inference/training in tests.
- **Experiment tracking is first-party**, not MLflow/W&B: `model_runs` + `model_versions` tables
  plus object-store artifacts satisfy US-F2's "metrics logged per run / model registry /
  promotion rules". A tracker service can be slotted behind the same interface later; avoids a
  heavy always-on service on the LAN box.
- **Labeling tool is not embedded.** US-F1 ships frame-sampling jobs, Label Studio JSON +
  YOLO-txt import/export round-trip, dataset versioning, and `docs/labeling_guide.md`; the tool
  itself (CVAT/Label Studio) runs wherever the operator prefers.
- US-F6 (Could, V3): stereo-pair calibration, DLT triangulation math validated against synthetic
  stereo scenes, and the 3D quality report ship now; the surveyed-target rig test is a
  real-hardware milestone.
- US-F4 auto bounce writes `bounce_estimates`; the manual `BounceMark` always wins — precedence
  resolved in one shared service so heatmap/exports can't drift. Low-confidence bounces are
  excluded from the map by default with an include toggle (served hollow).

## Foundation (built inline first)

1. **Enums** (`cricai_data.enums`): `DatasetSplit` (train|val|test), `LabelClass`
   (ball|bat|stumps|feet|glove|helmet), `AnnotationSource` (manual|imported|model),
   `ModelStage` (candidate|staging|production), `TrainingStatus`
   (pending|running|succeeded|failed), `CalibrationKind.STEREO` added.
2. **Schema migration** (one Alembic revision):
   - `frame_samples` (id, session_id FK, ball_no nullable, camera_id, frame_no, ts_ms,
     object_key, stratum jsonb — lighting/speed/block diversity tags, sampler_version,
     uq (session_id, camera_id, frame_no)) — provenance from every label back to session/ball.
   - `annotations` (id, frame_id FK, label_class LabelClass, x/y/w/h normalized floats,
     annotator, source AnnotationSource, created_at).
   - `datasets` (id, version unique, notes, frozen bool, manifest_digest nullable, created_at)
     — frozen versions are immutable (write paths refuse mutation; digest pins membership).
   - `dataset_members` (id, dataset_id FK, frame_id FK, split DatasetSplit,
     uq (dataset_id, frame_id)).
   - `model_runs` (id, model_name, dataset_id FK, config jsonb, metrics jsonb,
     report_key nullable, status TrainingStatus, trainer_version, started_at, finished_at).
   - `model_versions` (id, model_name, version, run_id FK, stage ModelStage,
     promoted_at/promoted_by nullable, uq (model_name, version)).
   - `ball_tracks` (id, session_id FK, ball_no, camera_id, tracker_version, points_key —
     object-store key of the per-ball trajectory payload, coverage float, segments jsonb,
     flags jsonb, confidence float, uq (session_id, ball_no, camera_id)).
   - `bounce_estimates` (id, session_id FK, ball_no, pitch_x, pitch_y, line/length nullable,
     confidence, tracker_version, created_at, uq (session_id, ball_no)) — auto only; manual
     override lives in `bounce_marks` and wins.
3. **Provider scaffold** (`cricai_vision.detect`): `Detection` dataclass (frame_no, ts_ms,
   label, bbox cx/cy/w/h normalized, score), `DetectionProvider` protocol,
   `FakeDetectionProvider` (seeded, deterministic: parametric ball flight + static
   bat/stumps boxes + configurable dropout/decoys — the only provider tests use).
   Real ultralytics adapter is story f2's (`yolo_detect.py`, lazy import, stub-tested).
4. **Router placeholders** `datasets.py`, `models.py`, `tracks.py` wired in `app.py`
   (ROUTER_MODULES == 21), plus `docs/labeling_guide.md` skeleton with per-class sections
   and hard-case list (blur, feed exit, bat occlusion).

## Story fan-out (parallel worktree agents; disjoint files)

| Agent | Story | Owns (new files + their tests) |
|---|---|---|
| f1 | US-F1 labeling/datasets | `cricai_worker.jobs.sample_frames` (diversity strata), `cricai_data.labelio` (Label Studio JSON + YOLO txt round-trip), dataset freeze/digest + split assignment with **session-disjoint test split checker**, `routers/datasets.py`, `docs/labeling_guide.md` body |
| f2 | US-F2 training/registry | `cricai_vision.yolo_detect` (ultralytics adapter over stub), `cricai_vision.train` (trainer protocol + fake), `scripts/train_detector.py` (one-command train/eval), eval-report artifact writer, promotion service (candidate→staging→production, auto-block on >2-point headline regression), `routers/models.py` |
| f3 | US-F3 tracking | `cricai_vision.track` (Kalman + gap-bridging ≤5 frames, longer gaps flagged; pure NumPy over synthetic trajectories), `cricai_worker.jobs.track_balls` (detector→track→pitch coords via calibration services, identity-error guards vs decoy balls), `routers/tracks.py` |
| f4 | US-F4 auto bounce map | `cricai_vision.bounce_estimate` (trajectory→bounce vertex), `cricai_worker.jobs.estimate_bounces`, shared precedence service (BounceMark > auto), heatmap source/confidence extension (hollow low-confidence toggle) |
| f5 | US-F5 contact fusion | `cricai_coaching.contact_fusion` (track deviation + audio class + bat detection → contact_quality + bat_path with confidence; any-modality-missing degradation), reliability-diagram harness, confusion-matrix regression gate, ball_metrics writes (proxy=false) |
| f6 | US-F6 triangulation | `cricai_vision.triangulate` (stereo extrinsics pairing, DLT, reprojection QC vs synthetic stereo scenes), 3D quality report, honest-limits doc section (no RPM/seam-axis claims) |

Pinned cross-group contracts:
1. Track payload: object key `sessions/{sid}/balls/{n}/track-{camera}.json`, points
   `[{frame_no, ts_ms, px_x, px_y, score, bridged}]`, `segments`
   `[{kind: pre_bounce|post_bounce|post_contact, start_ms, end_ms, confidence}]`,
   `flags` `{identity_risk: bool, long_gap: bool, ...}` (f3 writes; f4/f5 read).
2. Bounce precedence: exactly one service resolves manual-vs-auto; auto never overwrites
   or deletes `bounce_marks`; reprocessing preserves manual overrides (US-F4 AC).
3. F5 metric values keep the `{value, unit, confidence, reason-if-null, proxy?}` shape,
   phase=`contact`; fusion outputs set `proxy=false` and name their inputs in provenance.
4. Headline metric names pinned for the promotion gate: `map50_ball`, `map50_bat`,
   `map50_stumps`, `recall_ball_high_blur` — eval reports and gates use these keys.
5. Enums from `cricai_data.enums`; timing invariant unchanged; only `FakeDetectionProvider`
   (or story-local synthetic data) in tests — never real YOLO/torch.

Same protocol as Phases 1–3: temp-stub + delete before commit, no `git stash`, 100%
line+branch, ruff/mypy strict, safety assertions never weakened, sequential merge in
dependency order (f1, f2, f3, f4, f5, f6), full gates, adversarial review workflow **plus
fix-verification audit** (Phase-3 lesson: verify regression tests against pre-fix code),
review-fix fan-out, PR to main.

## Definition of done

`make all` green; integration+safety+golden pass; adversarial review + fix-verification
findings fixed; PR merged. Deferred to real-footage milestones: all model-quality ACs (the
harness — frozen splits, eval reports, promotion gates, agreement audits — ships now).

## Deferred/backlog notes

- Phase 5 (Epic J): re-derive cascade so detector re-runs can refresh machine-derived
  dependents (clips/pose/metrics/tracks) instead of dead-ending on the block-beats-corrupt
  guard (decision recorded in phase-3-plan.md).
- Real-footage milestones: US-F1 label volume + agreement audit, US-F2 benchmark training,
  US-F3 bounce-MAE study, US-F4 first-month dual-run audit, US-F5 coach-label benchmark,
  US-F6 surveyed-rig test.
