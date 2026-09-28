"""US-G3/G6: coaching reports — fetch, publish gate, evidence verdicts.

Read surfaces return the pinned Report-body-v1 JSON (and its printable HTML
render). The publish endpoint is the US-H5/US-G3 gate: it recomputes the
SHA-256 of the safety text, recomputes every ``claims[]`` number through an
injected recompute callback, checks claims cover every number in wording,
and crawls every evidence link — any failure lands the report in ``blocked``
with the reasons audited, never ``published``. Coach evidence verdicts
(US-G6 "confirms / not supported") are stored per finding and aggregated into
rule analytics so rules with high not-supported rates get flagged.
"""

import hashlib
import math
import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Any, Literal

from cricai_coaching.evidence import MIN_CLIPS_PER_FINDING, clip_count, crawl_report_body
from cricai_coaching.report import render_report_html, validate_claims_coverage
from cricai_data.enums import EvidenceVerdict, MilestoneKind, ReportKind, ReportStatus, Role
from cricai_data.models import (
    AuditLog,
    Clip,
    Drill,
    EvidenceVerdictRecord,
    Finding,
    MetricBaseline,
    Milestone,
    Player,
    Report,
)
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db
from cricai_api.services.report_export import render_pdf, render_png
from cricai_api.services.safety_state import compute_safety_state

router = APIRouter(prefix="/reports", tags=["reports"])

#: A rule is flagged in analytics once it has this many verdicts and at least
#: this share of them say the evidence does not support the finding (US-G6).
FLAG_MIN_VERDICTS = 3
FLAG_NOT_SUPPORTED_RATE = 0.5

#: Injected claim recomputation: ``(db, report, claim) -> value | None``
#: (``None`` = not recomputable, which fails the publish gate).
Recomputer = Callable[[OrmSession, Report, dict[str, Any]], float | None]


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    player_id: uuid.UUID
    session_id: uuid.UUID | None
    kind: ReportKind
    period_start: date
    period_end: date
    status: ReportStatus
    body: dict[str, Any]
    safety_sha256: str | None
    quality: dict[str, Any] | None
    created_at: datetime


class PublishOut(BaseModel):
    status: ReportStatus
    reasons: list[str]


class ClaimCheckOut(BaseModel):
    recompute_key: str
    metric: str
    claimed: float
    recomputed: float | None
    match: bool


class VerdictIn(BaseModel):
    verdict: EvidenceVerdict
    note: str | None = None


class VerdictOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    finding_id: uuid.UUID
    verdict: EvidenceVerdict
    actor: str
    note: str | None
    created_at: datetime


class RuleVerdictStats(BaseModel):
    rule_key: str
    confirms: int
    not_supported: int
    not_supported_rate: float
    flagged: bool


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_milestone_kind(value: str) -> MilestoneKind | None:
    try:
        return MilestoneKind(value)
    except ValueError:
        return None


def _recompute_baseline(db: OrmSession, report: Report, parts: list[str]) -> float | None:
    """``baseline:<metric>:<zone_key>:<window>:<date>`` -> ``metric_baselines.value``.

    US-G5/H5 (finding [24]): the rollup's trend-point numbers recompute against
    the frozen nightly baseline slice they were rendered from — keyed by the
    report's player plus the metric/zone/window/snapshot-date coordinates. The
    slice is unique (``uq_baseline_slice_snapshot``), so at most one row matches.
    """
    if len(parts) != 5:
        return None
    _, metric, zone_key, window, snapshot_date = parts
    parsed_date = _parse_date(snapshot_date)
    if parsed_date is None:
        return None
    row = db.scalar(
        select(MetricBaseline).where(
            MetricBaseline.player_id == report.player_id,
            MetricBaseline.metric == metric,
            MetricBaseline.zone_key == zone_key,
            MetricBaseline.window == window,
            MetricBaseline.snapshot_date == parsed_date,
        )
    )
    return float(row.value) if row is not None else None


def _recompute_milestone(db: OrmSession, report: Report, parts: list[str]) -> float | None:
    """``milestone:<kind>:<metric>:<achieved_on>`` -> ``milestones.value``.

    US-K4/H5 (finding [24]): the rollup's milestone numbers recompute against
    the celebrated landmark row, keyed by the report's player plus kind/metric/
    achieved-on (the milestone unique key), so a fabricated personal-best value
    in the stored body fails the publish gate.
    """
    if len(parts) != 4:
        return None
    _, kind, metric, achieved_on = parts
    parsed_kind = _parse_milestone_kind(kind)
    parsed_date = _parse_date(achieved_on)
    if parsed_kind is None or parsed_date is None:
        return None
    row = db.scalar(
        select(Milestone).where(
            Milestone.player_id == report.player_id,
            Milestone.kind == parsed_kind,
            Milestone.metric == metric,
            Milestone.achieved_on == parsed_date,
        )
    )
    return float(row.value) if row is not None else None


def _recompute_finding(db: OrmSession, finding_id: str, field: str) -> float | None:
    parsed = _parse_uuid(finding_id)
    finding = db.get(Finding, parsed) if parsed is not None else None
    if finding is None:
        return None
    if field == "n":
        return float(finding.n)
    if field == "effect_size":
        return float(finding.effect_size) if finding.effect_size is not None else None
    value = finding.payload.get(field)
    return float(value) if isinstance(value, int | float) else None


def default_recompute(db: OrmSession, report: Report, claim: dict[str, Any]) -> float | None:
    """Recompute a claim from the database via its ``recompute_key`` (US-G3 IT).

    Key vocabulary, dispatched on the leading scheme:

    - ``finding:<id>:n`` -> ``findings.n``; ``finding:<id>:<field>`` ->
      ``findings.payload[field]``; ``drill:<id>:ball_count`` -> ``drills.ball_count``
      (the daily-report schemes pinned in ``cricai_coaching.report``);
    - ``baseline:<metric>:<zone_key>:<window>:<date>`` -> ``metric_baselines.value``;
    - ``milestone:<kind>:<metric>:<achieved_on>`` -> ``milestones.value``
      (the rollup schemes; finding [24]).

    The rollup schemes carry more than three colon-separated parts, so they are
    matched BEFORE the daily three-part guard. Unknown keys are not recomputable
    and fail the publish gate.
    """
    parts = str(claim["recompute_key"]).split(":")
    scheme = parts[0] if parts else ""
    if scheme == "baseline":
        return _recompute_baseline(db, report, parts)
    if scheme == "milestone":
        return _recompute_milestone(db, report, parts)
    if len(parts) != 3:
        return None
    _scheme, entity_id, field = parts
    if scheme == "finding":
        return _recompute_finding(db, entity_id, field)
    if scheme == "drill" and field == "ball_count":
        parsed = _parse_uuid(entity_id)
        drill = db.get(Drill, parsed) if parsed is not None else None
        return float(drill.ball_count) if drill is not None else None
    return None


def get_recomputer() -> Recomputer:
    """Dependency seam: tests and the pipeline can inject their own recomputer."""
    return default_recompute


def _get_report_or_404(db: OrmSession, report_id: uuid.UUID) -> Report:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    return report


def _player_may_read(report: Report, role: Role) -> bool:
    """The player only ever sees PUBLISHED reports (US-G3/H5): a draft or a
    safety-blocked report never ships to the child. Parents and coaches (the
    reviewers) see every status so they can act on it."""
    return role is not Role.PLAYER or report.status is ReportStatus.PUBLISHED


def _readable_or_404(report: Report, role: Role) -> Report:
    if not _player_may_read(report, role):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    return report


def _claim_match(claimed: float, recomputed: float | None) -> bool:
    return recomputed is not None and math.isclose(claimed, recomputed, rel_tol=1e-9, abs_tol=1e-9)


def _check_claims(db: OrmSession, report: Report, recompute: Recomputer) -> list[ClaimCheckOut]:
    checks: list[ClaimCheckOut] = []
    for claim in report.body.get("claims", []):
        claimed = float(claim["value"])
        recomputed = recompute(db, report, claim)
        checks.append(
            ClaimCheckOut(
                recompute_key=str(claim["recompute_key"]),
                metric=str(claim["metric"]),
                claimed=claimed,
                recomputed=recomputed,
                match=_claim_match(claimed, recomputed),
            )
        )
    return checks


def _as_number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _rendered_baseline_value(
    body: dict[str, Any], metric: str, zone_key: str, iso_date: str
) -> float | None:
    """The trend-point value the dashboard renders for a baseline claim's slice."""
    for trend in body.get("trends", []):
        if str(trend.get("metric")) == metric and str(trend.get("zone_key")) == zone_key:
            for point in trend.get("points", []):
                if str(point.get("date")) == iso_date:
                    return _as_number(point.get("value"))
    return None


def _rendered_milestone_value(
    body: dict[str, Any], kind: str, metric: str, iso_date: str
) -> float | None:
    """The milestone value the dashboard renders for a milestone claim's landmark."""
    for milestone in body.get("milestones", []):
        if (
            str(milestone.get("kind")) == kind
            and str(milestone.get("metric")) == metric
            and str(milestone.get("achieved_on")) == iso_date
        ):
            return _as_number(milestone.get("value"))
    return None


def _structured_claim_reasons(report: Report) -> list[str]:
    """US-H5 (finding [24]): the child sees ``trends``/``milestones`` numbers, but
    the gate recomputes ``claims``. Verify each rollup claim still equals the
    number the body renders, so a value mutated in the structured keys WITHOUT
    touching its claim (the number a tamperer would edit to fool the reader) is
    caught, not only a mutated claim. The rollup schemes are the only ones with
    structured numbers; ``finding``/``drill`` claims live in wording, guarded by
    :func:`validate_claims_coverage`. Windows are single-valued today (the
    nightly job writes only ``window="session"``), so (metric, zone_key, date)
    identifies a trend point unambiguously.
    """
    body = report.body
    reasons: list[str] = []
    for claim in body.get("claims", []):
        key = str(claim.get("recompute_key", ""))
        parts = key.split(":")
        scheme = parts[0] if parts else ""
        if scheme == "baseline" and len(parts) == 5:
            rendered = _rendered_baseline_value(body, parts[1], parts[2], parts[4])
        elif scheme == "milestone" and len(parts) == 4:
            rendered = _rendered_milestone_value(body, parts[1], parts[2], parts[3])
        else:
            continue
        claimed = _as_number(claim.get("value"))
        if claimed is None:
            continue  # a malformed claim value fails the recompute path instead
        if rendered is None:
            reasons.append(f"claim {key} has no matching rendered number in the report body")
        elif not _claim_match(claimed, rendered):
            reasons.append(
                f"claim {key} value {claimed} disagrees with the rendered number {rendered}"
            )
    return reasons


def _clip_status_lookup(db: OrmSession, body: dict[str, Any]) -> dict[str, str]:
    """Status of every clip the body references; absent ids read as dead links."""
    referenced: set[uuid.UUID] = set()
    main = body.get("main_correction")
    evidences = ([main["evidence"]] if main is not None else []) + [
        item["evidence"] for item in body.get("secondary", [])
    ]
    for evidence in evidences:
        for cameras in evidence.values():
            for clip_id in cameras.values():
                parsed = _parse_uuid(str(clip_id))
                if parsed is not None:
                    referenced.add(parsed)
    if not referenced:
        return {}
    rows = db.scalars(select(Clip).where(Clip.id.in_(referenced)))
    return {str(row.id): row.status.value for row in rows}


def _safety_reasons(report: Report) -> list[str]:
    """US-H5 hash gate: the safety text must be byte-identical to what was
    hashed at generation time — tampering anywhere is a hard reject."""
    safety = report.body.get("safety")
    if safety is None:
        if report.safety_sha256 is not None:
            return ["safety verdict removed from body but safety_sha256 is recorded"]
        return []
    digest = _sha256(str(safety.get("text", "")))
    reasons = []
    if report.safety_sha256 is None or digest != report.safety_sha256:
        reasons.append("safety text does not match safety_sha256 (tampered or unhashed)")
    if str(safety.get("sha256", "")) != digest:
        reasons.append("safety verdict's own sha256 does not match its text")
    return reasons


def publish_reasons(db: OrmSession, report: Report, recompute: Recomputer) -> list[str]:
    """Every reason this report may not be published (empty = publishable).

    Public across the package boundary: the worker's ``run_review_sweep``
    composes this exact gate so a timeout default-publish runs the same
    hash/claims/coverage/evidence/live-safety validation a manual publish
    runs. Renamed from the former private ``_publish_reasons`` so that
    cross-package coupling depends on a name this router owns as public API
    (the ``_publish_reasons`` alias below keeps any internal caller working).
    """
    reasons = _safety_reasons(report)
    for check in _check_claims(db, report, recompute):
        if not check.match:
            reasons.append(
                f"claim {check.recompute_key} not recomputable: "
                f"claimed {check.claimed}, recomputed {check.recomputed}"
            )
    reasons.extend(_structured_claim_reasons(report))
    reasons.extend(
        f"number {literal} in wording has no matching claim"
        for literal in validate_claims_coverage(report.body)
    )
    main = report.body.get("main_correction")
    if main is not None and clip_count(main["evidence"]) < MIN_CLIPS_PER_FINDING:
        reasons.append(f"main correction links fewer than {MIN_CLIPS_PER_FINDING} evidence clips")
    lookup = _clip_status_lookup(db, report.body)
    reasons.extend(
        f"dead evidence link: ball {link.ball_no} {link.camera_id} "
        f"clip {link.clip_id} ({link.reason})"
        for link in crawl_report_body(report.body, lookup)
    )
    reasons.extend(_current_state_reasons(db, report))
    return reasons


#: Backward-compatible alias for the former private name (kept so any lingering
#: internal caller keeps working; the public name is :func:`publish_reasons`).
_publish_reasons = publish_reasons


def _current_state_reasons(db: OrmSession, report: Report) -> list[str]:
    """US-H5 stale-verdict gate: re-check CURRENT H1/H4 server state at publish.

    The hash gate only proves the body's verdict text is intact, never that it
    still reflects reality. So publish recomputes the player's live safety
    state: while it is active, a report carrying NO verdict, or a stale
    inactive one, must not ship — a pain flag or ceiling reached after the
    report was drafted would otherwise reach the child unwarned.
    """
    player = db.get(Player, report.player_id)
    if player is None:
        return ["player row is gone; cannot re-check H1/H4 safety state"]
    verdict = compute_safety_state(db, player, date.today()).verdict
    if not verdict["active"]:
        return []
    codes = ", ".join(verdict["codes"])
    body_safety = report.body.get("safety")
    if body_safety is None:
        return [f"no safety verdict in report body but safety state is active ({codes})"]
    if not bool(body_safety.get("active")):
        return [f"report safety verdict is stale (inactive) while safety state is active ({codes})"]
    return []


@router.get("")
def list_reports(
    player_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    kind: ReportKind | None = None,
) -> list[ReportOut]:
    """List a player's reports, newest period first, optionally by kind. The
    player sees only PUBLISHED reports (US-G3/H5); reviewers see every status."""
    query = select(Report).where(Report.player_id == player_id)
    if kind is not None:
        query = query.where(Report.kind == kind)
    query = query.order_by(Report.period_start.desc(), Report.created_at.desc())
    return [
        ReportOut.model_validate(row) for row in db.scalars(query) if _player_may_read(row, role)
    ]


@router.get("/verdicts/analytics")
def verdict_analytics(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> list[RuleVerdictStats]:
    """US-G6: rules with high "not supported" rates get flagged for review."""
    rows = db.execute(
        select(EvidenceVerdictRecord.verdict, Finding.rule_key, Finding.kind).join(
            Finding, EvidenceVerdictRecord.finding_id == Finding.id
        )
    ).all()
    tallies: dict[str, dict[str, int]] = {}
    for verdict, rule_key, finding_kind in rows:
        key = rule_key if rule_key is not None else f"probe:{finding_kind}"
        tally = tallies.setdefault(key, {"confirms": 0, "not_supported": 0})
        tally["confirms" if verdict is EvidenceVerdict.CONFIRMS else "not_supported"] += 1
    stats: list[RuleVerdictStats] = []
    for key in sorted(tallies):
        confirms = tallies[key]["confirms"]
        not_supported = tallies[key]["not_supported"]
        total = confirms + not_supported
        rate = not_supported / total
        stats.append(
            RuleVerdictStats(
                rule_key=key,
                confirms=confirms,
                not_supported=not_supported,
                not_supported_rate=rate,
                flagged=total >= FLAG_MIN_VERDICTS and rate >= FLAG_NOT_SUPPORTED_RATE,
            )
        )
    return stats


@router.post("/findings/{finding_id}/verdicts", status_code=status.HTTP_201_CREATED)
def create_verdict(
    finding_id: uuid.UUID,
    payload: VerdictIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.COACH))],
) -> VerdictOut:
    """Coach marks a finding's evidence "confirms" or "not supported" (US-G6)."""
    # Read-only existence probe: no autoflush, so a genuinely absent finding is
    # a clean 404 while a concurrently deleted one surfaces on the INSERT below.
    with db.no_autoflush:
        exists = db.get(Finding, finding_id) is not None
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "finding not found")
    record = EvidenceVerdictRecord(
        finding_id=finding_id, verdict=payload.verdict, actor=role.value, note=payload.note
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError as exc:
        # A concurrent re-derive can delete the finding between the existence
        # check and this INSERT; the FK then fails. Surface a clear 409 rather
        # than an opaque 500 so the coach re-fetches the regenerated finding.
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "finding was removed concurrently (likely a re-derive); re-fetch it and retry",
        ) from exc
    return VerdictOut.model_validate(record)


@router.get("/findings/{finding_id}/verdicts")
def list_verdicts(
    finding_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> list[VerdictOut]:
    if db.get(Finding, finding_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "finding not found")
    rows = db.scalars(
        select(EvidenceVerdictRecord)
        .where(EvidenceVerdictRecord.finding_id == finding_id)
        .order_by(EvidenceVerdictRecord.created_at, EvidenceVerdictRecord.id)
    )
    return [VerdictOut.model_validate(row) for row in rows]


@router.get("/{report_id}")
def get_report(
    report_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> ReportOut:
    return ReportOut.model_validate(_readable_or_404(_get_report_or_404(db, report_id), role))


@router.get("/{report_id}/html")
def get_report_html(
    report_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> HTMLResponse:
    """Printable render for the net wall (US-G3) — pure function of the body.

    The player only reaches PUBLISHED reports (draft/blocked -> 404); a reviewer
    render of an unpublished report carries a prominent status banner so a draft
    or safety-blocked report is never mistaken for a shipped one."""
    report = _readable_or_404(_get_report_or_404(db, report_id), role)
    html = render_report_html(report.body)
    if report.status is not ReportStatus.PUBLISHED:
        html = f"<p>STATUS: {report.status.value.upper()}</p>\n{html}"
    return HTMLResponse(html)


#: Export formats -> (renderer, media type). The renderers typeset the same
#: HTML the /html endpoint serves, so exports never diverge from it (US-K5).
_EXPORTERS: dict[str, tuple[Callable[[str], bytes], str]] = {
    "pdf": (render_pdf, "application/pdf"),
    "png": (render_png, "image/png"),
}


@router.get("/{report_id}/export")
def export_report(
    report_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    format: Literal["pdf", "png"] = "pdf",
) -> Response:
    """US-K5 print/export: A4-legible PDF/PNG of the report for the net wall.

    Typesets exactly what ``/reports/{id}/html`` serves — same renderer, same
    status banner on unpublished reports — so there is no divergent copy. The
    player only reaches PUBLISHED reports (draft/blocked -> 404, as /html).
    Export is a share action (US-L3): every call is audit-logged with the
    artifact's SHA-256 as its watermark record.
    """
    report = _readable_or_404(_get_report_or_404(db, report_id), role)
    html = render_report_html(report.body)
    if report.status is not ReportStatus.PUBLISHED:
        html = f"<p>STATUS: {report.status.value.upper()}</p>\n{html}"
    renderer, media_type = _EXPORTERS[format]
    payload = renderer(html)
    db.add(
        AuditLog(
            actor=role.value,
            action="share_export",
            entity="report",
            entity_id=str(report.id),
            detail={
                "format": format,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "surface": "report_export",
            },
        )
    )
    return Response(
        payload,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="report-{report.id}.{format}"'},
    )


@router.get("/{report_id}/claims")
def recompute_claims(
    report_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
    recompute: Annotated[Recomputer, Depends(get_recomputer)],
) -> list[ClaimCheckOut]:
    """US-G3 IT surface: recompute every claimed number straight from the DB."""
    return _check_claims(db, _get_report_or_404(db, report_id), recompute)


@router.post("/{report_id}/publish")
def publish_report(
    report_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
    recompute: Annotated[Recomputer, Depends(get_recomputer)],
    response: Response,
) -> PublishOut:
    """Publish gate (US-G3/G6/H5): hash, claims, coverage and links must all
    hold — any failure lands the report in ``blocked`` with reasons audited."""
    report = _get_report_or_404(db, report_id)
    reasons = publish_reasons(db, report, recompute)
    if reasons:
        report.status = ReportStatus.BLOCKED
        db.add(
            AuditLog(
                actor=role.value,
                action="report_publish_blocked",
                entity="report",
                entity_id=str(report.id),
                detail={"reasons": reasons},
            )
        )
        db.flush()
        response.status_code = status.HTTP_409_CONFLICT
        return PublishOut(status=ReportStatus.BLOCKED, reasons=reasons)
    report.status = ReportStatus.PUBLISHED
    db.add(
        AuditLog(
            actor=role.value,
            action="report_published",
            entity="report",
            entity_id=str(report.id),
            detail={"safety_sha256": report.safety_sha256},
        )
    )
    db.flush()
    return PublishOut(status=ReportStatus.PUBLISHED, reasons=[])
