"""US-I7 bowling seed rules: >=4 coach-approved ``bowling_*.yaml`` rules that
parse through the US-G2 DSL, scope themselves to bowling-mode records, fire on
matching bowling BallRecords (and never on batting ones), and pass the SAF
banned-claim + kid-safety lints over every string including rationales.

``scripts`` is not an importable package, so the loader module comes in by path
(the ``test_rules_io`` idiom).
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from cricai_coaching.content_lint import (
    BANNED_COACHING_PHRASES,
    BANNED_MEDICAL_PHRASES,
    BANNED_SPIN_CLAIMS,
    find_banned_phrases,
)
from cricai_coaching.rules import evaluate_rule, parse_rule_definition

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_DIR = _REPO_ROOT / "packages" / "data" / "rules"

_spec = importlib.util.spec_from_file_location(
    "rules_io_for_bowling", _REPO_ROOT / "scripts" / "rules_io.py"
)
assert _spec is not None and _spec.loader is not None
rules_io = importlib.util.module_from_spec(_spec)
sys.modules["rules_io_for_bowling"] = rules_io
_spec.loader.exec_module(rules_io)


def _bowling_rule_files() -> list[Path]:
    return sorted(_SEED_DIR.glob("bowling_*.yaml"))


def _documents() -> list[dict[str, Any]]:
    return [rules_io.parse_yaml(path.read_text()) for path in _bowling_rule_files()]


def _hit_value(definition: dict[str, Any]) -> Any:
    """A metric value that satisfies the rule's ``op`` against its threshold."""
    op, value = definition["op"], definition["value"]
    if op == "eq":
        return value
    number = float(value)
    return number + 1 if op in ("gt", "ge") else number - 1


def _records(definition: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    """min_n records that all hit the rule, in the given session mode."""
    return [
        {
            "ball_id": ball_no + 1,
            "mode": mode,
            definition["metric"]: _hit_value(definition),
            "confidence": {},
            "clips": {"C5": f"clip-{ball_no + 1}"},
        }
        for ball_no in range(definition["min_n"])
    ]


class TestBowlingSeedRules:
    def test_at_least_four_bowling_seed_rules_ship(self) -> None:
        assert len(_bowling_rule_files()) >= 4

    def test_rule_keys_match_file_names_for_export_round_trip(self) -> None:
        for path, document in zip(_bowling_rule_files(), _documents(), strict=True):
            assert document["rule_key"] == path.stem

    def test_every_rule_is_enabled_and_coach_approved(self) -> None:
        """US-I7/US-G2: the AI's opinions are the coach's opinions."""
        for document in _documents():
            assert document["enabled"] is True, document["rule_key"]
            assert document["approved_by"], document["rule_key"]

    def test_every_rule_parses_and_scopes_to_bowling_mode(self) -> None:
        """A bowling rule must never fire on batting records (condition guard)."""
        for document in _documents():
            parsed = parse_rule_definition(document["definition"])
            assert parsed.condition.get("mode") == ("bowling",), document["rule_key"]

    def test_rules_fire_on_bowling_records_and_not_on_batting(self) -> None:
        """Each seed rule can actually fire (US-I7: report reuses G2 rules)."""
        for document in _documents():
            definition = document["definition"]
            parsed = parse_rule_definition(definition)
            fired = evaluate_rule(document["rule_key"], parsed, _records(definition, "bowling"))
            assert fired is not None, document["rule_key"]
            assert fired["n"] == definition["min_n"]
            assert fired["kind"] == "rule"
            batting = evaluate_rule(document["rule_key"], parsed, _records(definition, "batting"))
            assert batting is None, document["rule_key"]

    @pytest.mark.safety
    def test_banned_claim_lint_over_all_strings_including_rationale(self) -> None:
        """SAF (US-I5): geometric language only — turn, drift, dip, bounce."""
        banned = BANNED_COACHING_PHRASES + BANNED_MEDICAL_PHRASES + BANNED_SPIN_CLAIMS
        documents = _documents()
        assert documents, "no bowling seed rules to lint"
        offenders: dict[str, list[str]] = {}
        for document in documents:
            texts = {"rationale": document["rationale"], **document["definition"]["text_data"]}
            for name, text in texts.items():
                violations = find_banned_phrases(text, banned)
                if violations:
                    offenders[f"{document['rule_key']}:{name}"] = [v.phrase for v in violations]
        assert offenders == {}

    @pytest.mark.safety
    def test_rule_wording_is_digit_free(self) -> None:
        """Rule text feeds report wording; digits there would need claims (US-G3)."""
        for document in _documents():
            for name, text in document["definition"]["text_data"].items():
                assert not any(char.isdigit() for char in text), (document["rule_key"], name)
