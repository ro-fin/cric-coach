# Field-Test Runbooks (FT) & Real-Footage Model Validation (MV)

These procedures require the physical lab (net, cameras, bowling machine) and are
executed by the Parent/Coach; each runbook defines a result-recording schema so
outcomes land in the system as structured data.

Planned runbooks (added in their phases):
- `sync_flash_test.md` — LED-flash cross-camera sync ≤ 1 frame @120 FPS (US-A2)
- `blur_flicker_test.md` — shutter smear ≤ 2 ball-diameters; no flicker banding (US-A2)
- `ball_strike_safety.md` — 30 max-speed balls; protected cameras undamaged (US-A1/A5)
- `rig_from_scratch.md` — re-rig in < 30 min from docs (US-A1)
- `tape_measure_calibration.md` — 10 marked pitch positions vs mapping error (US-C2)
- `camera_bump_drift.md` — controlled bump detection 10/10 (US-C4)
- `surveyed_targets_3d.md` — triangulation RMS ≤ 5 cm (US-F6)
- `real_footage_mv_gates.md` — model-quality gates (T3) once real labeled footage exists
