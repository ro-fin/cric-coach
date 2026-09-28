"""SAF (US-K4): every kid-facing dashboard string passes the content lint.

Two corpora, so centralization-by-convention can no longer hide a string:

* **Copy modules** — every ``copy.ts`` under ``apps/web/app`` (globbed, not a
  single pinned path) is linted against the same banned-phrase lists the
  report pipeline is gated on (US-G2 tone/age rules, US-H4
  no-medical-language, US-I5 banned spin claims — geometric language only)
  AND must be number-free: the only numbers US-K4 allows on screen are the
  API's own (data-parity).
* **All web sources** — every non-test ``.ts``/``.tsx`` under ``apps/web``
  (components included) has ALL of its string literals — double-quoted,
  single-quoted and template literals alike — scanned for banned phrases, so
  a hardcoded kid-facing string in a component (or a template literal added
  to a copy module) can never slip past the gate. Component literals may
  carry digits (SVG geometry, CSS classes), so only the banned-phrase lint
  applies to this wider corpus.
"""

import re
from pathlib import Path

import pytest
from cricai_coaching.content_lint import (
    BANNED_COACHING_PHRASES,
    BANNED_MEDICAL_PHRASES,
    BANNED_SPIN_CLAIMS,
    find_banned_phrases,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_ROOT = REPO_ROOT / "apps" / "web"

#: Double-quoted / single-quoted TS string literals.
DOUBLE_QUOTED_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
SINGLE_QUOTED_RE = re.compile(r"'((?:[^'\\\n]|\\.)*)'")
#: Template literals; interpolation spans are blanked before linting.
TEMPLATE_RE = re.compile(r"`((?:[^`\\]|\\.)*)`", re.DOTALL)
INTERPOLATION_RE = re.compile(r"\$\{[^}]*\}")

ALL_BANNED = BANNED_COACHING_PHRASES + BANNED_MEDICAL_PHRASES + BANNED_SPIN_CLAIMS


def _literals(source: str) -> list[str]:
    """Every string literal in a TS/TSX source, template literals included."""
    return (
        DOUBLE_QUOTED_RE.findall(source)
        + SINGLE_QUOTED_RE.findall(source)
        + [INTERPOLATION_RE.sub(" ", template) for template in TEMPLATE_RE.findall(source)]
    )


def _copy_modules() -> list[Path]:
    modules = sorted((WEB_ROOT / "app").rglob("copy.ts"))
    assert modules, f"no copy modules found under {WEB_ROOT / 'app'} - the lint would be vacuous"
    return modules


def _web_sources() -> list[Path]:
    sources = [
        path
        for pattern in ("*.ts", "*.tsx")
        for path in sorted(WEB_ROOT.rglob(pattern))
        if ".next" not in path.parts
        and "node_modules" not in path.parts
        and not path.name.endswith((".test.ts", ".test.tsx"))
    ]
    assert sources, f"no web sources found under {WEB_ROOT} - the lint would be vacuous"
    return sources


@pytest.mark.safety
def test_copy_module_strings_have_no_banned_phrases() -> None:
    for module in _copy_modules():
        strings = _literals(module.read_text(encoding="utf-8"))
        assert strings, f"no string literals found in {module} - the lint would be vacuous"
        for value in strings:
            assert find_banned_phrases(value, ALL_BANNED) == [], (module.name, value)


@pytest.mark.safety
def test_copy_module_strings_are_number_free() -> None:
    """US-K4 data-parity: numbers on the dashboard come from the API alone."""
    for module in _copy_modules():
        for value in _literals(module.read_text(encoding="utf-8")):
            assert not any(ch.isdigit() for ch in value), (module.name, value)


@pytest.mark.safety
def test_every_web_source_literal_has_no_banned_phrases() -> None:
    """A banned phrase hardcoded in ANY web component fails the gate too."""
    for source in _web_sources():
        for value in _literals(source.read_text(encoding="utf-8")):
            assert find_banned_phrases(value, ALL_BANNED) == [], (
                str(source.relative_to(REPO_ROOT)),
                value,
            )
