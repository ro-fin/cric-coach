#!/usr/bin/env python3
"""Coaching-rule YAML import/export CLI (US-G2: rules are data the coach edits).

Usage:
    uv run scripts/rules_io.py import packages/data/rules
    uv run scripts/rules_io.py import my_rule.yaml other_rule.yaml
    uv run scripts/rules_io.py export --out ./rules_backup

Import validates every file (top-level fields, the rule DSL via
``cricai_coaching.rules.parse_rule_definition`` — which also content-lints all
rule texts) and appends a NEW ``coaching_rules`` version per changed rule;
re-importing identical content is a no-op (idempotent), so ``export`` then
``import`` round-trips cleanly. Any invalid file aborts the whole import loud
(exit 2) before anything is written. Reads DB config from the same
``CRICAI_DATABASE_URL`` environment as the worker.

The YAML dialect is a strict self-contained subset (no third-party parser):
two-space-indented block mappings, block sequences of scalars, JSON scalars
(double-quoted strings, numbers, ``true``/``false``/``null``) plus bare
unquoted strings, and full-line ``#`` comments. Everything the emitter writes
parses back identically, and the files stay valid standard YAML.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cricai_coaching.rules import RuleError, parse_rule_definition
from cricai_data.models import AuditLog, CoachingRule
from cricai_worker.context import WorkerContext
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:[ \t]+(.*))?$")
_RULE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Top-level file fields; ``version`` is informational (versions are append-only).
_REQUIRED_FIELDS = ("rule_key", "author", "rationale", "definition")
_ALLOWED_FIELDS = frozenset({*_REQUIRED_FIELDS, "approved_by", "enabled", "version"})

#: Fields compared to decide whether a re-import is a no-op or a new version.
_CONTENT_FIELDS = ("author", "approved_by", "rationale", "enabled", "definition")


class RulesIOError(RuntimeError):
    """Anything that must stop an import/export with a loud message."""


# --------------------------------------------------------------------------
# Minimal YAML subset (parser + emitter)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Line:
    indent: int
    content: str
    number: int


def _lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if raw[indent : indent + 1] == "\t":
            raise RulesIOError(f"line {number}: tabs are not allowed for indentation")
        lines.append(_Line(indent=indent, content=stripped, number=number))
    return lines


def _parse_scalar(text: str, number: int) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        if text.startswith(('"', "'")):
            raise RulesIOError(f"line {number}: malformed quoted string: {text}") from None
        return text  # bare unquoted string


def _parse_sequence(lines: list[_Line], i: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while i < len(lines) and lines[i].indent == indent and lines[i].content.startswith("- "):
        items.append(_parse_scalar(lines[i].content[2:].strip(), lines[i].number))
        i += 1
    return items, i


def _parse_mapping(lines: list[_Line], i: int, indent: int) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    while i < len(lines) and lines[i].indent == indent and not lines[i].content.startswith("- "):
        line = lines[i]
        match = _KEY_RE.match(line.content)
        if match is None:
            raise RulesIOError(f"line {line.number}: expected 'key: value', got: {line.content}")
        key, inline = match.group(1), match.group(2)
        if key in mapping:
            raise RulesIOError(f"line {line.number}: duplicate key {key!r}")
        if inline is not None:
            mapping[key] = _parse_scalar(inline.strip(), line.number)
            i += 1
            continue
        i += 1
        if i >= len(lines) or lines[i].indent <= indent:
            raise RulesIOError(f"line {line.number}: key {key!r} has an empty block")
        mapping[key], i = _parse_block(lines, i, lines[i].indent)
    return mapping, i


def _parse_block(lines: list[_Line], i: int, indent: int) -> tuple[Any, int]:
    if lines[i].content.startswith("- "):
        return _parse_sequence(lines, i, indent)
    return _parse_mapping(lines, i, indent)


def parse_yaml(text: str) -> dict[str, Any]:
    """Parse one rule file written in the supported YAML subset."""
    lines = _lines(text)
    if not lines:
        raise RulesIOError("empty document")
    if lines[0].indent != 0 or lines[0].content.startswith("- "):
        raise RulesIOError("document must be a top-level mapping")
    mapping, i = _parse_mapping(lines, 0, 0)
    if i != len(lines):
        raise RulesIOError(f"line {lines[i].number}: unexpected indentation")
    return mapping


def _emit_scalar(value: Any, path: str) -> str:
    if value is None or isinstance(value, bool | int | float | str):
        return json.dumps(value)
    raise RulesIOError(f"{path}: cannot emit {type(value).__name__} as a YAML scalar")


def _emit(value: Any, indent: int, path: str, out: list[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _KEY_RE.match(f"{key}:") is None:
                raise RulesIOError(f"{path}: unsupported mapping key {key!r}")
            if isinstance(item, dict | list):
                out.append(f"{pad}{key}:")
                _emit(item, indent + 2, f"{path}.{key}", out)
            else:
                out.append(f"{pad}{key}: {_emit_scalar(item, f'{path}.{key}')}")
        return
    for index, item in enumerate(value):  # list of scalars
        out.append(f"{pad}- {_emit_scalar(item, f'{path}[{index}]')}")


def dump_yaml(data: dict[str, Any]) -> str:
    """Emit a mapping in the supported subset; output re-parses identically."""
    out: list[str] = []
    _emit(data, 0, "$", out)
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Import / export
# --------------------------------------------------------------------------


def _validate_file(document: dict[str, Any], origin: str) -> dict[str, Any]:
    """Validate one parsed rule file; returns its normalized content fields."""
    unknown = set(document) - _ALLOWED_FIELDS
    if unknown:
        raise RulesIOError(f"{origin}: unknown fields: {sorted(unknown)}")
    missing = [field for field in _REQUIRED_FIELDS if field not in document]
    if missing:
        raise RulesIOError(f"{origin}: missing fields: {missing}")
    rule_key = document["rule_key"]
    if not isinstance(rule_key, str) or _RULE_KEY_RE.match(rule_key) is None:
        raise RulesIOError(f"{origin}: rule_key must match {_RULE_KEY_RE.pattern}: {rule_key!r}")
    for field in ("author", "rationale"):
        if not isinstance(document[field], str) or not document[field].strip():
            raise RulesIOError(f"{origin}: {field} must be a non-empty string")
    approved_by = document.get("approved_by")
    if approved_by is not None and (not isinstance(approved_by, str) or not approved_by.strip()):
        raise RulesIOError(f"{origin}: approved_by must be a non-empty string or null")
    enabled = document.get("enabled", False)
    if not isinstance(enabled, bool):
        raise RulesIOError(f"{origin}: enabled must be a boolean")
    if enabled and approved_by is None:
        raise RulesIOError(f"{origin}: an enabled rule requires approved_by (US-G2 approval)")
    try:
        parse_rule_definition(document["definition"])
    except RuleError as exc:
        raise RulesIOError(f"{origin}: invalid definition: {exc}") from exc
    return {
        "rule_key": rule_key,
        "author": document["author"],
        "approved_by": approved_by,
        "rationale": document["rationale"],
        "enabled": enabled,
        "definition": document["definition"],
    }


def _rule_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            found = sorted(path.glob("*.yaml"))
            if not found:
                raise RulesIOError(f"no *.yaml files in directory: {path}")
            files.extend(found)
        elif path.is_file():
            files.append(path)
        else:
            raise RulesIOError(f"no such file or directory: {path}")
    return files


def _latest_version(db: OrmSession, rule_key: str) -> CoachingRule | None:
    return db.scalars(
        select(CoachingRule)
        .where(CoachingRule.rule_key == rule_key)
        .order_by(CoachingRule.version.desc())
    ).first()


def _same_content(row: CoachingRule, content: dict[str, Any]) -> bool:
    return all(getattr(row, field) == content[field] for field in _CONTENT_FIELDS)


def import_rules(ctx: WorkerContext, paths: list[Path]) -> dict[str, int]:
    """Validate every file, then append one new version per changed rule."""
    files = _rule_files(paths)
    contents = [_validate_file(parse_yaml(f.read_text()), str(f)) for f in files]
    seen: set[str] = set()
    for content in contents:
        if content["rule_key"] in seen:
            raise RulesIOError(f"duplicate rule_key across files: {content['rule_key']}")
        seen.add(content["rule_key"])
    created = skipped = 0
    with ctx.session_factory() as db:
        for content in contents:
            latest = _latest_version(db, content["rule_key"])
            if latest is not None and _same_content(latest, content):
                skipped += 1
                continue
            version = latest.version + 1 if latest is not None else 1
            db.add(CoachingRule(version=version, **content))
            db.add(
                AuditLog(
                    actor=content["author"],
                    action="rule_version_imported",
                    entity="coaching_rule",
                    entity_id=content["rule_key"],
                    detail={"version": version, "approved_by": content["approved_by"]},
                )
            )
            created += 1
        db.commit()
    return {"created": created, "skipped": skipped}


def export_rules(ctx: WorkerContext, out: Path) -> int:
    """Write the latest version of every rule as one YAML file per rule_key."""
    out.mkdir(parents=True, exist_ok=True)
    exported = 0
    with ctx.session_factory() as db:
        rows = db.scalars(
            select(CoachingRule).order_by(CoachingRule.rule_key, CoachingRule.version)
        ).all()
        latest: dict[str, CoachingRule] = {row.rule_key: row for row in rows}
        for rule_key, row in sorted(latest.items()):
            document = {
                "rule_key": row.rule_key,
                "version": row.version,
                "author": row.author,
                "approved_by": row.approved_by,
                "enabled": row.enabled,
                "rationale": row.rationale,
                "definition": row.definition,
            }
            (out / f"{rule_key}.yaml").write_text(dump_yaml(document))
            exported += 1
    return exported


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import/export coaching rules as YAML (US-G2: rules are data)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import", help="validate files and append new rule versions")
    imp.add_argument("paths", nargs="+", type=Path, help="rule .yaml files or directories")
    exp = commands.add_parser("export", help="write the latest version of every rule")
    exp.add_argument("--out", type=Path, required=True, help="output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "import":
            counts = import_rules(WorkerContext.from_env(), args.paths)
            print(f"imported rules: created={counts['created']} skipped={counts['skipped']}")
        else:
            exported = export_rules(WorkerContext.from_env(), args.out)
            print(f"exported {exported} rules to {args.out}")
    except RulesIOError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
