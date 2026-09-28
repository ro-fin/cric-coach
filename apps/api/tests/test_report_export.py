"""US-K5 acceptance: PDF/PNG report export — A4 typesetting of the exact HTML
the /html surface serves (no divergent copies), deterministic bytes (the ST
snapshot contract), player publish gate, and US-L3 export audit logging."""

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

from cricai_api.services.report_export import (
    A4_HEIGHT_IN,
    STYLES,
    TextLine,
    html_to_lines,
    layout_pages,
    render_pdf,
    render_png,
    wrap_lines,
)
from cricai_data.enums import ReportStatus
from cricai_data.models import AuditLog, Report
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth

PERIOD = date(2026, 7, 9)


@contextmanager
def _db(client: TestClient) -> Iterator[OrmSession]:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db: OrmSession = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _report_body() -> dict[str, Any]:
    return {
        "kind": "daily",
        "period": {"start": PERIOD.isoformat(), "end": PERIOD.isoformat()},
        "main_correction": {
            "finding_id": "f-1",
            "text": "Move your front foot to the ball. Seen on 12 balls.",
            "evidence": {"3": {"C1": "clip-a"}, "4": {"C2": "clip-b"}},
        },
        "drill": {
            "drill_id": None,
            "text": "Front-foot ladder drill.",
            "machine_settings": {},
            "success_metric": "control_pct",
        },
        "goal": {"metric": "control_pct", "target": 70.0, "condition": {}},
        "secondary": [],
        "positive": "Great effort today.",
        "safety": None,
        "honesty_banner": None,
        "coverage_note": None,
        "fatigue_note": None,
        "claims": [{"value": 12, "metric": "ball_count", "recompute_key": "finding:f-1:n"}],
    }


def _seed_report(
    client: TestClient, *, status: ReportStatus = ReportStatus.DRAFT
) -> tuple[str, str]:
    player = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    ).json()
    session = client.post(
        "/sessions",
        json={
            "player_id": player["id"],
            "date": PERIOD.isoformat(),
            "session_type": "batting",
            "bowler_source": "coach",
        },
        headers=auth(PARENT_TOKEN),
    ).json()
    with _db(client) as db:
        report = Report(
            player_id=uuid.UUID(player["id"]),
            session_id=uuid.UUID(session["id"]),
            kind="daily",
            period_start=PERIOD,
            period_end=PERIOD,
            body=_report_body(),
            status=status,
        )
        db.add(report)
        db.flush()
        return str(report.id), player["id"]


# --- HTML flattening ----------------------------------------------------------


def test_html_to_lines_extracts_styled_text_in_document_order() -> None:
    html = (
        '<article class="report" data-kind="daily"><header><h1>Daily report</h1>'
        "<p>2026-07-09 to 2026-07-09</p></header>"
        "<section><h2>Main correction</h2><p>Watch the   stride &amp; head.</p>"
        '<ul class="evidence"><li>ball 3 - C1: <code>clip-a</code></li></ul></section>'
        '<details class="secondary"><summary>Also worth a look</summary>'
        "<ul><li>Second item.</li></ul></details></article>"
    )
    assert html_to_lines(html) == [
        TextLine("Daily report", "title"),
        TextLine("2026-07-09 to 2026-07-09", "body"),
        TextLine("Main correction", "heading"),
        TextLine("Watch the stride & head.", "body"),
        TextLine("ball 3 - C1: clip-a", "body"),
        TextLine("Also worth a look", "heading"),
        TextLine("Second item.", "body"),
    ]


def test_html_to_lines_skips_empty_text_stray_endtags_and_orphan_data() -> None:
    assert html_to_lines("<p>   </p>") == []  # whitespace-only: no line
    assert html_to_lines("</p><p>ok</p>") == [TextLine("ok", "body")]  # stray endtag
    assert html_to_lines("<details>loose text</details>") == []  # data outside text tags
    assert html_to_lines("</code>") == []  # endtag that is not a text tag


# --- wrapping & pagination ----------------------------------------------------


def test_wrap_lines_respects_per_style_column_budgets() -> None:
    long_text = "word " * 40
    [line] = [TextLine(long_text.strip(), "body")]
    wrapped = wrap_lines([line])
    assert len(wrapped) > 1
    assert all(len(piece.text) <= STYLES["body"][2] for piece in wrapped)
    assert all(piece.style == "body" for piece in wrapped)
    heading = wrap_lines([TextLine("h " * 50, "heading")])
    assert all(len(piece.text) <= STYLES["heading"][2] for piece in heading)


def test_layout_paginates_at_a4_and_restarts_heading_gap_per_page() -> None:
    lines = [TextLine("Heading", "heading")] + [TextLine(f"line {i}", "body") for i in range(120)]
    pages, height = layout_pages(lines, A4_HEIGHT_IN)
    assert height == A4_HEIGHT_IN
    assert len(pages) > 1
    assert all(page for page in pages)  # no empty pages
    # every placed baseline sits inside the printable area
    for page in pages:
        assert all(0 < y <= A4_HEIGHT_IN for y, _line in page)


def test_layout_single_page_grows_beyond_a4_for_long_reports() -> None:
    short_pages, short_height = layout_pages([TextLine("one line", "body")], None)
    assert len(short_pages) == 1
    assert short_height == A4_HEIGHT_IN  # never smaller than A4
    long_lines = [TextLine(f"line {i}", "body") for i in range(200)]
    long_pages, long_height = layout_pages(long_lines, None)
    assert len(long_pages) == 1  # a PNG has no page to break to
    assert long_height > A4_HEIGHT_IN
    assert len(long_pages[0]) == 200


def test_renderers_are_deterministic_and_emit_real_artifacts() -> None:
    html = "<h1>Daily report</h1><h2>Goal</h2><p>control_pct: reach 70 next session.</p>"
    pdf = render_pdf(html)
    png = render_png(html)
    assert pdf.startswith(b"%PDF-")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert render_pdf(html) == pdf  # identical body -> identical bytes
    assert render_png(html) == png


def test_render_pdf_paginates_long_reports() -> None:
    single = render_pdf("<p>short</p>")
    many = "".join(f"<p>paragraph {i} of a very long report body</p>" for i in range(150))
    multi = render_pdf(many)
    # matplotlib's PDF backend writes one /Type /Page object per page.
    assert multi.count(b"/Type /Page") > single.count(b"/Type /Page")


# --- export endpoint ----------------------------------------------------------


def test_export_pdf_is_default_audited_and_deterministic(client: TestClient) -> None:
    report_id, _player = _seed_report(client)
    response = client.get(f"/reports/{report_id}/export", headers=auth(COACH_TOKEN))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert f'filename="report-{report_id}.pdf"' in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")
    again = client.get(f"/reports/{report_id}/export?format=pdf", headers=auth(COACH_TOKEN))
    assert again.content == response.content
    with _db(client) as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "share_export")).all()
        details = [dict(row.detail or {}) for row in rows]
        assert len(rows) == 2
        assert all(row.entity == "report" and row.entity_id == report_id for row in rows)
        assert all(
            d
            == {
                "format": "pdf",
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "surface": "report_export",
            }
            for d in details
        )


def test_export_png_format(client: TestClient) -> None:
    report_id, _player = _seed_report(client)
    response = client.get(f"/reports/{report_id}/export?format=png", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_export_rejects_unknown_formats_and_missing_reports(client: TestClient) -> None:
    report_id, _player = _seed_report(client)
    bad = client.get(f"/reports/{report_id}/export?format=docx", headers=auth(COACH_TOKEN))
    assert bad.status_code == 422
    unknown = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/reports/{unknown}/export", headers=auth(COACH_TOKEN)).status_code == 404
    assert client.get(f"/reports/{report_id}/export").status_code == 401


def test_player_exports_published_reports_only(client: TestClient) -> None:
    """US-G3/H5 gate holds on the export surface too: a draft or blocked report
    never ships to the child, in any format."""
    draft_id, _player = _seed_report(client, status=ReportStatus.DRAFT)
    response = client.get(f"/reports/{draft_id}/export", headers=auth(PLAYER_TOKEN))
    assert response.status_code == 404
    published_id, _player = _seed_report(client, status=ReportStatus.PUBLISHED)
    ok = client.get(f"/reports/{published_id}/export", headers=auth(PLAYER_TOKEN))
    assert ok.status_code == 200
    assert ok.content.startswith(b"%PDF-")


def test_reviewer_export_of_unpublished_report_carries_status_banner(
    client: TestClient,
) -> None:
    """Reviewer exports mirror the /html status banner so a draft print is
    never mistaken for a published one; published exports drop the banner
    (and therefore differ in bytes)."""
    report_id, _player = _seed_report(client, status=ReportStatus.DRAFT)
    draft_bytes = client.get(f"/reports/{report_id}/export", headers=auth(COACH_TOKEN)).content
    with _db(client) as db:
        report = db.get(Report, uuid.UUID(report_id))
        assert report is not None
        report.status = ReportStatus.PUBLISHED
    published_bytes = client.get(f"/reports/{report_id}/export", headers=auth(COACH_TOKEN)).content
    assert draft_bytes != published_bytes
