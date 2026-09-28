"""Release audit (Phase 7; T6/DoD): the manifest matches the backlog id-for-id.

The closing inventory in ``cricai_data.release_manifest`` must cover EVERY
``US-*`` story in ``cricket-ai-coach-user-stories.md`` exactly once, use only
the sanctioned statuses, and never leave a deferral without a pointer. The
expected id list is parsed from the backlog file itself, so adding or renaming
a story without updating the manifest fails the release gate.
"""

import re
from pathlib import Path

from cricai_data.release_manifest import (
    DEFERRED,
    MANIFEST,
    SHIPPED_AS_SEAM,
    SHIPPED_PARTIAL,
    VALID_STATUSES,
    StoryStatus,
)

#: Repo root: packages/data/tests/ -> packages/data -> packages -> root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKLOG = _REPO_ROOT / "cricket-ai-coach-user-stories.md"

#: A backlog story is a level-3 heading: ``### US-<Epic><n> — <title>``.
_STORY_HEADING = re.compile(r"^### (US-[A-L][0-9]+) ", re.MULTILINE)

#: A repo-relative docs/ path mentioned in a pointer (e.g. the register file a
#: residue points at). Matched so a renamed/deleted doc fails the audit.
_DOC_PATH = re.compile(r"docs/[\w./-]+\.\w+")

#: Canary: stories that carry a RELEASE-RECORDED gap. This is an INDEPENDENT
#: hardcoded expectation (not derived from MANIFEST) so a silent flip of any of
#: them to plain ``shipped`` — which would re-hide the gap — fails the gate.
_PINNED_NON_SHIPPED: dict[str, str] = {
    "US-D4": SHIPPED_PARTIAL,  # drag-to-adjust review UI never shipped
    "US-E5": SHIPPED_PARTIAL,  # frame-locked before/after AC never shipped
    "US-I3": SHIPPED_PARTIAL,  # release-scatter POINTS deploy-blocked (register R3)
    "US-K3": SHIPPED_PARTIAL,  # voice-note capture AC never shipped
    "US-L2": SHIPPED_PARTIAL,  # hosted CI blocked (GitHub Actions billing)
    "US-L5": SHIPPED_AS_SEAM,  # live-nudge production transport is a seam
}


def _backlog_ids() -> list[str]:
    return _STORY_HEADING.findall(_BACKLOG.read_text(encoding="utf-8"))


def test_backlog_parse_is_sane() -> None:
    """Guard the parser itself: the backlog has its known epics, no dupes."""
    ids = _backlog_ids()
    assert len(ids) == len(set(ids)), "backlog declares a story id twice"
    epics = {story_id.split("-")[1][0] for story_id in ids}
    assert epics == set("ABCDEFGHIJKL"), f"unexpected epic letters: {sorted(epics)}"


def test_every_backlog_story_present_exactly_once() -> None:
    manifest_ids = [entry.story_id for entry in MANIFEST]
    assert len(manifest_ids) == len(set(manifest_ids)), "duplicate story id in MANIFEST"
    backlog_ids = _backlog_ids()
    missing = sorted(set(backlog_ids) - set(manifest_ids))
    extra = sorted(set(manifest_ids) - set(backlog_ids))
    assert not missing, f"backlog stories absent from the manifest: {missing}"
    assert not extra, f"manifest ids not in the backlog: {extra}"


def test_counts_match_the_backlog() -> None:
    assert len(MANIFEST) == len(_backlog_ids())


def test_every_status_is_valid() -> None:
    bad = [(e.story_id, e.status) for e in MANIFEST if e.status not in VALID_STATUSES]
    assert not bad, f"invalid statuses: {bad}"


def test_every_entry_carries_a_pointer() -> None:
    """Stronger than the deferred-only requirement: an inventory line without
    evidence of WHERE the story lives (or where its deferral is recorded) is
    worthless, so every entry must point somewhere."""
    blank = [e.story_id for e in MANIFEST if not e.pointer.strip()]
    assert not blank, f"entries without a pointer: {blank}"


def test_every_deferred_entry_carries_a_pointer() -> None:
    """The assignment's literal gate, kept explicit even though the blanket
    pointer test above subsumes it: a deferral without a recorded decision
    pointer cannot be audited."""
    blank = [e.story_id for e in MANIFEST if e.status == DEFERRED and not e.pointer.strip()]
    assert not blank, f"deferred entries without a pointer: {blank}"


def test_non_shipped_statuses_name_their_residue() -> None:
    """Honesty guard: a shipped-partial must name its residue with ``RESIDUE``;
    the ``seam`` escape hatch is reserved for SHIPPED_AS_SEAM (US-L5). A future
    partial can no longer pass the gate by merely mentioning a seam in prose
    without naming its own gap (finding [13])."""
    vague = [
        entry.story_id
        for entry in MANIFEST
        if (entry.status == SHIPPED_PARTIAL and "RESIDUE" not in entry.pointer)
        or (entry.status == SHIPPED_AS_SEAM and "seam" not in entry.pointer)
    ]
    assert not vague, f"non-shipped entries without a named residue: {vague}"


def test_pointer_doc_paths_resolve() -> None:
    """A pointer that names a ``docs/`` file must resolve on disk. A residue
    that points at a renamed or deleted register/runbook silently rots the
    audit trail otherwise (finding [13])."""
    missing: list[tuple[str, str]] = []
    for entry in MANIFEST:
        for path in _DOC_PATH.findall(entry.pointer):
            if not (_REPO_ROOT / path).exists():
                missing.append((entry.story_id, path))
    assert not missing, f"manifest pointers name docs that do not exist: {missing}"


def test_known_partial_stories_stay_partial() -> None:
    """Canary: these stories carry a release-recorded gap, so a silent flip to
    plain ``shipped`` would re-hide it. Pinned independently of the MANIFEST so
    the flip fails the release gate (finding [13])."""
    actual = {entry.story_id: entry.status for entry in MANIFEST}
    wrong = {
        story_id: (actual.get(story_id), expected)
        for story_id, expected in _PINNED_NON_SHIPPED.items()
        if actual.get(story_id) != expected
    }
    assert not wrong, f"pinned non-shipped stories changed status: {wrong}"


def test_manifest_entries_are_story_statuses() -> None:
    assert all(isinstance(entry, StoryStatus) for entry in MANIFEST)
