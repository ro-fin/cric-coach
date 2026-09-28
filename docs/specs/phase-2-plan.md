# Phase 2 Plan — Calibration & Pitch Mapping (Epic C)

**Stories:** US-C1–C6 · **Branch:** `phase-2-calibration` · **Package:** `packages/vision`
(+ API routers `calibration.py`, `bounce.py`, `heatmap.py`)

## Coordinate frame contract (US-C2, fixed here for the whole system)

Origin = middle-stump base at the **striker's** end; +x toward bowler (down the pitch),
+y toward off side for a RH batter (mirrored presentation for LH); units meters.
Pitch: 20.12 m × 3.05 m; crease geometry per MCC law (popping crease 1.22 m in front of
stumps line, return creases 1.32 m either side of middle stump).

## Design

1. **`cricai_vision.geometry`**: pitch constants, landmark catalog (named points:
   stump bases, crease intersections), synthetic pinhole camera renderer for tests
   (project known 3D pitch points through K,R,t → pixels).
2. **`cricai_vision.intrinsics` (US-C1)**: ChArUco calibration via OpenCV
   (`cv2.aruco`), producing IntrinsicsResult{camera_matrix, dist_coeffs,
   reprojection_error, board_spec, n_views}; loud InsufficientCoverageError when
   < N valid views/corners; JSON round-trip with version + capture date.
   Tests: synthetic rendered board sweeps (known K/dist → recovered within tol);
   failure paths. `scripts/calibrate_cameras.py` CLI (thin wrapper).
3. **`cricai_vision.extrinsics` (US-C2)**: homography from ≥6 clicked landmarks
   (undistorted px ↔ pitch-plane xy); `pixel_to_pitch_xy()`, `pitch_xy_to_pixel()`,
   RMS on held-out landmarks, `draw_pitch_map()` overlay points; calibration JSON
   per camera-era. Tests: synthetic pinhole scenes exact; noise tolerance; contract
   tests of the coordinate frame (documented signs).
4. **`cricai_vision.zones` (US-C5)**: config-driven zone map — length bands measured
   in meters of bounce distance from the striker's stumps (initial defaults, junior
   pitch tunable: yorker ≤ 2.0, full 2.0–5.0, good 5.0–8.0, short > 8.0) and line
   channels in meters of lateral offset (outside_off/off/middle/leg for RH, mirrored
   for LH); classify(x, y, handedness) → (line, length); boundaries from a
   `ZoneConfig` dataclass loaded from a YAML-able dict, **never hardcoded**;
   reclassification without re-clicking (raw xy stored).
5. **Calibration session state (US-C3/C4)**: `cricai_data` additions —
   `calibrations` table (camera_id, era_no, kind intrinsic|extrinsic, params JSON,
   rms, created_at, valid) + session link `calibration_id`; drift check module
   `cricai_vision.drift`: landmark re-detection shift statistic, threshold flag →
   session `calibration_suspect` flag (needs schema: add boolean/flag column via
   new Alembic migration in this phase's foundation step).
6. **API routers**: `calibration.py` (upload landmark clicks → homography + RMS;
   attach calibration to session; drift report; block `analyzed` without calibration),
   `bounce.py` (US-C5: click → store raw px + camera + frame + computed xy + derived
   zones; cross-camera agreement flag > 15 cm), `heatmap.py` (US-C6: aggregation
   per zone: balls, control %, false-shot %; filters block/bowler/shot/contact/control;
   PNG export via matplotlib Agg).
7. **US-C4 exclusion contract**: `calibration_suspect` sessions excluded from trend
   queries — enforced in heatmap/aggregation layer with tests.

## Execution

Foundation (schema migration + geometry + zones config) inline → fan-out agents:
c1 intrinsics, c2 extrinsics(+geometry tests), c3/c4 calibration state+drift(+router),
c5 bounce router, c6 heatmap — same worktree protocol as Phase 1. opencv-python-headless
and matplotlib become `cricai-vision` deps. FT runbooks: tape-measure test, bump test.
