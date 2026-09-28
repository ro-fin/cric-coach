# Labeling Guide — Ball / Bat / Stumps / Feet (US-F1)

> Status: per-class rules filled by the f1 story; the hard-case gallery grows
> with every labeling batch. This guide is versioned with the code: label
> decisions changed here must reference the dataset version they first apply
> to.

Datasets train on **our** net, lighting, and ball. Boxes use normalized YOLO
`cx cy w h` in [0, 1]. Every frame carries provenance (`session`, `ball_no`,
`camera`, `frame_no`) — never label a frame that lost its provenance row (the
importer rejects such files outright).

## Classes

### ball
- Tight box around the visible ball, including motion blur streak.
- **Blur-streak rule:** box the **full streak** — from the trailing edge to the
  leading edge of the smear — not just the leading crescent. Rationale: the
  tracker (US-F3) estimates ball position as the box center, and a
  leading-edge-only box systematically biases the center forward along the
  flight path; the full streak keeps the center on the mean position for that
  exposure. Examples:
  - 120 fps side-on, fast ball: streak ≈ 2–4 ball diameters — one box over the
    whole smear, tag `blur`.
  - Faint streak barely brighter than the net: still box it if you can trace
    both ends; if you cannot tell either end, do not label the frame's ball
    (no box beats a guessed box) and tag the frame `blur`.
  - Streak crossing the stumps: box the streak only; never merge with the
    stumps box.

### bat
- Full blade + handle when visible; exclude gloves (separate class).

### stumps
- One box around the full set (not per stump).

### feet
- One box per visible foot, ankle down.

### glove / helmet (optional extras)
- Label only in sessions flagged for the extended-class experiment.

## Hard cases (label anyway, note the case tag)

| Case | Rule |
|---|---|
| Ball blurred | Box the full streak (see blur-streak rule above); tag `blur`. |
| Ball at feed exit | Box even when partially inside the machine mouth; tag `feed_exit`. |
| Bat occluding ball | Box the visible sliver only; if fully hidden, no box (the tracker bridges). |
| Second ball lying in net | Always label it too — the tracker's identity tests depend on decoys being labeled. |
| Keeper's gloves near ball | Never extend the ball box to include glove pixels. |

## Tooling round trip (US-F1)

- Frames are sampled by the `cricai_worker.sample_frames` job with diversity
  strata (lighting / speed band / block intent) recorded on every row.
- Export the labeling tasks with
  `uv run scripts/export_label_tasks.py --session-id <id> --out tasks.json`
  (`--image-url-prefix` maps object keys to your Label Studio local-files
  serving root, e.g. `/data/local-files/?d=`). Each task embeds the frame's
  provenance under `data.cricai` — the importer requires it — and each
  pre-existing box rides along as a pre-label with its exact normalized
  coordinates under `meta.norm`.
- Label in Label Studio, then import the tool's JSON export back with
  `POST /annotations/import` (parent/coach token): each task's boxes replace
  its frame's stored labels wholesale. Untouched boxes round-trip losslessly;
  human-edited boxes take the tool's geometry. Any malformed task, unknown
  class, provenance mismatch (including `ts_ms`/`ball_no` — stale files never
  land on re-extracted pixels), or frame pinned by a frozen dataset rejects
  the whole file and nothing persists — fix the file, never hand-patch the
  DB.
- Training exports use `scripts/export_dataset.py` (YOLO txt via
  `cricai_data.labelio.to_yolo`; class indices are pinned in
  `YOLO_CLASS_ORDER`, line 0 is always `ball`).

## Split discipline (US-F1 AC)

- The **test split is session-disjoint** from train/val: all frames of a session
  live in exactly one side. The freeze-time checker enforces this; do not
  hand-edit splits.
- Dataset versions are immutable once frozen: freezing pins a SHA-256
  `manifest_digest` over the canonical membership and every later mutation is
  refused. Fixes go into a new version; `export_dataset.py` re-verifies the
  digest and refuses unfrozen versions.

## Agreement audit (process loop)

1. Every audit round double-labels 200 frames drawn across strata; ball-box
   mean IoU target ≥ 0.8.
2. Disagreements are triaged in review: each one either (a) becomes a new
   hard-case row in this guide, or (b) reveals a labeling error to re-do.
3. Guide updates land in the same change as the next dataset version they
   apply to, so "which rules produced these labels" is always answerable from
   the version alone.
