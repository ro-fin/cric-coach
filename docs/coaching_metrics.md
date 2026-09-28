# Coaching Metric Definitions (US-E2/E3/I2 — coach-reviewed)

**Version:** 0.1 (skeleton — each metric added in its phase with formula, landmarks, frames)
**Coach sign-off:** ______ · Date: ______

Every metric entry must specify: name, unit, landmarks used, frame window,
formula, nullable-with-reason conditions, and validation study reference.

## Batting — pre-release (Phase 3)

Implemented by `cricai_coaching.pre_release.compute_pre_release(track, release_frame,
px_per_cm, config)` — pure deterministic functions over a C1 (side-on) pose track
(US-E1 `PoseTrack`, MediaPipe 33-landmark topology). All defaults below live in
`PreReleaseConfig` and are shown as `name=default`.

**Shared conventions**

- Frames: `release_frame` indexes the track frame at ball release. Times are
  milliseconds relative to release (`(frame - release_frame) * 1000 / fps`);
  negative = before release.
- Reference frame for stance/head sampling: `release_frame - reference_offset_frames`
  (`reference_offset_frames=0`, i.e. at release unless configured earlier).
- Sign convention: `head_offset` is positive toward the batter's OFF side. On the
  pinned C1 rig orientation, a right-handed batter's off side is toward increasing
  image x; the sign flips for a left-hander (`handedness="right"`). Image y grows
  downward; angles are reported "screen-up" positive.
- Confidence: from the visibilities of the landmarks actually used — minimum for
  single-frame measurements (stance, head offset, pickup endpoints), mean for window
  measurements (trigger run, stillness window). Null metrics carry `confidence 0.0`.
- Global gate (US-E2 AC, no silent zeros): if track availability (fraction of frames
  whose median landmark visibility >= `min_visibility=0.5`) is below
  `min_availability=0.8`, EVERY metric is null with reason
  `insufficient pose availability`. Individual landmarks below `min_visibility` null
  only the metrics that use them, with the specific reasons listed per metric.

**stance_width_px / stance_width_cm**

- Unit: px / cm. Landmarks: `left_ankle`, `right_ankle`. Frame: the reference frame.
- Formula: Euclidean image distance between the two ankle points;
  `stance_width_cm = stance_width_px / px_per_cm` (px->cm scale from calibration).
  `stance_width_px` is always reported alongside the cm value.
- Null when: reference frame precedes the track start (`reference frame before start
  of track`); either ankle below `min_visibility` (`ankle landmarks below visibility
  threshold`); no calibration scale — cm value only (`no calibration scale`).
- Validation: US-E2 staged-stills study (tape measure, ±3 cm target).

**head_offset_px / head_offset_cm**

- Unit: px / cm (signed, positive toward off side). Landmarks: `nose`, `left_ankle`,
  `right_ankle`. Frame: the reference frame.
- Formula: `raw = nose.x - (left_ankle.x + right_ankle.x) / 2`; signed per the
  handedness convention above; cm via `px_per_cm`. Measures head position over the
  base center (US-E2 "head over base").
- Null when: reference frame precedes the track (`reference frame before start of
  track`); nose or either ankle below `min_visibility` (`nose or ankle landmarks
  below visibility threshold`); no calibration scale — cm value only
  (`no calibration scale`).
- Validation: US-E2 plumb-line study (±3 cm target).

**trigger_start_ms**

- Unit: ms vs release (negative = before). Landmarks: `left_ankle`, `right_ankle`,
  `left_hip`, `right_hip`. Frames: every inter-frame interval ending at or before
  `release_frame`.
- Formula: an interval qualifies when the mean speed of the four landmarks is
  >= `trigger_speed_px_per_ms=0.05`; the onset is the END frame of the first interval
  in a run of `trigger_sustain_frames=3` consecutive qualifying intervals;
  `trigger_start_ms = (onset_frame - release_frame) * 1000 / fps`. Intervals with any
  of the four landmarks below `min_visibility` never qualify and reset the run
  (movement is never fabricated across low-visibility gaps).
- Null when: fewer than `trigger_sustain_frames` intervals before release
  (`insufficient frames before release`); every interval low-visibility (`ankle/hip
  landmarks below visibility threshold`); no sustained run found (`no sustained
  movement before release`).
- Validation: coach-labeled trigger onsets on real footage (deferred milestone).

**still_at_release / head_speed_at_release**

- Units: bool / px_per_ms. The measured head speed is reported as its own
  first-class metric (`head_speed_at_release`) under the pinned
  `{value, unit, confidence, reason-if-null}` contract — never as an extra key
  inside the boolean's value, which the ball-metrics API rejects. Landmark: `nose`.
  Frames: `stillness_window_frames=4` intervals ending at `release_frame`.
- Formula: `head_speed_at_release = mean(|nose(t) - nose(t-1)|) / (1000 / fps)` over
  the window; `still_at_release = head_speed_at_release < stillness_speed_px_per_ms=0.1`.
  Both carry the same confidence (mean nose visibility over the window).
- Null when: window starts before the track (`insufficient frames before release`);
  any nose sample in the window below `min_visibility` (`nose below visibility
  threshold`). `head_speed_at_release` is null, with the same reason, whenever the
  boolean is null.
- Validation: US-E2 coach-judgment agreement (>= 85% on 100 labeled balls, deferred).

**pickup_direction_deg**

- Unit: degrees from image +x, "screen-up" positive (90 = straight up), range
  (-180, 180]. Landmarks: `left_wrist`, `right_wrist`. Frames: the two endpoint frames
  of the `pickup_window_frames=6` window ending at `release_frame`.
- Formula: displacement `(dx, dy)` of the mid-wrist point (mean of both wrists)
  between the endpoints; `angle = atan2(-dy, dx)` in degrees.
- Null when: window starts before the track (`insufficient frames before release`);
  any wrist at either endpoint below `min_visibility` (`wrist landmarks below
  visibility threshold`); displacement magnitude < `pickup_min_displacement_px=2.0`
  (`no discernible pickup movement` — an angle from noise would be meaningless).
- Validation: constructed wrist-path unit tests now; bat-tracking cross-check when
  Epic F lands.

## Batting — contact & post-contact (Phase 3–4)

Implemented by `cricai_coaching.contact_metrics.compute_contact(track, contact_frame,
release_frame=0, px_per_cm=None, config=ContactConfig())`. All formulas run over the
US-E1 pose track only (pose-ONLY proxies until Epic F bat/ball tracking); every value
follows the `{value, unit, confidence, reason-if-null, proxy?}` contract. Geometry
conventions: image x grows right, image y grows DOWN; `toward_ball_sign` (-1 default)
maps image-x displacement onto "toward the ball line"; the front foot is the
`front_side` foot ("left" for a right-handed stance). Per-metric confidence is the
mean visibility of the landmarks/frames that metric used. Every metric goes
null-with-reason when track availability < `min_availability` (default 0.80) or any
used landmark's visibility < `min_visibility` (default 0.50) — never silent zeros.

### front_foot_direction_cm (unit: cm; px fallback: front_foot_direction_px)
- Landmarks: front ankle. Frames: stance baseline (first `baseline_frames`, default 3,
  clipped to the contact frame) + contact frame.
- Formula: `(x_ankle[contact] - mean(x_ankle[baseline])) * toward_ball_sign`, divided
  by `px_per_cm`. Positive = the front foot moved toward the ball's line.
- Null reasons: no `px_per_cm` scale (px value still reported), low ankle visibility.

### head_stability_score (unit: score in [0, 1])
- Landmarks: nose over the release->contact window; both shoulders at contact.
- Formula: `clamp(1 - mean|x_nose - median(x_nose)| / (0.5 * shoulder_width_px), 0, 1)`
  where the mean absolute deviation is taken over the release->contact window and the
  shoulder width at the contact frame normalizes for camera scale. 1.0 = the head held
  its line through the swing; 0.0 = lateral head drift of half a shoulder width or more.
- Null reasons: low nose/shoulder visibility; degenerate (zero) shoulder width.

### balance_at_contact (unit: class stable|falling_away; margin: balance_margin_px)
- Landmarks: nose + front ankle at the contact frame.
- Rule: `offset = (x_nose - x_front_ankle) * toward_ball_sign`;
  stable iff `offset >= -balance_threshold_px` (default 30 px, i.e. the head is over or
  ahead of the front ankle toward the ball, within threshold). The signed margin from
  the decision boundary (`offset + threshold`, >= 0 means stable) is reported so the
  UI can show "how close" (boundary margin 0 classifies as stable).

### contact_point_class (unit: class; **proxy: true** until Epic F — US-E3 AC)
- Landmarks: nose + both wrists at the contact frame. The wrist midpoint proxies the
  hands/contact vicinity until Epic F ball tracking: no ball position is measured, so
  the value always carries `proxy: true`, even when null.
- Rule table over `d = (x_wrist_mid - x_nose) * toward_ball_sign` (px ahead of eyes):

  | class            | condition (defaults)   |
  |------------------|------------------------|
  | late             | d <= -40               |
  | cramped          | -40 < d <= -10         |
  | under_eyes       | -10 < d <= 35          |
  | too_far_in_front | d > 35                 |

### bat_path_class (unit: class; **proxy: true** until Epic F — US-E3 AC)
- Landmarks: both wrists at the swing-window start (`bat_path_window_frames` ending at
  contact, clipped to the release frame) and at contact. This classifies the WRIST-path
  angle, not the bat: the value always carries `proxy: true`, even when null.
- Angle: `theta = atan2(dx_toward_ball, dy_down)` in degrees; 0 = straight down the
  vertical, 90 = flat toward the ball line.
- Rule table (defaults):

  | class    | condition           |
  |----------|---------------------|
  | straight | 25 <= theta <= 65   |
  | across   | 65 < theta <= 110   |
  | closed   | -10 <= theta < 25   |
  | open     | theta < -10 or theta > 110 |

- Null reasons: wrist travel < `bat_path_min_travel_px` (default 5 px; no swing to
  classify), low wrist visibility.

Deferred to Epic F (per phase-3 scope decision): bat-face angle, follow-through shape,
middled/edge/miss detection, and the auto `control` flag; `control` today is the manual
US-B4 tag aggregated by the metrics summary (fraction true with its denominator).
Coach-label validation studies (±5 cm front-foot agreement, head-stability AUC >= 0.8,
bat-path agreement >= 80%) await real-footage milestones.

## Contact quality & bat path fusion (US-F5, Phase 4)

Implemented by `cricai_coaching.contact_fusion` and written per ball by the
`cricai_worker.fuse_contacts` job as phase=`contact` keys **`contact_quality`** and
**`bat_path`** — both `proxy: false` (real ball-track / audio / bat-detection inputs,
unlike the US-E3 wrist stand-ins, which keep their own keys and survive every fusion
write: the job merges by key, never replaces the row). All weights, thresholds and
rule tables below live in `FusionConfig` (defaults shown); score tables are the
module's public `*_SCORES_*` constants.

### contact_quality (unit: class middle|edge|miss over `cricai_data.enums.Contact`)

Three independent modality votes, each a score distribution over the three classes
plus a modality confidence:

1. **track** (weight 0.5) — from the pinned US-F3 payload
   (`sessions/{sid}/balls/{n}/track-{camera}.json`, reference camera C1). No
   post-contact segment ⇒ miss-leaning (0.05/0.15/0.80) at confidence
   `no_post_contact_confidence=0.7`. With a post-contact segment, the direction
   change between the incoming and outgoing velocity (up to `deviation_points=3`
   track points each side of the bat plane — `contact_ms` when known, else the
   segment start) selects the table: `>= 35°` clean-hit (0.75/0.20/0.05),
   `>= 8°` deflection (0.20/0.70/0.10), below that faint (0.10/0.50/0.40).
   Both velocity windows use only **real points**: `bridged` points are the
   tracker's fabricated constant-velocity chord fills (US-F3) and never count
   as measurements — around an occluded contact they would read ~0° for a
   middled ball. And each window is **clamped to its own flight segment** so it
   never straddles a bounce: the incoming window to the latest
   non-`post_contact` segment starting at/before the bat plane (unbounded only
   when the payload carries no such segment), the outgoing window to the
   `post_contact` segment itself — with bridged fills excluded, an unbounded
   incoming window would backfill from pre-bounce descent points and score the
   bounce's own direction change as bat deviation (a faint edge read as a
   clean hit). Fewer than two real in-bounds points on either side, or a
   degenerate direction vector ⇒ deviation not computable ⇒ presence-only
   (0.45/0.35/0.20). Confidence is the segment's confidence, halved
   (`flagged_track_penalty=0.5`) when the payload flags `identity_risk` or
   `long_gap`.
2. **audio** (weight 0.3) — the US-D3 `RefinedContact` from
   `cricai_vision.audio_onset` (same public API and post-release window rule as the
   refinement job; a pre-release machine clank can never count as contact
   evidence). `bat_crack` ⇒ 0.60/0.35/0.05, `thud` ⇒ 0.10/0.20/0.70, `unknown` ⇒
   uniform. Confidence is the refined onset's confidence.
3. **bat** (weight 0.2) — bat/ball detection boxes within
   `contact_frame_tolerance_ms=25` of the contact instant. When the caller
   supplies the track payload's points (the fusion job always does), the
   modality is additionally **identity-gated**: without a real (non-bridged)
   tracked-ball point within the same tolerance of the contact instant, every
   ball box there is an unverified impostor (e.g. a static decoy ball in the
   net while the bat occludes the real ball), so the modality is **missing**
   rather than a confident far/miss vote against the wrong ball. Past the
   gate, at least one ball AND one bat box must lie in the window (else
   missing); the closest bat-ball pair's normalized box gap selects the table:
   overlapping ⇒ 0.55/0.35/0.10, within `bat_near_gap=0.05` ⇒ 0.30/0.45/0.25,
   farther ⇒ 0.05/0.15/0.80. Confidence is the weaker detection score of the
   pair.

Fusion: base weights are **renormalized over the modalities present**
(`eff_w = w / Σ w_present`), `combined[c] = Σ eff_w · scores[c]`, class = argmax
(ties resolve toward **edge** first — edge recall matters most — then middle, then
miss), and

`confidence = combined[top] × coverage × Σ eff_w · modality_confidence`, with
`coverage = Σ w_present / Σ w_all` — missing modalities always LOWER confidence.
Provenance `source` names the inputs, e.g. `contact-fusion-1.0.0(track+audio)`.

Degradation table (what each present/absent combination yields):

| track | audio | bat | result | coverage factor |
|-------|-------|-----|--------|-----------------|
| ✓ | ✓ | ✓ | full fusion | 1.00 |
| ✓ | ✓ | – | track+audio | 0.80 |
| ✓ | – | ✓ | track+bat | 0.70 |
| – | ✓ | ✓ | audio+bat | 0.50 |
| ✓ | – | – | track only | 0.50 |
| – | ✓ | – | audio only | 0.30 |
| – | – | ✓ | bat only | 0.20 |
| – | – | – | **null** with reason `all fusion modalities missing (...)` | 0 |

A malformed track payload degrades to a missing track modality (the ball still
fuses from audio/bat); balls with no track payload at all are skipped loudly in
the job summary, not written.

### bat_path (unit: class straight|across|inside_out)

From real bat boxes (best per frame) over the `swing_window_ms=300` before contact:
`theta = atan2(dx_toward_ball, dy_down)` of the first→last displacement
(normalized coords, `toward_ball_sign=-1` on the C1 rig).

| class      | condition           |
|------------|---------------------|
| straight   | 20 <= theta <= 60   |
| across     | theta > 60          |
| inside_out | theta < 20          |

Confidence is the mean detection score of the used boxes. Null-with-reason when:
no detection provider is configured; the ball has no contact time (no
`contact_ms` and no post-contact segment — e.g. a leave); fewer than
`min_bat_frames=2` bat frames in the window; or first→last travel <
`bat_path_min_travel=0.01` (a class is never fabricated from noise).

### Confidence calibration & regression gates (US-F5 AC)

- `reliability_diagram((confidence, correct) pairs, bins=10)` publishes per-bin
  observed accuracy vs stated confidence plus `calibration_gap` (count-weighted
  expected calibration error). Target: at reported >= 0.9 confidence, accuracy
  >= 95% — measured at the real-footage coach-label milestone.
- `confusion_gate(old_matrix, new_matrix)` FAILS whenever the edge→middled
  misclassification rate rises release-over-release (`tolerance=0.0` by default).
  A benchmark with no true edges scores 0.0 — keeping the deliberate edge-drill
  session in the benchmark is the MV study's job, not the gate's.

### Honest limits

- Edge detection without high-speed audio+video is **approximate**: at 30–60 fps a
  thin edge deflects the track by only a few pixels and its crack is often within
  one analysis window of the pad/pitch thud; audio alone cannot cleanly split edge
  from middle (both crack), which the audio score table reflects.
- The bat plane is approximated by the contact instant on the reference camera, not
  a measured 3D plane (until US-F6 triangulation).
- `contact_quality = miss` means "ball not hit" — fusion cannot distinguish a
  deliberate leave from a play-and-miss; that stays with the US-E4 decision layer.
- 3-class accuracy >= 85% vs coach labels and bat-path agreement >= 80% are
  real-footage milestone ACs; the harness (reliability diagram + confusion gate)
  ships now so the studies plug in without schema change.

## Ball flight & decision (US-E4, Phase 3–4)

Implemented by `cricai_coaching.decision.assemble_flight_metrics(tag, bounce_mark,
machine_speed_kph)`; V1 fills the schema from MANUAL sources and Epic F later swaps in
auto values under the SAME keys — every populated value carries `source` provenance so
the schema is identical either way.

| metric         | unit  | V1 source (provenance `manual`)                          | null reason when absent |
|----------------|-------|----------------------------------------------------------|-------------------------|
| speed_kph      | kph   | machine settings (tag's block first, else session)       | no machine speed recorded |
| line           | class | US-B4 tag, else the bounce mark's stored line class      | no tag/mark, or mark unclassified |
| length         | class | US-B4 tag, else the bounce mark's stored length class    | no tag/mark, or mark unclassified |
| bounce_xy      | m     | US-C5 bounce mark pitch-plane (x, y)                     | no bounce mark |
| footwork       | class | US-B4 tag                                                | no tag |
| shot           | class | US-B4 tag (granular label preserved)                     | no tag |
| decision_class | class | 8-way collapse of the tag's shot (`cricai_data.enums.decision_class`) | no tag |

When several cameras marked the same ball, the mark with the lowest camera_id is the
representative (the US-C6 heatmap dedupe rule).

### Decision-quality view (leaves vs chases outside off)
`cricai_coaching.decision.decision_quality(balls)` counts, for every ball whose line is
`outside_off`: a **leave** when the 8-way decision class is `leave`, a **chase** for any
other decision class (the batter played at a leaveable ball), grouped per length zone.
Outside-off balls missing shot or length context are reported `unclassified` — the
denominator is always visible, never silently shrunk. Served by
`GET /sessions/{id}/decision-quality`.

## Bowling — action checkpoints (Phase 6, US-I2/I3)

Release and action checkpoints for the leg-spin lab. Evaluated by
`cricai_coaching` at the release frame (`cricai_vision.release`, ±2 frames @
120fps) from bowler pose (`cricai_vision.bowler_pose`, C5/C6 zones). Every value
carries `source` provenance and is nullable-with-reason — populated into
`ball_metrics` (phase `pre_release`, bowling metric names, contract #6) and
surfaced on BallRecord v1.1 bowling fields. Geometric only: **no RPM, no
seam/spin axis** (SAF banned-claim lint gates every bowling-facing string).

The per-ball job (`cricai_worker.bowling_action.analyze_session_bowling_action`)
runs on BOWLING sessions only and writes the full key set below into the ball's
`pre_release` metrics row (merge-by-key — foreign keys survive). Thresholds and
band edges are **initial targets** pending the coach-labeled benchmarks named in
US-I2/I3; every boundary lives in `ReleaseConfig` / `CheckpointConfig`, so a
coach-approved change is configuration, not code.

### Bowler selection (`cricai_vision.bowler_pose`)
The tracked subject is the BOWLER, chosen by the same US-E1 geometry (zone
prior + track continuity + size, blended by landmark visibility) with the
bowling crease zone as the location prior: C5 (side-on, square of the bowling
crease) uses the central band `(0.30, 0.10, 0.75, 0.95)`; C6 (front-on, behind
the batting end) the pitch-line corridor `(0.35, 0.15, 0.65, 0.80)`
(normalized x0, y0, x1, y1; per-call overridable). When nobody plausibly is
the bowler — the front-on umpire-view occlusion case — selection returns the
no-selection path and every downstream checkpoint goes null-with-reason.

### Release detection (`release_frame`, `release_ms`, `hand_xy`; contract #6)
The release frame is the apex of the bowling wrist's over-shoulder arc: among
frames where wrist AND same-side shoulder are visible (>= 0.5), the wrist is
above the shoulder, and wrist speed clears the swing gate (0.5 px/ms — ~4
px/frame @ 120 fps), the frame with the highest wrist position (minimum image
y; earliest on ties) is release. The ±2 frames @ 120 fps agreement (US-I3 AC)
is verified on synthetic kinematics now and re-verified on the hand-labeled
benchmark. Null reasons, in precedence order: `empty pose track`;
`bowling wrist/shoulder below visibility threshold in every frame`;
`bowling wrist never above the shoulder`;
`no overhead frame reaches the arm-swing speed gate` — and when detection is
null, EVERY key below is written null with that same reason.
`release_ms` is session-timeline milliseconds (event window start + track
offset); `hand_xy` is the wrist image position (px) at release, persisted per
contract #6 as the release-point primitive for a FUTURE height × lateral
scatter. The shipped US-I3/US-I7 consistency chart is **height-only**
(`release_height_cm`); no consumer reads `hand_xy` yet — see "Release
consistency" below for the shipped definition and the recorded deferral.

### release_frame_offset (BallRecord `release_frame_offset`; unit: frames)
Detected release frame minus the ball event's nominal release frame
(`round((event.release_ms - event.start_ms) * fps / 1000)`), signed. The drift
the US-I3 benchmark scores; |offset| <= 2 @ 120 fps is the acceptance target.

### release_height_cm (BallRecord `release_height_cm`; unit: cm)
Release-point height above the pitch surface. Calibrated path first: when the
ball's triangulated 3D track exists (US-F6 stereo pair), the pitch-frame z at
the detected release time via `cricai_vision.triangulate.release_height`
(meters -> cm; `source="stereo"`). Fallback pose+scale path: (ground y − wrist
y) / `px_per_cm` at the release frame on C5, where the ground reference is the
lowest visible foot landmark (front foot planted at release;
`source="pose_scale"`). Null reasons: `no release frame detected`; the stereo
estimate's own reason when it misses and no scale exists; `no calibration
scale` (the US-E2 string) when neither seam is available; `foot landmarks
below visibility threshold at release`; `implausible release height (hand at
or below ground reference)` — a negative height is a geometry bug surfaced,
never clamped. The ±3 cm repeatability AC is a machine-metronome drill study
(deferred real-hardware milestone).

### brace_state (BallRecord `brace_state`; class `braced|bent|collapsed`)
Front-leg knee angle (hip–knee–ankle, degrees in [0, 180]) at the release
frame over `cricai_data.enums.BraceState`. The front leg is the bowling arm's
opposite leg (a right-arm bowler lands on the left foot). Bands (initial
targets for the >= 80% coach-agreement AC): `braced` >= 165°; `collapsed`
< 140°; `bent` between. Null reasons: `front-leg landmarks below visibility
threshold`; `degenerate front-leg geometry (coincident landmarks)`.

### falling_away_deg (BallRecord `falling_away_deg`; unit: deg)
Lateral trunk lean at release: the angle of the mid-hip -> mid-shoulder
segment from image vertical, evaluated on the front-on view. Positive = the
trunk leans toward the bowler's non-bowling-arm side, i.e. falling away from
the target line (for a right-arm bowler on the pinned C6 orientation that is
increasing image x; `CheckpointConfig.arm` flips the sign for left-arm). 0° is
perfectly upright; the falling-away FLAG threshold (AUC >= 0.8 AC) is set by
the coach-labeled study, the raw angle ships now. Null reasons: `shoulder/hip
landmarks below visibility threshold`; `degenerate trunk geometry (coincident
shoulders/hips)`.

### head_offset_at_release_px / head_offset_at_release_cm (US-I2 checkpoint)
Head alignment over the front foot at release — a bowling-specific single-frame
measure, NOT the batting `head_stability_score` window scale: nose x minus
front-foot-ankle x, positive toward the target
(`CheckpointConfig.target_toward_positive_x` flips a mirrored mount); 0 = head
stacked over the front foot. The cm twin divides by `px_per_cm` and is null
with `no calibration scale` when absent. `ball_metrics` keys only — no
BallRecord v1.1 field (additive candidate for a future MINOR). Null reason:
`nose or front-ankle landmarks below visibility threshold`.

### Cross-cutting null rules
A track whose landmark availability is below 0.8 nulls every checkpoint with
`insufficient pose availability` (the US-E2 convention). Confidence is the
minimum visibility of the landmarks actually used (single-frame measurements);
release-detection confidence blends visibility with an arm-speed factor. All
confidences are deterministic heuristics, not calibrated probabilities.

## Bowling — targets and flight (Phase 6, US-I4/I5)

Implemented by `cricai_vision.trajectory` (provenance `TRAJECTORY_VERSION =
"flight-geom-1"`): pure geometry over tracked flight points (the US-F3 track
payload, pitch-mapped via the US-C2 homography where available) in the
canonical pitch frame — origin at the striker's middle stump, +x toward the
bowler's end, **+y toward the off side for a right-hand batter**. Every value
is nullable-with-reason with a confidence in [0, 1] (deterministic heuristic,
not a calibrated probability) — no silent zeros. Thresholds live in
`FlightConfig` (defaults shown as `name=default`), so a coach-approved change
is configuration, not code. Aggregated into the US-I7 report blocks by
`cricai_coaching.bowling_report`.

### turn_cm (BallRecord `turn_cm`; unit: cm, signed)
Lateral deviation after pitching — geometry only, never a spin inference.
- Inputs: pitch-mapped flight points on BOTH sides of the resolved bounce
  time; travel direction is inferred from the data (a delivery's pitch_x
  decreases toward the striker).
- Formula: least-squares line fits of the ground path (pitch y vs x) over the
  mapped points before and after the bounce; both fits are evaluated at the
  same down-pitch distance `eval_x = bounce_x + direction *
  min(turn_eval_distance_m=2.0, post-bounce mapped reach)`;
  `turn_cm = (post_fit(eval_x) - pre_fit(eval_x)) * 100`.
- **Sign convention (pinned):** positive = deviation toward **+y**, the off
  side for a right-hand batter in the canonical frame — a right-arm leg-break
  to a right-hand batter reads positive, a googly negative. The frame never
  flips with batter handedness.
- Confidence: detected (non-bridged) fraction of the fitted points ×
  `min(1, post_reach / turn_eval_distance_m)`.
- Null when: track is not pitch-mapped (single-camera pixel-only track);
  fewer than `min_fit_points=2` mapped points on either side of the bounce;
  pre-bounce mapped reach < `min_pre_reach_m=1.0`; post-bounce mapped reach
  < `min_post_reach_m=0.5`.
- Validation: synthetic-geometry unit suite now; real-footage benchmark is a
  deferred milestone (honest-limits register below).

### apex_m (BallRecord `apex_m`; unit: m)
Greatest height between release and bounce.
- Formula: pixel gap between the flight's highest pre-bounce image point
  (minimum `px_y`) and the interpolated image position at the bounce time,
  scaled by a calibrated vertical meters-per-pixel factor
  (`vertical_scale_m_per_px`, side-on view).
- Confidence: detected fraction of the pre-bounce points.
- Null when: no calibrated vertical scale (pixels alone cannot measure
  height honestly); fewer than `min_apex_points=3` pre-bounce points; the
  apex is not bracketed by the tracked window (peak at the edge); the bounce
  time is outside the tracked window; the apex is not above the bounce point
  in the image (view unsuitable).
- Validation: synthetic-geometry unit suite now; accuracy against surveyed
  heights is a deferred real-hardware milestone.

### dip_flag (BallRecord `dip_flag`; boolean)
Late-descent steepening — a fit-residual test, scale-free (works on
pixel-only tracks).
- Formula: a quadratic reference arc is fit to the EARLY portion of the
  descent (apex to bounce, minus the trailing `late_fraction=0.25` of the
  points); the flag is set when the mean late residual below the arc exceeds
  `max(dip_floor_px=3.0, dip_sigma=3.0 × early-fit RMS)`. A plain arc-like
  descent never trips it.
- Confidence: detected fraction of the descent points.
- Null when: no tracked points before the bounce; descent shorter than
  `min_descent_points=8`; early segment shorter than `min_early_points=4`.

### target_hit (BallRecord `target_hit`; boolean) and miss distance
US-I4 target scoring, reusing the US-C5 zone vocabulary so it can never
disagree with the pitch map.
- Hit predicate: the bounce classifies (`cricai_vision.zones.classify`, the
  same config the bounce pipeline writes with) into the declared target's
  line AND length (`bowling_targets` row).
- Miss distance (`distance_m`, scatter payloads): Euclidean distance to the
  same half-open `[lo, hi)` zone bands the classifier uses — 0.0 exactly when
  the bounce classifies inside the zone; a bounce exactly on a zone's FAR
  edge belongs to the farther band and reads as an infinitesimal positive
  miss, never a contradictory "missed by 0.0 m" beside `hit=false`.
- Confidence: the bounce estimate's own confidence (manual bounce marks
  score 1.0 and beat auto estimates).
- Unscored (null-with-reason): no declared target for the delivery; no
  bounce estimate (full toss or tracking failure); bounce outside the
  classifiable pitch area (an honest miss distance is still reported where
  measurable).

### Accuracy-scorecard denominator rule (US-I4 AC)
`cricai_vision.trajectory.accuracy_scorecard` counts a delivery in a
target's `attempts` denominator ONLY when it was scored with a confident
bounce (`confidence >= min_confidence`, default
`DEFAULT_MIN_BOUNCE_CONFIDENCE = 0.5`). Every excluded delivery stays
visible — `low_confidence` / `unscored` per target, `untargeted` overall —
so the percentage can never quietly inflate, and `hit_rate` is null (never a
silent 0%) when there are no attempts. The report-side scorecard
(`cricai_coaching.bowling_report.accuracy_scorecard`) applies the same rule
at the BallRecord level: only deliveries with a non-null `target_hit` enter
any percentage, with `counted`/`total` always shown.

### Release consistency (US-I3 report block; V1 is height-only)
The shipped consistency measure is the release-height scatter
(`cricai_coaching.bowling_report.release_scatter`): per-ball
`release_height_cm` points plus mean and population 1σ (`sigma_cm`), overall
and per declared variation — a tighter σ means a more repeatable release
slot. σ over fewer than 2 balls is null (a 1-ball σ of 0.0 would fake
perfect consistency). The block's own note states the limit plainly:
**height-only for now**. The US-I3 backlog AC names height × lateral
scatter; the lateral half is an open deferral — `hand_xy` is persisted per
contract #6 as its primitive but has no shipped consumer yet.

### Learning-modules sign-off (US-I7)
The Warne/Saqlain module content
(`cricai_coaching.bowling_report.LEARNING_MODULES`) was authored in-repo and
has **not** been coach-reviewed: every module ships with
`approved_by: "pending_coach_review"`, and coach endorsement is disabled by
default — no surface may present the modules as coach-endorsed, and nothing
in the default pipeline (the US-J5 review gate defaults to `auto_publish`)
constitutes a sign-off. The `"coach"` value may only ever ship together with
a sign-off recorded here: Coach: ______ · Date: ______.

## Honest-limits register (US-I5, US-F6)
The system never claims: spin RPM, seam/spin axis — not measurable with our cameras.
Turn/flight/dip (BallRecord `turn_cm`, `apex_m`, `dip_flag`) are geometric
measurements from tracked trajectories (post-bounce lateral deviation, apex
height, descent steepening) — never inferred revolutions or axis; the full
definitions live in the "Bowling — targets and flight" section above.
