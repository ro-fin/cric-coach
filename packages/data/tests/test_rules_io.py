"""US-G2 YAML import/export CLI: the strict YAML subset (parser + emitter), file
validation, append-only versioning with idempotent re-import (export -> import
round-trips), and a release-gating SAF lint over every shipped seed rule.

``scripts`` is not an importable package, so the module is loaded by path.
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
from cricai_data.db import create_all, make_engine, make_session_factory
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_DIR = _REPO_ROOT / "packages" / "data" / "rules"

_spec = importlib.util.spec_from_file_location("rules_io", _REPO_ROOT / "scripts" / "rules_io.py")
assert _spec is not None and _spec.loader is not None
rules_io = importlib.util.module_from_spec(_spec)
sys.modules["rules_io"] = rules_io
_spec.loader.exec_module(rules_io)


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = make_engine("sqlite://")
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


VALID_RULE = """\
# a seed rule
rule_key: "sample_rule"
author: "coach"
approved_by: "coach"
enabled: true
rationale: "sample rationale"
definition:
  metric: "control"
  op: "eq"
  value: false
  min_n: 10
  severity: "minor"
  condition:
    length:
      - "short"
  text_data:
    finding: "Short balls hurried you today."
"""


def write(dir_path: Path, name: str, text: str) -> Path:
    path = dir_path / name
    path.write_text(text)
    return path


# --------------------------------------------------------------------------
# YAML subset parser
# --------------------------------------------------------------------------


class TestParseYaml:
    def test_parses_nested_mapping_and_sequence(self) -> None:
        parsed = rules_io.parse_yaml(VALID_RULE)
        assert parsed["rule_key"] == "sample_rule"
        assert parsed["enabled"] is True
        assert parsed["definition"]["value"] is False
        assert parsed["definition"]["condition"]["length"] == ["short"]

    def test_empty_document(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="empty document"):
            rules_io.parse_yaml("# only a comment\n\n")

    def test_top_level_must_be_mapping(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="top-level mapping"):
            rules_io.parse_yaml("- item\n")

    def test_tab_indentation_rejected(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="tabs are not allowed"):
            rules_io.parse_yaml("key: 1\n\tnested: 2\n")

    def test_non_key_line_rejected(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="expected 'key: value'"):
            rules_io.parse_yaml("just text\n")

    def test_duplicate_key_rejected(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="duplicate key 'a'"):
            rules_io.parse_yaml("a: 1\na: 2\n")

    def test_empty_block_rejected(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="empty block"):
            rules_io.parse_yaml("a:\nb: 1\n")

    def test_trailing_bad_indentation_rejected(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="unexpected indentation"):
            rules_io.parse_yaml("key: value\n- item\n")

    def test_malformed_quoted_scalar_rejected(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="malformed quoted string"):
            rules_io.parse_yaml('key: "unterminated\n')

    def test_bare_string_scalar(self) -> None:
        assert rules_io.parse_yaml("key: bareword\n")["key"] == "bareword"


# --------------------------------------------------------------------------
# YAML subset emitter
# --------------------------------------------------------------------------


class TestDumpYaml:
    def test_round_trips_through_parser(self) -> None:
        parsed = rules_io.parse_yaml(VALID_RULE)
        assert rules_io.parse_yaml(rules_io.dump_yaml(parsed)) == parsed

    def test_unsupported_scalar_type(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="cannot emit"):
            rules_io.dump_yaml({"a": {1, 2}})

    def test_unsupported_mapping_key(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="unsupported mapping key"):
            rules_io.dump_yaml({"has space": 1})

    def test_emits_list_of_scalars(self) -> None:
        assert rules_io.dump_yaml({"xs": [1, 2, 3]}) == "xs:\n  - 1\n  - 2\n  - 3\n"


# --------------------------------------------------------------------------
# File validation
# --------------------------------------------------------------------------


class TestValidateFile:
    def _doc(self, **overrides: Any) -> dict[str, Any]:
        doc: dict[str, Any] = rules_io.parse_yaml(VALID_RULE)
        doc.update(overrides)
        return doc

    def test_unknown_field(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="unknown fields"):
            rules_io._validate_file(self._doc(bogus=1), "f")

    def test_missing_field(self) -> None:
        doc = self._doc()
        del doc["author"]
        with pytest.raises(rules_io.RulesIOError, match="missing fields"):
            rules_io._validate_file(doc, "f")

    def test_bad_rule_key(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="rule_key must match"):
            rules_io._validate_file(self._doc(rule_key="Bad-Key"), "f")

    def test_blank_author(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="author must be a non-empty string"):
            rules_io._validate_file(self._doc(author="  "), "f")

    def test_bad_approved_by(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="approved_by must be"):
            rules_io._validate_file(self._doc(approved_by="  "), "f")

    def test_non_bool_enabled(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="enabled must be a boolean"):
            rules_io._validate_file(self._doc(enabled="yes"), "f")

    def test_enabled_requires_approval(self) -> None:
        with pytest.raises(rules_io.RulesIOError, match="requires approved_by"):
            rules_io._validate_file(self._doc(enabled=True, approved_by=None), "f")

    def test_invalid_definition(self) -> None:
        doc = self._doc()
        doc["definition"]["op"] = "ne"
        with pytest.raises(rules_io.RulesIOError, match="invalid definition"):
            rules_io._validate_file(doc, "f")

    def test_valid_normalized(self) -> None:
        content = rules_io._validate_file(self._doc(), "f")
        assert content["rule_key"] == "sample_rule"
        assert set(content) == {
            "rule_key",
            "author",
            "approved_by",
            "rationale",
            "enabled",
            "definition",
        }


class TestRuleFiles:
    def test_directory_without_yaml(self, tmp_path: Path) -> None:
        with pytest.raises(rules_io.RulesIOError, match=r"no \*.yaml files"):
            rules_io._rule_files([tmp_path])

    def test_missing_path(self, tmp_path: Path) -> None:
        with pytest.raises(rules_io.RulesIOError, match="no such file"):
            rules_io._rule_files([tmp_path / "ghost.yaml"])

    def test_directory_and_file(self, tmp_path: Path) -> None:
        write(tmp_path, "a.yaml", VALID_RULE)
        single = write(tmp_path, "b.yaml", VALID_RULE.replace("sample_rule", "other"))
        found = rules_io._rule_files([tmp_path, single])
        assert found[-1] == single


# --------------------------------------------------------------------------
# Import / export against a database
# --------------------------------------------------------------------------


class TestImportExport:
    def test_seed_rules_import_and_round_trip(self, ctx: WorkerContext, tmp_path: Path) -> None:
        # The shipped seed count grows with the backlog (5 batting rules from
        # US-G2, +4 bowling rules from US-I7); derive it, don't hard-code it.
        seed_count = len(sorted(_SEED_DIR.glob("*.yaml")))
        assert seed_count >= 9
        created = rules_io.import_rules(ctx, [_SEED_DIR])
        assert created["created"] == seed_count
        assert created["skipped"] == 0

        again = rules_io.import_rules(ctx, [_SEED_DIR])  # idempotent
        assert again == {"created": 0, "skipped": seed_count}

        out = tmp_path / "export"
        assert rules_io.export_rules(ctx, out) == seed_count
        reimport = rules_io.import_rules(ctx, [out])  # export -> import round-trips
        assert reimport == {"created": 0, "skipped": seed_count}

    def test_changed_content_appends_a_new_version(
        self, ctx: WorkerContext, tmp_path: Path
    ) -> None:
        write(tmp_path, "r.yaml", VALID_RULE)
        assert rules_io.import_rules(ctx, [tmp_path])["created"] == 1
        write(tmp_path, "r.yaml", VALID_RULE.replace("sample rationale", "sharper rationale"))
        result = rules_io.import_rules(ctx, [tmp_path])
        assert result == {"created": 1, "skipped": 0}
        out = tmp_path / "export"
        rules_io.export_rules(ctx, out)
        exported = rules_io.parse_yaml((out / "sample_rule.yaml").read_text())
        assert exported["version"] == 2
        assert exported["rationale"] == "sharper rationale"

    def test_duplicate_rule_key_across_files(self, ctx: WorkerContext, tmp_path: Path) -> None:
        write(tmp_path, "a.yaml", VALID_RULE)
        write(tmp_path, "b.yaml", VALID_RULE)  # same rule_key
        with pytest.raises(rules_io.RulesIOError, match="duplicate rule_key across files"):
            rules_io.import_rules(ctx, [tmp_path])


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------


class TestMain:
    def test_import_then_export(
        self, ctx: WorkerContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(rules_io.WorkerContext, "from_env", lambda: ctx)
        seed_count = len(sorted(_SEED_DIR.glob("*.yaml")))  # batting + bowling seeds
        assert rules_io.main(["import", str(_SEED_DIR)]) == 0
        assert f"created={seed_count}" in capsys.readouterr().out
        out = tmp_path / "out"
        assert rules_io.main(["export", "--out", str(out)]) == 0
        assert f"exported {seed_count} rules" in capsys.readouterr().out

    def test_import_error_exits_two(
        self, ctx: WorkerContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(rules_io.WorkerContext, "from_env", lambda: ctx)
        write(tmp_path, "bad.yaml", VALID_RULE.replace('op: "eq"', 'op: "ne"'))
        assert rules_io.main(["import", str(tmp_path)]) == 2
        assert "error:" in capsys.readouterr().err

    def test_missing_subcommand_is_usage_error(self) -> None:
        with pytest.raises(SystemExit):
            rules_io.main([])


# --------------------------------------------------------------------------
# SAF: every shipped seed rule text passes the age-appropriateness lint
# --------------------------------------------------------------------------


class TestSeedRuleSafety:
    @pytest.mark.safety
    def test_no_seed_rule_text_uses_banned_phrasing(self) -> None:
        banned = BANNED_COACHING_PHRASES + BANNED_MEDICAL_PHRASES + BANNED_SPIN_CLAIMS
        seed_files = sorted(_SEED_DIR.glob("*.yaml"))
        assert seed_files, "no seed rules to lint"
        offenders: dict[str, list[str]] = {}
        for path in seed_files:
            document = rules_io.parse_yaml(path.read_text())
            for name, text in document["definition"]["text_data"].items():
                violations = find_banned_phrases(text, banned)
                if violations:
                    offenders[f"{path.name}:{name}"] = [v.phrase for v in violations]
        assert offenders == {}
