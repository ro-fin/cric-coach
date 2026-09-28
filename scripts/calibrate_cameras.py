#!/usr/bin/env python3
"""Calibrate per-camera intrinsics from ChArUco board captures (US-C1).

Usage:
    uv run scripts/calibrate_cameras.py --images DIR --captured-on 2026-07-07 --out cam.json
    uv run scripts/calibrate_cameras.py --video sweep.mp4 --every 10 \
        --captured-on 2026-07-07 --out cam.json

Thin wrapper: parses args, loads frames via cv2, delegates all calibration
logic to ``cricai_vision.intrinsics``, writes the params JSON. Exits 2 with
the error message when board coverage is insufficient.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
from cricai_vision.intrinsics import (
    BoardSpec,
    InsufficientCoverageError,
    calibrate_from_images,
    to_params,
)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def _load_image_dir(directory: Path) -> list[cv2.typing.MatLike]:
    frames: list[cv2.typing.MatLike] = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        image = cv2.imread(str(path))
        if image is None:
            print(f"warning: could not read {path}, skipping", file=sys.stderr)
            continue
        frames.append(image)
    return frames


def _load_video(path: Path, every: int) -> list[cv2.typing.MatLike]:
    capture = cv2.VideoCapture(str(path))
    frames: list[cv2.typing.MatLike] = []
    index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if index % every == 0:
                frames.append(frame)
            index += 1
    finally:
        capture.release()
    return frames


def _build_parser() -> argparse.ArgumentParser:
    defaults = BoardSpec()
    parser = argparse.ArgumentParser(
        description="Calibrate per-camera intrinsics from ChArUco board captures (US-C1)."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--images", type=Path, help="directory of .png/.jpg/.jpeg board views")
    source.add_argument("--video", type=Path, help="board sweep clip, sampled every Nth frame")
    parser.add_argument("--every", type=int, default=10, help="video frame sampling stride")
    parser.add_argument("--squares-x", type=int, default=defaults.squares_x)
    parser.add_argument("--squares-y", type=int, default=defaults.squares_y)
    parser.add_argument("--square-len-m", type=float, default=defaults.square_len_m)
    parser.add_argument("--marker-len-m", type=float, default=defaults.marker_len_m)
    parser.add_argument("--aruco-dict", default=defaults.aruco_dict)
    parser.add_argument("--min-views", type=int, default=8)
    parser.add_argument(
        "--captured-on",
        required=True,
        help="ISO date the views were captured (provenance; deliberately no default)",
    )
    parser.add_argument("--out", type=Path, required=True, help="output params JSON path")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.every < 1:
        parser.error(f"--every must be >= 1, got {args.every}")

    board = BoardSpec(
        squares_x=args.squares_x,
        squares_y=args.squares_y,
        square_len_m=args.square_len_m,
        marker_len_m=args.marker_len_m,
        aruco_dict=args.aruco_dict,
    )
    if args.images is not None:
        frames = _load_image_dir(args.images)
    else:
        frames = _load_video(args.video, args.every)
    try:
        result = calibrate_from_images(
            frames, board, min_views=args.min_views, captured_on=args.captured_on
        )
    except InsufficientCoverageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.out.write_text(json.dumps(to_params(result), indent=2) + "\n")
    print(
        f"wrote {args.out} ({result.n_views} views, "
        f"reprojection error {result.reprojection_error_px:.3f} px)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
