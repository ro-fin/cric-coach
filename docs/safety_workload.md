# Workload & Safety Policy (US-H1–H5) — the AI can never talk around these

**Version:** 0.1 · **Coach sign-off:** ______ · **Parent sign-off:** ______ · Date: ______

All thresholds are configuration with audited changes; raising any ceiling requires
coach approval. Defaults below come from the backlog (ECB junior fast-bowling
directives; Cricket Australia lumbar bone-stress guidance).

## Bowling workload defaults (US-H1)

| Rule | Default |
|---|---|
| Age ≤ 11 weekly overs | 12–16 target, **16 ceiling** |
| Age 12–13 weekly overs | 16–20 target, **20 ceiling** |
| Bowling days per rolling 7 | ≤ 4 |
| Consecutive bowling days | at most once per rolling 7 |
| Mixed intensity | pace-intent weighted to ceiling; light spin counts fully (default) |

Violations: `WORKLOAD_CEILING` (hard block on planner bowling), `DAY_PATTERN_VIOLATION`
(rest/batting-only recommendation). Warning text is immutable through any LLM step
(hash-verified, US-H5).

Intensity weighting: pace-intent balls weight 1.0 and light spin balls weight 1.0 toward
the overs ceiling. Throwdowns are arm throws, not a bowling action — they are recorded in
the ledger but **weigh 0** toward the lumbar-stress overs ceiling.

## Batting volume quality split (US-H2)

Default 500-ball day: 150 technical / 150 decision / 100 match-scenario /
50 spin-specific / 50 fun. The fun block is protected: never converted to drills.

## Pain & wellness (US-H4)

Any pain report ⇒ bowling recommendations suppressed until an adult clears the flag
(logged). ≥ 2 pain reports in 14 days ⇒ escalated prominence in weekly report.
The system never diagnoses; wellness copy passes the banned-phrase lint.

## Safety supremacy (US-H5)

The Workload & Safety agent runs last in the agent pipeline; its verdicts are
appended post-LLM and checksum-verified in the final artifact. Red-team suite
(adversarial notes, rule text, player requests) gates every release: 100% blocked.

## Warning texts (immutable, hash-verified)

These are the ONLY safety warning strings the system emits. They live verbatim in code
(`cricai_coaching.safety_agent.WARNING_TEXTS`) and here; a mirror test asserts the two
never drift. They are inserted verbatim into reports/plans and SHA-256-verified — no LLM
step, session note, rule text or player request can alter, soften or remove them.

```
SAFETY - WORKLOAD CEILING: The weekly bowling overs ceiling for this age band has been reached. Bowling is stopped for the rest of this rolling 7-day window, and no drill plan may schedule more bowling until the window clears.
```

```
SAFETY - DAY PATTERN: The bowling-day pattern limit for this rolling 7-day window has been reached. Rest or batting-only practice is recommended until the pattern clears.
```

```
SAFETY - PAIN REPORTED: Pain was reported at check-in, so bowling recommendations are paused until a parent or coach clears the flag. Tell your coach or parent, and see a qualified professional if the pain persists. This system does not give medical advice.
```
