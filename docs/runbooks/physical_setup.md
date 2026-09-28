# Physical Setup Manual — from bare net to running AI cricket lab

**Audience:** the Parent (no developer help needed). **Starting point:** a netted
pitch with stumps, bats and balls — nothing else. **End point:** a 4-camera
(later 7-camera) lab capturing every ball, the full software stack analysing it,
and the day's report on a tablet at the net.

Companion documents (read in this order as you reach each stage):

| Doc | What it covers |
|---|---|
| `docs/procurement_guide.md` | What to buy — researched picks, India pricing, tiers |
| `docs/camera_setup.md` | The authoritative camera placement spec (this manual walks it) |
| `docs/runbooks/deploy_lan.md` | Software installation on the lab box (§5 owns cron) |
| `docs/uat_scripts.md` | First-session walkthroughs incl. exact curl bodies |
| `docs/release_checklist.md` | The hardware benchmarks (R1–R6) that close the release |
| `docs/safety_workload.md` | The child-safety policy the whole system enforces |

**Build order at a glance** — each stage is independently useful; stop points
are fine:

1. Stage 0 — plan + procure (V1 kit first: 4 cameras, machine, server)
2. Stage 1 — site power + network
3. Stage 2 — lab box + software deploy
4. Stage 3 — cameras C1–C4 installed, protected, registered
5. Stage 4 — lighting (only if you play evenings) + capture quality tests
6. Stage 5 — calibration day
7. Stage 6 — first real session end-to-end
8. Stage 7 — benchmarks R1–R6 + weekly operations
9. Stage 8 — leg-spin expansion (C5–C7) when V3 begins

---

## Stage 0 — Plan and procure

1. Read `docs/procurement_guide.md`. Buy the **V1 kit** first (cameras C1–C4,
   bowling machine, lab box, network, calibration kit). C5–C7 (leg-spin) and
   evening lighting can wait — the software runs 4-camera sessions natively
   and treats absent cameras honestly. Honor the guide's **purchase gates**,
   especially gate #1: buy ONE camera body first and verify manual shutter
   (≥1/1000 s) holds at both 120 and 240 fps before ordering the fleet.
2. Print the ChArUco board (spec + print instructions in the procurement
   guide) and mount it on rigid board. Verify flatness against a straightedge.
3. Decide the lab box location: within cable reach of the net area, dry,
   ventilated, mains + UPS. The upload workflow is SD-card sneakernet, so the
   box does NOT need to be at the pitch — but the tablet needs WiFi there.

## Stage 1 — Site power and network

> Everything outdoors is RCD/RCCB-protected and IP-rated. If in any doubt,
> the outdoor circuit is an electrician job — it is a line item in the
> procurement guide.

1. **Power**: one RCD-protected outdoor point near the bowler's end (machine +
   charging), one near the batting end (lights later, charging). Weatherproof
   covers closed whenever sockets are unattended.
2. **Network**: lab box wired to your router/switch. If the pitch has no WiFi
   coverage, install the outdoor AP from the procurement guide so the tablet
   works at the net. Give the lab box a stable LAN name (`lab.local` or a
   router DHCP reservation) — the deploy runbook depends on it.
3. **Time**: the lab box will serve NTP to itself (chrony, configured during
   Stage 2); cameras get their timebase from the sync-flash procedure, not
   from NTP, so no camera network config is needed.

## Stage 2 — Lab box and software

1. Install Ubuntu 24.04 LTS on the lab box. Apply updates. Install the NVIDIA
   driver (`ubuntu-drivers install`) and reboot; verify `nvidia-smi` shows the
   GPU.
2. Follow **`docs/runbooks/deploy_lan.md`** end-to-end (prerequisites → env
   file → process path → smoke). Finish with:

   ```sh
   uv run scripts/verify_deploy.py --parent-token-file /path/to/token --check-worker
   ```

   All legs must PASS. This is UAT-PA1 — record the date; it is the first
   live run of the deploy path.
3. Set up cron per `deploy_lan.md` §5 (nightly, weekly, review-sweep) and the
   backup drive per `docs/runbooks/backup_restore.md`.
4. From the tablet on the pitch WiFi, open `http://lab.local:3000` — the
   dashboard must load. Bookmark it.

## Stage 3 — Cameras C1–C4

Placement is specified in `docs/camera_setup.md`; this is the build sequence.
Work one camera at a time: mount → aim → protect → record test → register.

1. **Mount the hardware** (poles/clamps from the procurement guide):
   - **C1** side-on, square of the *batter*, 4–6 m outside the net, lens at
     chest height of the batter's stance (~1.2–1.4 m for an 11–12-year-old).
   - **C2** behind the bowling machine, aligned with the stumps line, BEHIND
     its polycarbonate shield. Never in the ball corridor.
   - **C3** behind the batter (keeper's end), looking straight down the pitch,
     high enough to see over the stumps (~1.8–2.2 m works well).
   - **C4** overhead/high-diagonal: net-frame corner or pole at 3.5–5 m,
     looking down the pitch; both creases and the full pitch must be in frame.
   - Safety-tether every camera to the net frame independently of its mount.
2. **Configure each camera identically**: 1920×1080 @ **120 fps**, shutter
   locked at **1/1000 s or faster** (per-model steps in the procurement
   guide), fixed white balance, timestamps on, one formatted V30+ SD card
   each. Disable auto-sleep; confirm ≥60 min recording (USB power if needed).
3. **FOV reference images**: photograph each camera's exact view. C3/C4 must
   show stumps + both creases. Upload them to the object store under
   `calibration/fov/{camera_id}/` (see `docs/camera_setup.md`) and keep copies.
4. **Register each camera** via the API or dashboard — the exact curl shape is
   in `docs/uat_scripts.md` (camera registration). Record real measured
   values: `xyz_offset_m` (tape-measure from the striker's middle-stump base:
   +x toward bowler, +y off side for a RH batter, +z up), `height_m`, `fps`,
   `resolution`, `role`. Fill the install table in `docs/camera_setup.md` and
   get both sign-offs.
5. **Ball-strike safety pass** (US-A1 FT): 30 machine balls at maximum speed.
   No unprotected camera may be touchable by any ball path; C2's shield takes
   hits without camera movement. Re-aim and re-photograph FOV if anything
   shifted.

## Stage 4 — Lighting and capture quality

Daytime sessions need no lighting — skip to the tests below. For evening play,
install the flicker-free floods from the procurement guide (batting crease,
bowling crease, corridor), power them from the RCD circuit, then run the same
tests under lights.

Run the three US-A2 field tests (they are the R6 benchmark's checklist):

1. **Sync-flash test**: start all cameras recording, fire the LED flasher
   once visible to every lens, stop. On the lab box, find the flash frame in
   each file; offsets must be ≤1 frame at 120 fps. Re-run whenever any camera
   is power-cycled — and do the flash at the START of every real session (it
   is how uploads get aligned).
2. **Flicker test**: record 10 s under final lighting; scrub frame-by-frame
   for banding. Any visible banding at 120 fps fails — replace or rewire that
   fixture (mains-flicker LEDs band at 100 Hz).
3. **Blur test**: machine at max speed, freeze-frame the ball mid-corridor;
   smear must be ≤2 ball diameters. If longer, shutter is too slow — raise
   shutter (and add light rather than ISO if the image gets dark).

## Stage 5 — Calibration day (~1 hour once; ritual is ≤2 min thereafter)

1. **Survey the pitch frame**: with the laser measurer + steel tape, verify
   pitch dimensions (20.12 m stumps-to-stumps, 3.05 m width, crease geometry)
   and mark 10+ landmark points (stump bases, popping/return crease
   intersections, pitch-edge midpoints). Record distances in the session log —
   these are the extrinsics ground truth.
2. **Intrinsics (US-C1)**: per camera, record a 60–90 s board sweep — the
   ChArUco board moved slowly through the field of view: near/far, corners,
   tilted. Run `uv run scripts/calibrate_cameras.py` per its `--help`; mean
   reprojection error must be ≤1.0 px. Verify undistortion straightens the
   crease/net poles in the verification image.
3. **Extrinsics (US-C2)**: no dashboard UI ships for this — it is an API
   flow. Per camera (C3/C4 mandatory — they drive the pitch map): open a
   calibration-ritual frame, read the pixel coordinates of ≥6 surveyed
   landmarks (any image viewer shows px under the cursor), and POST them as
   `observed_landmarks` (named catalog landmarks + their `px`) to the
   calibration endpoint (`routers/calibration.py`; landmark names are the
   documented catalog — stump bases, crease intersections). The API fits the
   homography and reports held-out RMS; it must pass (≤3 cm at the crease,
   ≤10 cm far-half). Check the drawn crease overlay hugs the real creases.
4. **Stereo pairs (US-F6, needed for release heights)**: calibrate C1+C4 and
   C2+C3 per `docs/camera_setup.md` §Stereo. Then run the **surveyed-target
   rig test** exactly as written there (≥12 taped-cross/ball targets,
   tape-measured, clicked on both cameras) — this is benchmark **R3**'s field
   half. Note R3 also has a code pre-step (see the release checklist).
5. **The per-session ritual (US-C3)**: before every session — 30 s clip of
   the board at striker's crease, popping crease, good length, release area.
   The system blocks analysis without it and auto-detects camera drift (>5 px
   median shift prompts recalibration). Budget: ≤2 minutes.

## Stage 6 — First real session, end to end

Follow `docs/uat_scripts.md` (Parent script) verbatim. In outline:

1. Complete the safety checklist in the dashboard (machine session — e-stop
   walk-through included; the API refuses to start without the acknowledgment).
2. Create + start the session (player, type, `bowler_source=machine`,
   machine settings). Record the sync flash. Record the calibration ritual.
3. Play the session (start with a 30–60 ball block).
4. Stop cameras; carry SD cards to the lab box; upload every file via the
   multipart flow (curl bodies in the UAT doc — or the dashboard's upload
   surface); stop the session.
5. Trigger the pipeline (`POST /pipeline/sessions/{id}/runs`); watch stage
   progress; when it completes, open the daily report on the tablet, review
   it as the coach, publish it.
6. Verify honesty end-to-end: every number in the report should trace to
   balls you can click through to clips.

## Stage 7 — Benchmarks and weekly operation

1. Work through the **hardware register** (`docs/release_checklist.md` §3,
   R1–R6): sync/capture quality (R6, already done in Stage 4), then over the
   first weeks of real footage: release-timing (R1), bounce MAE dual-run
   (R2), surveyed rig (R3), the 4-hour SLO on a 500-ball session (R4), and
   the 7-day soak (R5). Each row names its exact protocol.
2. Weekly rhythm (mostly automated by cron): tag the weekly 10-ball
   ground-truth sample the drift monitor posts, clear alerts, check the
   review queue, verify the backup drill monthly
   (`docs/runbooks/backup_restore.md`).

## Stage 8 — Leg-spin expansion (V3)

When ready for Epic I: buy the C5–C7 kit (procurement guide, phase-2 list),
install per `docs/camera_setup.md` (C5 mirrors C1 at the *bowling* crease;
C6 behind the batting end; C7 the 240 fps wrist view behind protection),
register them with their roles, re-run Stage 4's tests including the separate
240 fps wrist-tier health check, and extend the calibration ritual to the
release area. Note the R3 code pre-step gates release-height numbers.

---

## Standing safety rules

- The bowling machine's **emergency stop** is tested quarterly and everyone
  present can reach and operate it (US-A5; drill documented at install).
- No one enters the ball corridor while the machine is loaded.
- Machine sessions never start without the checklist acknowledgment — the
  software enforces this; do not work around it.
- The workload/pain rules in `docs/safety_workload.md` are the system's
  hard limits — the AI cannot talk around them, and neither should adults.
