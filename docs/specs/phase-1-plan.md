# Phase 1 Plan — Capture & Ingestion Core

**Stories:** US-A1–A5 (software surfaces), US-B1–B4, US-B6, US-L3 · **Branch:** `phase-1-ingestion`

## Foundation (built first, single hand — everything else depends on it)

1. **Settings** (`cricai_api.settings`): env-driven (DB URL, storage root/S3 endpoint,
   auth tokens), LAN-local defaults (US-L3).
2. **DB schema** (`cricai_data.models`, SQLAlchemy 2 typed ORM) — complete Phase 1 schema
   in one Alembic migration:
   - `players` (id, name, birthdate, handedness, is_guest)
   - `users` (id, name, role: parent|coach|player, token_hash) — LAN token auth
   - `camera_configs` (camera_id C1–C8, position_label, xyz_offset_m, height_m, fps,
     resolution, lens, mount, protected, era_started_at, active)
   - `sessions` (id uuid, player_id, date, session_type, bowler_source, machine_settings
     jsonb, notes, state: created|recording|captured|processing|analyzed|failed,
     degraded bool, missing_views jsonb, checklist_ack_id nullable, created_at)
   - `session_blocks` (id, session_id, block_no, start_ts, end_ts nullable, bowler_source,
     machine_settings jsonb, intent)
   - `videos` (id, session_id, camera_id, object_key, filename, checksum_sha256, size,
     fps, resolution, codec, duration_s, status: pending|uploaded|probed|failed|
     metadata_conflict, probe jsonb, created_at; unique (session_id, checksum))
   - `upload_parts` (upload_id, part_no, size, checksum, complete) — resumable uploads
   - `ball_tags` (id, session_id, ball_no, block_id nullable, line, length, shot, footwork,
     contact, outcome, control, source='manual', ground_truth_eligible bool default true)
   - `tag_audits` (id, tag_id, actor_user_id, field, old, new, at)
   - `checklist_acks` (id, session_id nullable until start, items jsonb, acked_by, at)
   - `health_checks` (id, session_id nullable, at, passed, results jsonb)
   - `audit_log` (id, actor, action, entity, entity_id, detail jsonb, at) — deletions,
     retention, share/export watermarks (L3)
3. **Storage adapter** (`cricai_data.storage`): `ObjectStore` protocol — `put/get/delete/
   exists/list, multipart begin/put_part/complete/abort` with `FsObjectStore` (dev/test/
   LAN default) and key layout `sessions/{session_id}/{camera_id}/{filename}`.
4. **Session state machine** (`cricai_data.lifecycle`): created→recording→captured→
   processing→analyzed, failure edges, degraded flag rules, idempotent start.
5. **Auth/RBAC** (`cricai_api.auth`): bearer token → user; role dependencies
   (parent=admin, coach=review, player=read-own age-appropriate); guest segregation.
6. **Test infra**: temp-Postgres cluster fixture (initdb/pg_ctl on scratch dir; CI uses
   service container via `CRICAI_TEST_DATABASE_URL`), truncation between tests,
   TestClient app factory with role-token helpers.
7. **US-B1 sessions router** implemented as the exemplar (validation 422s, pagination).

## Story fan-out (parallel worktree agents; disjoint files; no migration edits)

| Agent | Story | Files (new only) |
|---|---|---|
| a1 | US-A1 camera registry CRUD + FOV record refs | `routers/cameras.py`, tests |
| a2 | US-A2 metadata completeness validator + sync-offset calculator + flicker FFT check | `cricai_vision.capture_qc`, tests |
| a3 | US-A3 start/stop lifecycle endpoints (atomic stop, checksums, degraded, idempotent) | `routers/lifecycle.py`, tests |
| a4 | US-A4 health-check runner (simulated device probes, disk estimator) | `routers/health_checks.py`, `cricai_data.healthcheck`, tests |
| a5 | US-A5 checklist gating (machine session cannot start unacked) | `routers/checklists.py`, tests |
| b2 | US-B2 resumable video upload (parts, checksum verify, ffprobe adapter, dedupe, metadata_conflict) | `routers/videos.py`, `cricai_data.probe`, tests |
| b3 | US-B3 blocks CRUD (no overlap, gaps reported, ball→block assignment) | `routers/blocks.py`, tests |
| b4 | US-B4 manual tagging (audit trail, export/import JSON/CSV round-trip) | `routers/tags.py`, tests |
| b6 | US-B6 retention/backup (retention selector, never deletes ground-truth clips, forecast, backup manifest + restore) | `cricai_data.retention`, `routers/storage_admin.py`, tests |
| l3 | US-L3 deletion round-trip (video+clips+metrics+backups), share watermark log, egress allow-list module | `cricai_api.egress`, `routers/privacy.py`, tests |

Rules for agents: 100% line+branch coverage on owned files; ruff+mypy clean; SAF/SEC
tests where the story lists them; no edits to shared foundation files (request changes
via notes if the foundation is insufficient — reviewed and applied centrally).

## Definition of done for the phase

`make check` + `pytest -m integration` + `pytest -m safety` green; adversarial review
workflow findings fixed; PR merged with CI green; B5 (dashboard) intentionally deferred
to Phase 6 with the rest of Epic K so the UI is built once against stable APIs.
