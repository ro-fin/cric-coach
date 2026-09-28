"""SAF (US-I5): ONE canonical banned-claim lint over every bowling-facing module.

The honest-limits rule (backlog; docs/coaching_metrics.md honest-limits
register) bans unmeasurable spin claims — rpm, revs, spin rate, spin/seam
axis — from every bowling-facing string. The canonical list is
:data:`cricai_coaching.content_lint.BANNED_SPIN_CLAIMS`; this suite scans the
string constants of every bowling-facing Python module against it via
:func:`find_banned_phrases`, so a phrase added to the canonical list is
enforced everywhere at once. Before this suite, two hand-rolled inline regex
copies (``rpm|revs|spin rate`` only) gated trajectory.py and bowling_flight.py
and had already drifted behind the canonical list (no ``spin axis`` /
``seam axis``) — an inline copy can never drift again because there is none.
"""

import ast
from pathlib import Path

import pytest
from cricai_coaching import bowling_analysis, bowling_report
from cricai_coaching.content_lint import BANNED_SPIN_CLAIMS, find_banned_phrases
from cricai_vision import bowler_pose, release, trajectory

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKER_SRC = REPO_ROOT / "apps" / "worker" / "src" / "cricai_worker"

#: Every bowling-facing Python module whose strings the US-I5 lint gates.
#: Importable packages resolve via ``__file__`` (rename-proof); the worker app
#: is not importable from here, so its modules are pinned by path.
BOWLING_FACING_MODULES: tuple[Path, ...] = (
    Path(str(trajectory.__file__)),
    Path(str(release.__file__)),
    Path(str(bowler_pose.__file__)),
    Path(str(bowling_analysis.__file__)),
    Path(str(bowling_report.__file__)),
    WORKER_SRC / "bowling_flight.py",
    WORKER_SRC / "bowling_action.py",
    WORKER_SRC / "classify_variations.py",
)


def _string_constants(path: Path) -> list[tuple[int, str]]:
    assert path.exists(), f"bowling-facing module moved: update {path}"
    return [
        (node.lineno, node.value)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


@pytest.mark.safety
@pytest.mark.parametrize("module", BOWLING_FACING_MODULES, ids=lambda p: str(p.name))
def test_bowling_module_strings_pass_the_canonical_spin_lint(module: Path) -> None:
    """No bowling-facing string may overclaim: geometry only (backlog rule)."""
    strings = _string_constants(module)
    assert strings, f"no string constants found in {module.name} - the lint would be vacuous"
    for lineno, value in strings:
        violations = find_banned_phrases(value, BANNED_SPIN_CLAIMS)
        assert violations == [], (
            f"banned spin claim(s) {[v.phrase for v in violations]} in {module.name} line {lineno}"
        )
