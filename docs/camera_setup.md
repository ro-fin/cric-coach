# Camera Setup — C1–C4 MVP layout (US-A1) + C5–C7 bowling capture (US-I1)

**Version:** 0.2 (C5–C7 bowling-side sections real as of Phase 6; physical
values recorded at install)
**Sign-off:** Parent: ______ · Coach: ______ · Date: ______

## Placement spec (from backlog US-A1 / US-I1)

| Camera | Role | Placement | Notes |
|---|---|---|---|
| C1 | Side-on batting (`batting_side`) | Square of the batter, 4–6 m outside net, chest height, ≥120 FPS | Stance, trigger, stride, head, swing plane, contact |
| C2 | Bowler's/machine end | Behind machine, aligned with stumps, behind rated protection | Never in ball path |
| C3 | Keeper's end | Behind batter looking down the pitch | Inside/outside-the-line view |
| C4 | Overhead/high-diagonal | 3.5–5 m high net corner/ceiling | Pitch map + session overview |
| C5 | Bowling side-on (`bowling_side`) | Square of the **bowling** crease, 4–6 m outside net, chest height, ≥120 FPS | Run-up, gather, brace, release height, follow-through (US-I2/I3) |
| C6 | Bowling front-on (`front_on`) | Behind the batting end, aligned with the stumps, looking back at the bowler | Alignment, front-arm pull, falling-away; expect umpire-view occlusions |
| C7 | Wrist close-up (`wrist`) | Zoomed tight on the bowling hand at the release window, **240 FPS**, behind rated protection (`protected: true`) | Wrist/hand position at release; placement tuned so the wrist is visible and in focus at release in ≥ 80% of deliveries *(initial target, placement study)* |
| C8 | Reserved (impact close-up) | Mount point + wiring reserved at install | Future |

## Per-camera config record schema

`camera_id, role, position_label, xyz_offset_m, height_m, fps, resolution, lens, mount, protected`
— stored in the camera registry (API), mirrored here after each install/change.

## Camera roles (US-I1)

Every registration may declare a `role` — what the camera looks at:
`batting_side | bowling_side | wrist | front_on | other`. Today a role is
registry metadata plus a listing filter (`GET /cameras?role=`) — **no
pipeline stage consumes cameras by role yet**. Stages select by camera id:
the bowling-action pipeline reads the release view from **C5** (its pinned
default), bowling-flight walks cameras in ascending id order, and clip
cutting follows the session's `expected_cameras` roster — so the bowling
side-on feed MUST be registered as C5, whatever its role says.
Role-driven stage consumption is a recorded future seam, not shipped
behavior. Position labels are human notes, never parsed. Rows registered
before Phase 6 have no role until re-registered (or role-patched via
`PATCH /cameras/{camera_id}`, which does not open a new placement era
because the camera has not moved).

C5–C8 are reserved by the mount plan: the registry accepts them but flags
`reserved_for_future` until the registration carries a role. Registering
C5/C6/C7 with their bowling roles makes them real — they then join session
rosters (`expected_cameras`), health checks, sync checks and per-ball clips
exactly like C1–C4.

## Bowling capture: health, sync and clips (US-I1)

- **Health checks** are per-camera and fleet-size-agnostic: a 7-camera run
  records `feed:C5 … exposure:C7` probes alongside the C1–C4 ones, plus the
  fleet-wide `disk` and `sync` verdicts.
- **FPS tiers**: the health-check `fps` probe takes one `target_fps` per run,
  so run the wrist tier as its own check — C7 alone with `target_fps: 240` —
  after the 120-FPS fleet run. A throttled C7 must fail loudly, not hide
  under the slower fleet target.
- **Sync budget** follows the highest-FPS camera in the run: one frame at
  240 FPS is ~4.2 ms, so the wrist-tier check tolerates far less clock skew
  than the 120-FPS fleet (~8.3 ms). Same shared LAN NTP clock as US-C3.
- **Clips**: bowling-mode ball events clip every camera in the session roster
  — C5–C7 ride the existing per-ball clip pipeline (US-D2) with no special
  casing; a missing bowling cam produces the same loud `GAP` clip rows as
  any other expected camera.

## Field-of-view reference images

Stored per camera under object storage `calibration/fov/{camera_id}/`; stumps and
both creases must be visible where required.

## Safety envelope (US-A5)

- C2 and any camera near the ball corridor sit behind rated polycarbonate/netting.
- Emergency-stop procedure for the bowling machine: documented at install; drill quarterly.
- Machine sessions require checklist acknowledgment before start (enforced by API).

## Rig-from-scratch drill (FT)

Target: any adult re-rigs the net to spec in under 30 minutes using only this doc.
Record: date, time-taken, FOV compliance vs reference images, deviations.

## Stereo pairs & 3D triangulation (US-F6)

### Camera pairs

| Pair | Views | What it measures |
|---|---|---|
| C1 + C4 | side-on + overhead/high-diagonal | true 3D flight, dip, release height (batting end) |
| C2 + C3 | bowler's end + keeper's end, facing each other down the pitch | down-the-pitch flight, line drift |

Pair geometry rules: the two cameras must NOT be near-coincident or
parallel-mounted — the software rejects baselines under 1 cm outright
(`StereoGeometryError`) and flags per-point ray angles under 0.5° as
`low_parallax`. The wider the angular separation at the ball corridor, the
better the depth accuracy; both MVP pairs give 15°+ at mid-pitch.

### Sync requirement

Track points are matched across the pair by `frame_no` on the shared video
timeline. Matched frames whose timestamps disagree by more than **10 ms**
(`MAX_SYNC_SKEW_MS`) are dropped from 3D and counted in the quality report —
never interpolated. Gap-bridged 2D points are likewise excluded from 3D.
Cameras must therefore record on a common clock (LAN NTP + the US-C3 drift
monitor); re-check drift before any triangulated session.

### Stereo calibration record

One `Calibration` row with `kind=stereo` per pair (stored under the pair's
first camera id). `params` carries `{camera_pair, per-camera intrinsics,
relative R/t, camera A's pitch-frame pose, frame contract}` — the exact shape
is documented in the `cricai_vision.triangulate` module docstring. Triangulated
coordinates come out in the canonical pitch frame (origin = striker's
middle-stump base, +x toward the bowler, +y off side for a right-hand batter,
+z up, meters), so release height is read directly as `z`.

### Surveyed-target rig test (deferred field test, US-F6 AC)

Run at install and after any camera move, once real hardware is in place:

1. Place >= 12 targets (taped crosses or balls on stands) through the flight
   corridor: spread across x = 2–18 m down the pitch, y across the pitch
   width, z from 0.2 m up to 2.5 m (release height).
2. Tape-measure each target's pitch-frame position (x, y, z) from the
   striker's middle-stump base; record in the session log.
3. Capture simultaneous stills on both cameras of the pair; click each target
   on both images.
4. Triangulate the clicked pairs and compare against the surveyed positions.
   **Pass: RMS 3D error <= 5 cm across all targets** *(initial target)*.
5. Repeatability: fire >= 10 identical bowling-machine feeds, triangulate each,
   and check release height spread. **Pass: within +/- 3 cm** *(initial
   target)*.

### Honest limits

This system does **not** claim spin RPM or seam/spin-axis measurements —
those are not measurable with our cameras and would require specialist
hardware (high-speed marked-ball or radar systems). 3D confidence values in
quality reports are deterministic heuristics (monotone in reprojection
quality and point count), not calibrated probabilities.
