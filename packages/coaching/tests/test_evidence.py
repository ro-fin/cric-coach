"""US-G6 acceptance: evidence objects, the >=2-clips rule, link integrity."""

from dataclasses import dataclass
from typing import Any

from cricai_coaching.evidence import (
    MIN_CLIPS_PER_FINDING,
    DeadLink,
    build_evidence,
    check_min_clips,
    clip_count,
    clip_ids,
    crawl_findings,
    crawl_links,
    crawl_report_body,
    report_body_evidence,
)


@dataclass(frozen=True)
class FakeClipRow:
    """ORM-like clip row: attributes, ``id`` as the clip identity."""

    id: str
    ball_no: int
    camera_id: str
    status: str


def _clip(ball_no: int, camera_id: str, clip_id: str, status: str = "cut") -> dict[str, object]:
    return {"ball_no": ball_no, "camera_id": camera_id, "clip_id": clip_id, "status": status}


def _finding(fid: str, evidence: dict[str, dict[str, str]], **payload: object) -> dict[str, object]:
    return {"finding_id": fid, "evidence": evidence, "payload": dict(payload)}


# --- build_evidence -------------------------------------------------------


def test_build_evidence_keeps_only_cut_clips() -> None:
    evidence = build_evidence(
        [
            _clip(1, "C1", "a"),
            _clip(1, "C2", "b", status="pending"),
            _clip(2, "C1", "c", status="failed"),
            _clip(2, "C2", "d", status="gap"),
            _clip(3, "C1", "e"),
        ]
    )
    assert evidence == {"1": {"C1": "a"}, "3": {"C1": "e"}}


def test_build_evidence_accepts_orm_like_rows() -> None:
    rows = [
        FakeClipRow(id="clip-1", ball_no=4, camera_id="C1", status="cut"),
        FakeClipRow(id="clip-2", ball_no=4, camera_id="C2", status="cut"),
    ]
    assert build_evidence(rows) == {"4": {"C1": "clip-1", "C2": "clip-2"}}


def test_build_evidence_serializes_ball_numbers_as_strings() -> None:
    evidence = build_evidence([_clip(10, "C1", "a")])
    assert list(evidence) == ["10"]


# --- clip_ids / clip_count ------------------------------------------------


def test_clip_ids_are_deterministically_ordered() -> None:
    evidence = {"2": {"C2": "d", "C1": "c"}, "10": {"C1": "a"}}
    assert clip_ids(evidence) == ["a", "c", "d"]  # sorted by ball key then camera


def test_clip_count_dedupes_repeated_clip_ids() -> None:
    evidence = {"1": {"C1": "same"}, "2": {"C1": "same"}}
    assert clip_count(evidence) == 1


# --- the >=2-clips rule (US-G6) -------------------------------------------


def test_two_clips_is_sufficient_and_compliant() -> None:
    check = check_min_clips(_finding("f1", {"1": {"C1": "a", "C2": "b"}}))
    assert check.sufficient
    assert check.compliant
    assert check.clip_count == MIN_CLIPS_PER_FINDING
    assert check.note is None


def test_one_clip_without_a_note_is_a_violation() -> None:
    check = check_min_clips(_finding("f1", {"1": {"C1": "a"}}))
    assert not check.sufficient
    assert not check.compliant


def test_one_clip_with_a_stated_reason_is_compliant_but_not_sufficient() -> None:
    check = check_min_clips(
        _finding("f1", {"1": {"C1": "a"}}, evidence_note="C2 footage failed checksum")
    )
    assert not check.sufficient
    assert check.compliant
    assert check.note == "C2 footage failed checksum"


def test_min_clips_threshold_is_configurable() -> None:
    finding = _finding("f1", {"1": {"C1": "a", "C2": "b"}})
    assert not check_min_clips(finding, minimum=3).sufficient


def test_check_handles_findings_without_evidence_or_payload() -> None:
    check = check_min_clips({"finding_id": "f1"})
    assert check.clip_count == 0
    assert not check.compliant


# --- link-integrity crawler ------------------------------------------------


def test_crawler_passes_when_every_link_is_cut() -> None:
    evidence = {"1": {"C1": "a", "C2": "b"}}
    assert crawl_links(evidence, {"a": "cut", "b": "cut"}) == []


def test_crawler_reports_missing_and_non_cut_clips() -> None:
    evidence = {"1": {"C1": "a", "C2": "b"}, "2": {"C1": "c"}}
    dead = crawl_links(evidence, {"a": "cut", "b": "pending"})
    assert dead == [
        DeadLink(ball_no="1", camera_id="C2", clip_id="b", reason="status:pending"),
        DeadLink(ball_no="2", camera_id="C1", clip_id="c", reason="missing"),
    ]


def test_crawler_accepts_a_callable_lookup() -> None:
    evidence = {"1": {"C1": "a"}}
    assert crawl_links(evidence, lambda _clip_id: "cut") == []
    dead = crawl_links(evidence, lambda _clip_id: None)
    assert [link.reason for link in dead] == ["missing"]


def test_crawl_findings_aggregates_across_findings() -> None:
    findings: list[dict[str, Any]] = [
        _finding("f1", {"1": {"C1": "a"}}),
        {"finding_id": "f2"},  # no evidence key at all
        _finding("f3", {"2": {"C1": "z"}}),
    ]
    dead = crawl_findings(findings, {"a": "cut"})
    assert [link.clip_id for link in dead] == ["z"]


# --- report-body crawling ---------------------------------------------------


def _body(main: dict[str, Any] | None, secondary: list[dict[str, Any]]) -> dict[str, Any]:
    return {"main_correction": main, "secondary": secondary}


def test_report_body_evidence_collects_main_and_secondary() -> None:
    main: dict[str, Any] = {"evidence": {"1": {"C1": "a"}}}
    secondary: list[dict[str, Any]] = [{"evidence": {"2": {"C1": "b"}}}, {"evidence": {}}]
    assert report_body_evidence(_body(main, secondary)) == [
        {"1": {"C1": "a"}},
        {"2": {"C1": "b"}},
    ]


def test_report_body_evidence_handles_honesty_bodies() -> None:
    assert report_body_evidence(_body(None, [])) == []
    assert report_body_evidence({}) == []


def test_crawl_report_body_finds_dead_links_everywhere() -> None:
    body = _body(
        {"evidence": {"1": {"C1": "a"}}},
        [{"evidence": {"2": {"C1": "b"}}}],
    )
    dead = crawl_report_body(body, {"a": "failed"})
    assert [(link.clip_id, link.reason) for link in dead] == [
        ("a", "status:failed"),
        ("b", "missing"),
    ]
