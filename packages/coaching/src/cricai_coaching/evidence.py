"""US-G6: evidence-first clip linking — the uniform evidence object.

Every finding, drill rationale and trend point links to concrete ball clips
through one shape (pinned cross-group contract #2):

    ``{ball_no -> {camera_id -> clip_id}}``

Ball numbers are serialized as strings so the object round-trips JSON
unchanged. This module owns:

- :func:`build_evidence` — assemble the object from clip rows/dicts the
  caller injects (only ``CUT`` clips count as evidence);
- the >=2-clips-per-finding rule (:func:`check_min_clips`): 100% of findings
  link at least :data:`MIN_CLIPS_PER_FINDING` clips or state why fewer exist;
- the link-integrity crawler (:func:`crawl_links` and friends): every
  referenced ``clip_id`` must exist and be ``CUT`` — dead links are a build
  failure (US-G6 "dead links = build failure").

The module never touches the database: clip rows and clip-status lookups are
injected by the caller, keeping the rules pure and unit-testable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cricai_data.enums import ClipStatus

#: Evidence object shape (contract #2): ball_no (str) -> camera_id -> clip_id.
EvidenceMap = dict[str, dict[str, str]]

#: US-G6 acceptance: findings link >= 2 clips (or state why fewer exist).
MIN_CLIPS_PER_FINDING = 2

#: Injected clip-status source: mapping or callable ``clip_id -> status | None``
#: (``None`` = clip does not exist). Callers build it from DB rows.
ClipStatusLookup = Mapping[str, str] | Callable[[str], str | None]


def _field(row: Any, name: str) -> Any:
    """Read ``name`` from a mapping or an attribute-bearing row (Clip ORM row)."""
    if isinstance(row, Mapping):
        return row[name]
    return getattr(row, name)


def _clip_id(row: Any) -> str:
    """Clip identity: mappings say ``clip_id``, ORM rows say ``id``."""
    if isinstance(row, Mapping):
        return str(row["clip_id"])
    return str(row.id)


def build_evidence(clips: Iterable[Any]) -> EvidenceMap:
    """Assemble the contract-#2 evidence object from injected clip rows.

    ``clips`` are mappings (``ball_no``/``camera_id``/``clip_id``/``status``)
    or ORM-like rows (same attributes, ``id`` for the clip id). Only ``CUT``
    clips are evidence: PENDING/FAILED/GAP rows prove nothing playable exists.
    """
    evidence: EvidenceMap = {}
    for row in clips:
        status = str(_field(row, "status"))
        if status != ClipStatus.CUT.value:
            continue
        ball_key = str(_field(row, "ball_no"))
        camera_id = str(_field(row, "camera_id"))
        evidence.setdefault(ball_key, {})[camera_id] = _clip_id(row)
    return evidence


def clip_ids(evidence: Mapping[str, Mapping[str, str]]) -> list[str]:
    """Every clip id referenced by an evidence object, in deterministic order."""
    return [
        str(clip_id)
        for ball_key in sorted(evidence, key=str)
        for camera_id in sorted(evidence[ball_key])
        for clip_id in (evidence[ball_key][camera_id],)
    ]


def clip_count(evidence: Mapping[str, Mapping[str, str]]) -> int:
    """Distinct clips linked by an evidence object."""
    return len(set(clip_ids(evidence)))


@dataclass(frozen=True)
class EvidenceCheck:
    """Outcome of the >=2-clips rule for one finding (US-G6).

    ``sufficient`` — the finding can headline a report (>= minimum clips).
    ``compliant`` — sufficient, OR the finding states why fewer clips exist
    (``payload["evidence_note"]``); anything else is a build failure.
    """

    finding_id: str
    clip_count: int
    sufficient: bool
    compliant: bool
    note: str | None


def check_min_clips(
    finding: Mapping[str, Any], *, minimum: int = MIN_CLIPS_PER_FINDING
) -> EvidenceCheck:
    """Apply the US-G6 rule: >= ``minimum`` clips or an explicit shortfall note."""
    evidence: Mapping[str, Mapping[str, str]] = finding.get("evidence", {})
    count = clip_count(evidence)
    payload: Mapping[str, Any] = finding.get("payload", {})
    raw_note = payload.get("evidence_note")
    note = str(raw_note) if raw_note is not None else None
    sufficient = count >= minimum
    return EvidenceCheck(
        finding_id=str(finding["finding_id"]),
        clip_count=count,
        sufficient=sufficient,
        compliant=sufficient or note is not None,
        note=note,
    )


@dataclass(frozen=True)
class DeadLink:
    """One broken evidence link found by the crawler (US-G6 build failure)."""

    ball_no: str
    camera_id: str
    clip_id: str
    reason: str  # "missing" | "status:<status>"


def _lookup_status(lookup: ClipStatusLookup, clip_id: str) -> str | None:
    if callable(lookup):
        return lookup(clip_id)
    return lookup.get(clip_id)


def crawl_links(
    evidence: Mapping[str, Mapping[str, str]], clip_status: ClipStatusLookup
) -> list[DeadLink]:
    """Verify every referenced clip exists and is ``CUT``; list what is broken."""
    dead: list[DeadLink] = []
    for ball_key in sorted(evidence, key=str):
        cameras = evidence[ball_key]
        for camera_id in sorted(cameras):
            clip_id = str(cameras[camera_id])
            status = _lookup_status(clip_status, clip_id)
            if status is None:
                reason = "missing"
            elif str(status) != ClipStatus.CUT.value:
                reason = f"status:{status}"
            else:
                continue
            dead.append(
                DeadLink(
                    ball_no=str(ball_key), camera_id=str(camera_id), clip_id=clip_id, reason=reason
                )
            )
    return dead


def crawl_findings(
    findings: Iterable[Mapping[str, Any]], clip_status: ClipStatusLookup
) -> list[DeadLink]:
    """Link-integrity crawl across many findings' evidence objects."""
    dead: list[DeadLink] = []
    for finding in findings:
        dead.extend(crawl_links(finding.get("evidence", {}), clip_status))
    return dead


def report_body_evidence(body: Mapping[str, Any]) -> list[EvidenceMap]:
    """Every evidence object embedded in a report body (main + secondary)."""
    found: list[EvidenceMap] = []
    main = body.get("main_correction")
    if main is not None and main.get("evidence"):
        found.append(main["evidence"])
    for item in body.get("secondary", []):
        if item.get("evidence"):
            found.append(item["evidence"])
    return found


def crawl_report_body(body: Mapping[str, Any], clip_status: ClipStatusLookup) -> list[DeadLink]:
    """Link-integrity crawl over one report body (publish gate, CI + nightly)."""
    dead: list[DeadLink] = []
    for evidence in report_body_evidence(body):
        dead.extend(crawl_links(evidence, clip_status))
    return dead
