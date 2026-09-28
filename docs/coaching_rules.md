# Coaching Rules DSL (US-G2)

Rules are data, not code: versioned `coaching_rules` rows with
`id, author, approved_by, version, rationale`, imported/exported as YAML by
`scripts/rules_io.py`. Every rule text passes the safety content lint; free
prose never originates in findings — findings carry only rule-authored
`text_data` strings (pinned contract #2 in `docs/specs/phase-5-plan.md`).

## Rule definition shape

A rule `definition` is JSON validated by
`cricai_coaching.rules.parse_rule_definition`:

```json
{
  "metric": "head_stability_score",
  "op": "lt",
  "value": 0.6,
  "condition": {"line": ["outside_off"], "length": ["full", "good"]},
  "min_n": 10,
  "severity": "major",
  "trigger_share": 0.5,
  "text_data": {"finding": "…", "correction": "…", "cue": "…"}
}
```

- **metric** — a canonical BallRecord field (US-G1). Numeric metrics
  (`head_stability_score`, `front_foot_direction_cm`, `speed_kph`) take the
  ordering ops `lt|le|gt|ge`; categorical metrics (`line`, `length`, `shot`,
  `footwork`, `outcome`, `bowler`, `mode`, `bat_path`, `contact_quality`) and
  the boolean `control` take only `eq` (float equality is a footgun, so `eq`
  on numeric metrics is rejected).
- **condition** — zone/context filters: `{field: [allowed values]}` over the
  categorical/boolean BallRecord fields. Values are validated against the
  canonical vocabularies (`cricai_data.enums`); `bat_path`/`contact_quality`
  accept any non-empty string because producer versions may extend their
  classes. A ball with a null field never matches a filter on it.
- **Semantics** — a ball *matches* when it passes every condition filter and
  its metric is non-null; a matched ball is a *hit* when `metric OP value`
  holds per ball. The rule fires only when matches ≥ `min_n` (minimum-sample
  gate, default 10 — no conclusions from 3 balls) AND hit share ≥
  `trigger_share` (default 0.5).
- **severity** — `info | minor | major` (`FindingSeverity`), used by the
  report ranking (US-G3).
- **text_data** — the ONLY place kid-facing prose may originate. Every string
  is content-linted at parse time (banned-phrase list: no shaming, no
  "you always/never", no medical diagnosis language) and the SAF suite lints
  every seed file in CI.

Fired rules emit Finding-shaped dicts (pinned contract #2): `finding_id`,
`agent`, `rule_key`, `kind`, `severity`, `metric`, `condition`, `n` (matched
count — the honest denominator), `effect_size` (hit share), `confidence`
(mean stored metric confidence over matches), the exact `ball_ids` of the
hits, `evidence` (`{ball_no: {camera_id: clip_id}}`), `text_data`, and a
`payload` with `op/threshold/aggregate/hits/share/min_n/trigger_share/
rule_version` so every later claim is recomputable (US-G3).

## Per-player overrides

`rule_overrides` rows disable or re-parameterize one rule for one player,
with reason and actor logged (plus an `audit_log` row):

- `action: "disable"` — the rule never runs for this player.
- `action: "adjust"` — `params` (e.g. `{"value": 0.5, "min_n": 15}`) are
  merged over the rule's definition in creation order; the merged definition
  is re-validated, so an override can never produce a malformed rule.

API: `POST /rules/{rule_key}/overrides`, `GET /rules/overrides?player_id=…`.

## Authoring workflow

1. **Author** writes `<rule_key>.yaml` (see `packages/data/rules/*.yaml` for
   the shape: `rule_key`, `author`, `approved_by`, `enabled`, `rationale`,
   `definition`). The dialect is a strict YAML subset: two-space indents,
   block sequences of scalars, JSON scalars or bare strings, full-line `#`
   comments.
2. **Import** with `uv run scripts/rules_io.py import <file-or-dir>`. Every
   file is validated (DSL + content lint) before anything is written; any
   invalid file aborts the whole import (exit 2).
3. **Versioning is append-only**: importing changed content (or `POST /rules`)
   creates version N+1; history is never rewritten. Re-importing identical
   content is a no-op, so `export` → `import` round-trips.
4. **Approval**: a version can only be `enabled` when `approved_by` is
   non-null (file-level check, API `POST /rules/{rule_key}/enable` returns
   409 otherwise, and the runner independently skips unapproved rules).
   `disable` retires a rule without rewriting history.
5. **Export** with `uv run scripts/rules_io.py export --out DIR` (latest
   version per rule) for review or backup.
