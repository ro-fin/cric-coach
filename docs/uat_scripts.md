# UAT Scripts (T6 — per release)

Scripted acceptance tasks for the three personas, exactly as the backlog's T6 defines
them (`cricket-ai-coach-user-stories.md` §T6). Run them on the deployed LAN stack
after every release, before sign-off. Every step below names the real shipped
surface (web route, API endpoint, script, or runbook) — nothing here is aspirational.

**A task passes only if the persona completes it unaided** (the facilitator may set
up preconditions, marked *Setup*, but must not steer during the task).

## Conventions

- `WEB` = the Next.js dashboard origin (e.g. `http://<lab-host>:3000`); `API` = the
  FastAPI origin (e.g. `http://<lab-host>:8000`).
- Roles are LAN bearer tokens (US-L3, `cricai_api.auth`): Parent (admin), Coach
  (review), Player (read-only). `curl` steps show the role whose token they need.
- Preconditions for all scripts: the stack is up (Phase 7 `scripts/deploy_local.sh`
  or `deploy/docker-compose.yaml`, verified by `scripts/verify_deploy.py`) and at
  least one processed session exists with a PUBLISHED daily report.
- Record outcomes in the table at the bottom; UAT results feed the release
  checklist (`docs/release_checklist.md`).

---

## Player script (UAT-P)

Persona: the 11-year-old. Device: the lab tablet/laptop, signed in at `WEB/login` as
**Player** with the Player token (Phase 8: the token lives in an httpOnly cookie on the
device, never in the page; `NEXT_PUBLIC_API_TOKEN` no longer exists).

Automated twin: UAT-P1, UAT-P2, the UAT-PA degraded-session check and UAT-C2 also run
in Chromium on a tablet and a desktop viewport (`make web-e2e`, `apps/web/e2e`) against
the seeded demo lab. The scripts below stay the human pass on real footage.

### UAT-P1 — Find your best cover drive (US-B5 / US-K1)

*Setup:* open `WEB/sessions` (today's session must contain at least one tagged
cover drive).

1. Player picks today's session from the list (`apps/web/app/sessions`,
   filterable by date/type) and opens it → `WEB/sessions/{id}`.
2. In the **Filters** bar (`TimelineFilters`), player sets **Shot** =
   `cover_drive` (the pinned tag vocabulary, `cricai_data.enums`).
3. Player clicks ball chips on the timeline (`BallTimeline`); each click seeks
   every camera in the multi-cam player (`MultiCamPlayer`).
4. Player frame-steps (±1 frame controls) around contact on the ball they judge
   their best, and replays it.

**Pass:** the player finds and rewatches their chosen cover drive unaided (US-B5
UAT AC), in under 2 minutes.

### UAT-P2 — Read today's report aloud (US-G3 / US-K5)

*Setup:* the player opens **Today** (`WEB/`), which shows the day's PUBLISHED report,
or `WEB/reports` and picks it from the list (Phase 8; newest first, kind filter).
Drafts held by the review gate are never shown to the Player role.

1. Player reads the whole report aloud from `ReportView`: the positive, the ONE
   correction, the drill, the measurable goal, and any honesty banner.
2. Player plays at least one evidence clip linked in the report (US-G6).

**Pass:** player reads it without stumbling on jargon and can say, in their own
words, what the one correction is.

### UAT-P3 — State tomorrow's drill (US-G3 / US-J3)

1. Immediately after UAT-P2, ask: *"What drill are you doing tomorrow, and what's
   the target?"* Player answers from memory of the report.
2. Facilitator cross-checks against the plan:
   `GET API/drills/plans/{player_id}/{tomorrow}` (any role) — or the drill block in
   the report body if no plan row exists for tomorrow yet.

**Pass:** the stated drill and its measurable goal match the report/plan verbatim
in substance (reps/target may be paraphrased, not invented).

### UAT-P4 — Interpret your own pitch map (US-C6 / US-K2)

*Setup:* open `WEB/pitchmap/{sessionId}` for today's session.

1. Ask the player: *"Where did most balls land?"* — player answers from the zone
   table / heat cells (`ZoneTable`, `PitchMapSvg`).
2. Ask: *"Show me the balls in your weakest zone."* — player clicks that cell and
   the ball list (`BallList`) opens the underlying balls.

**Pass:** both answers are consistent with the zone counts on screen (facilitator
verifies against `GET API/sessions/{id}/heatmap`).

### UAT-P5 — Interpret the release scatter summary (US-I3, V3 surface)

*Setup:* a bowling-block session with a PUBLISHED report; open it at
`WEB/reports?report_id=<id>`. The **Release scatter** section (`ReportView`,
`data-testid="bowling-scatter"`) shows n measured, mean, sigma, range, and
(when numbers exist) per-variation cells.

> **Deploy caveat — read before running.** On any current deployment the
> release-height seam is unwired (US-I3 is shipped-partial; see register R3 in
> `docs/release_checklist.md`), so this section renders **n=0 · mean — · sigma —**
> with no per-variation cells: the dashes are the *honest empty* state, not a
> bug. Until R3 lands numeric heights on the rig, the pass criterion is that the
> player reads that empty state correctly. Run the sigma-comparison questions
> (2–3 below) ONLY once the scatter is carrying numbers.

1. Ask: *"What is this section telling you right now?"* — with n=0 the honest
   answer is *"it hasn't measured any release heights yet"* (dashes, not zeroes).
2. *(R3-gated)* Ask: *"What does the sigma number mean about your release?"*
3. *(R3-gated)* Ask: *"Which variation is your release most consistent for?"* —
   player answers from the per-variation cells.

**Pass (today, n=0):** player reads the empty scatter honestly — "no release
heights measured yet" — and does NOT invent a value from the dashes.
**Pass (post-R3, numbers present):** player explains scatter = "how much my
release point moves around" and picks the variation with the smallest sigma.

---

## Parent script (UAT-PA)

Persona: the Parent as lab operator. Needs the Parent token.

### UAT-PA1 — Rig from the doc (deployment)

*Setup:* a clean machine (or clean checkout) and the printed runbook only.

1. Parent follows `docs/runbooks/deploy_lan.md` (Phase 7) end-to-end: prerequisites,
   configuration, `scripts/deploy_local.sh` (the verified process-based path;
   `deploy/docker-compose.yaml` is the authored container path), scheduled jobs
   (`scripts/run_scheduled.py` + the runbook's crontab lines).
2. Parent runs the black-box smoke: `uv run scripts/verify_deploy.py`.

**Pass:** `verify_deploy.py` exits 0 (health, auth, session lifecycle, report
fetch, dashboard reachable) with the developer watching only.

### UAT-PA2 — Session start → report walkthrough (US-B1/B2/A3/L1/G3)

All against `API` with the Parent token. The exact `curl` bodies for every step
are inline below (this script is the source of them — nothing to look up
elsewhere). Set `API` and the token first; substitute the ids the earlier calls
return:

```bash
API=http://lab.local:8000
AUTH=(-H "Authorization: Bearer $CRICAI_PARENT_TOKEN" -H "Content-Type: application/json")

# 1. Register/confirm cameras (GET to confirm; POST once per rig).
curl -s "${AUTH[@]}" "$API/cameras"
curl -s "${AUTH[@]}" -X POST "$API/cameras" -d '{
  "camera_id": "C1", "position_label": "behind bowler",
  "xyz_offset_m": {"x": 0.0, "y": -3.0, "z": 0.0},
  "height_m": 1.6, "fps": 30, "resolution": "1280x720"}'

# 2. Create the session (session_type: batting|mixed|bowling; the date field is
#    named "date"; a machine session MUST declare machine_settings).
curl -s "${AUTH[@]}" -X POST "$API/sessions" -d '{
  "player_id": "<PLAYER_ID>", "date": "2026-07-13",
  "session_type": "batting", "bowler_source": "machine",
  "machine_settings": {"speed_kph": 85.0, "length": "good"}}'

# 3. Ack the machine safety checklist (machine sessions only, US-A5): read the
#    items, then ack each id true.
curl -s "${AUTH[@]}" "$API/checklists/machine"
curl -s "${AUTH[@]}" -X POST "$API/sessions/<SESSION_ID>/checklist-ack" -d '{
  "items": {"<ITEM_ID_1>": true, "<ITEM_ID_2>": true}, "acked_by": "parent"}'

# 4. Start as one unit (validates the cameras against the registry).
curl -s "${AUTH[@]}" -X POST "$API/sessions/<SESSION_ID>/start" -d '{"cameras": ["C1"]}'

# 5. Upload each camera file (checksum-verified 3-step multipart, US-B2).
CHK=$(sha256sum c1.mp4 | cut -d' ' -f1); SIZE=$(wc -c < c1.mp4)
curl -s "${AUTH[@]}" -X POST "$API/sessions/<SESSION_ID>/videos/uploads" -d "{
  \"camera_id\": \"C1\", \"filename\": \"c1.mp4\",
  \"declared_checksum\": \"$CHK\", \"declared_size\": $SIZE}"
# the response gives upload_id + store_upload_id; PUT the bytes as part 1:
curl -s -H "Authorization: Bearer $CRICAI_PARENT_TOKEN" \
  -X PUT "$API/videos/uploads/<UPLOAD_ID>/parts/1?store_upload_id=<STORE_UPLOAD_ID>" \
  --data-binary @c1.mp4
curl -s "${AUTH[@]}" -X POST "$API/videos/uploads/<UPLOAD_ID>/complete" -d '{
  "store_upload_id": "<STORE_UPLOAD_ID>", "part_count": 1}'

# 6. Stop — note the server-computed verdict (complete vs degraded, US-A3).
curl -s "${AUTH[@]}" -X POST "$API/sessions/<SESSION_ID>/stop" -d '{}'

# 7. Run the pipeline (Parent-only), then poll until it completes.
curl -s "${AUTH[@]}" -X POST "$API/pipeline/sessions/<SESSION_ID>/runs" -d '{}'
curl -s "${AUTH[@]}" "$API/pipeline/runs/<RUN_ID>"

# 8. Fetch the report, read the HTML, then publish (a 409 with reasons is the
#    publish gate working, not a failure of this step).
curl -s "${AUTH[@]}" "$API/reports?player_id=<PLAYER_ID>&kind=daily"
curl -s "${AUTH[@]}" "$API/reports/<REPORT_ID>/html"
curl -s "${AUTH[@]}" -X POST "$API/reports/<REPORT_ID>/publish" -d '{}'
```

**Pass:** parent reaches a PUBLISHED (or honestly gate-BLOCKED-with-reasons) report
from a fresh session without developer keyboard input.

### UAT-PA3 — The five dashboard questions (US-K4 / US-B6)

*Setup:* open `WEB/progress?player=<player_id>` (Parent view; a kid-mode toggle on
the page switches to the celebratory view). Answer using ONLY
this screen. The five questions (derived from the US-K4 "is this working, and is
he safe?" ACs and the US-B6 forecast AC):

1. **Is he safe to bowl this week?** — workload panel (`WorkloadPanel`, never
   below the fold): rolling-7-day overs vs ceiling and bowling-day pattern.
2. **Any wellness flags right now?** — wellness flags in the same panel.
3. **Is the training working?** — trend charts (`TrendChart`) with n-values and
   confidence shading; regressions only inside a coaching frame.
4. **What changed this month?** — weekly/monthly report cards (`ReportCard`):
   period, positive line, milestones (US-G5).
5. **Can I trust these numbers today?** — honesty banner on the report card
   and the suspect-data exclusion note above the trends (US-L4/US-C4).

**Pass:** all five answered from the dashboard alone, and each answer matches the
API numbers (facilitator spot-checks `GET API/workload/players/{id}/summary` and
`GET API/wellness/{id}/state` — US-K4 data-parity).

### UAT-PA4 — Restore drill, developer watching only (US-B6)

1. Parent performs the quarterly drill in `docs/runbooks/backup_restore.md`
   §"Quarterly restore drill", steps 1–7, on a staging machine: pick last night's
   manifest, restore DB dump + object store, start the API against the restored
   pair, then `POST API/storage/backup/verify` with the manifest key (Parent token).
2. Expectation per the runbook: `{"ok": true, "discrepancies": []}` — bit-exact.

**Pass:** verify returns ok with zero discrepancies and the audit row
(`action="backup_verify"`) exists; developer never touches the keyboard.

---

## Coach script (UAT-C)

Persona: the Coach. Needs the Coach token.

### UAT-C1 — Author a rule (US-G2)

1. Coach writes a rule YAML (fields per `docs/coaching_rules.md`: `rule_key`,
   `author`, `rationale`, `definition`; DSL validated by
   `cricai_coaching.rules.parse_rule_definition`).
2. Import it: `uv run scripts/rules_io.py import my_rule.yaml` — invalid files
   abort loudly (exit 2) before anything is written.
3. Confirm it landed: `GET API/rules/{rule_key}`, then dry-run it on a processed
   session: `POST API/rules/run/{session_id}`.

**Pass:** the rule imports, is listed enabled, and its dry-run output matches the
coach's intent on a session they know.

### UAT-C2 — Veto a finding (US-G6 / US-J5)

1. Coach opens a report (`GET API/reports/{id}/html` or `WEB/reports?report_id=`),
   picks a finding whose evidence clips they disagree with, and records the veto:
   `POST API/reports/findings/{finding_id}/verdicts` with
   `{"verdict": "not_supported", "note": "<why>"}` (Coach-only).
2. Confirm it counts: `GET API/reports/verdicts/analytics` — rules with high
   not-supported rates get flagged for review.
3. Where the review gate is on, held drafts are decided in the coach review queue:
   `WEB/review` over `GET API/settings/review-queue` with publish / block actions
   (`POST API/reports/{id}/publish`; Phase 7 surface). A gate-BLOCKED publish (409)
   drops the report OUT of the queue (it lists DRAFT reports only): the block
   reasons persist in the `report_publish_blocked` audit rows, and the report stays
   readable to coach/parent at `GET API/reports/{id}` (and `WEB/reports?report_id=`);
   republish after fixing the cause is `POST API/reports/{id}/publish` again, not
   the queue.

**Pass:** the verdict is stored (visible via
`GET API/reports/findings/{finding_id}/verdicts`) and the analytics tally moved.

### UAT-C3 — Edit a plan (US-J3 / US-H5)

1. Coach fetches tomorrow's plan: `GET API/drills/plans/{player_id}/{date}`
   (generate first via `POST API/drills/plans/{player_id}/{date}/generate` if none).
2. Coach edits it: `PUT API/drills/plans/{player_id}/{date}` (Coach-only) — swap a
   drill, adjust reps.
3. Safety supremacy check, deliberately: coach re-submits the plan with a bowling
   block while a workload ceiling or uncleared pain flag is active for the player.
   Expected: **422, plan rejected, never stored** (the client's safety verdict is
   ignored; the server recomputes it — US-H5).

**Pass:** the honest edit persists (re-GET shows it, audit row `plan_edit` exists);
the unsafe edit is rejected with a clear violation message.

### UAT-C4 — Sign off metric definitions (US-E2/E3/I2 / US-L2)

1. Coach reads `docs/coaching_metrics.md` (every metric: name, unit, landmarks,
   frame window, formula, nullable-with-reason conditions).
2. Coach cross-checks at least two metrics against a rendered report's numbers for
   a session they attended (`GET API/sessions/{id}/balls/{ball}/metrics`).
3. Coach fills the **Coach sign-off** + date line at the top of the doc; the change
   is committed (the doc is versioned with the repo).

**Pass:** sign-off line is filled and committed; any definition the coach disputes
is logged as an issue before release.

### UAT-C5 — Review a month of trends vs. own notes (US-G5 / US-K3 / US-K4)

1. Coach opens `WEB/progress?player=<player_id>` and reads the monthly report card
   and trend charts for the last month (backed by `cricai_worker.rollup_reports`).
2. Coach pulls up their own notes for the same period: `WEB/notes?player_id=` or
   `GET API/notes?player_id=<id>&q=<term>` (searchable, author-attributed).
3. For each monthly trend, coach states agree / disagree vs their notes.
   Disagreements are recorded as new coach notes (`POST API/notes`) so the next
   rollup review has them.

**Pass:** coach completes the agree/disagree pass; every disagreement has a stored
note; nothing on the dashboard contradicts a number the coach can check.

---

## Known surface gaps (honest, not steps)

These are the places where a T6 task needs facilitator setup because a UI surface
does not exist; they are recorded in the release inventory
(`cricai_data.release_manifest`) and the release checklist:

- ~~No report-list UI~~ **closed in Phase 8**: `WEB/reports` lists the player's reports
  (reviewers also see drafts and blocked); `?report_id=` still opens one directly.
- **Uploads are still API-only**: Phase 8 added `WEB/sessions/new` (create → cameras →
  machine safety checklist → start/stop, resumable after a tablet reload), but the
  per-camera video upload (US-B2 multipart) has no UI yet, so UAT-PA2 steps 5–8 stay
  `curl`-based with the request bodies inline above.
- **Event corrections have no drag UI** (US-D4 residue): correction UAT is not in
  this release's scripts; corrections run through `API/sessions/{id}/events/*`.
- **A gate-BLOCKED report leaves the web review queue** (the queue lists DRAFT
  reports only): its reasons live in the `report_publish_blocked` audit rows, the
  report is still readable at `GET API/reports/{id}`, and republish is
  `POST API/reports/{id}/publish` (curl), not a queue action.
- **Notes are text-only** (US-K3 voice residue).

## Results log

| Date | Release/commit | Task | Persona | Pass/Fail | Notes |
|---|---|---|---|---|---|
| | | | | | |
