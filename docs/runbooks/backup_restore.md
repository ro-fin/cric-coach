# Backup & Restore Drill (US-B6)

Covers the nightly backup manifest and the quarterly restore drill. The goal is
a provable, **bit-exact** restore: after restoring a backup, the system can
reproduce the exact manifest recorded at backup time — same row fingerprints,
same object keys, same sha256.

## What a manifest is

`POST /storage/backup/manifest` (Parent token) fingerprints the current state:

- **DB entities**: `players`, `sessions`, `session_blocks`, `videos`,
  `ball_tags` — per row, `id` plus a sha256 over every other column
  (canonical JSON). Row contents never leave the database.
- **Objects**: every key under `sessions/` in the object store.

The manifest JSON (`{sha256, entity_counts, object_count}`) is stored in the
object store at `backups/manifest-{sha12}.json` (`sha12` = first 12 hex chars
of the manifest sha256), and an `AuditLog` row with `action="backup_manifest"`
records the run. The response returns the sha256 and the manifest key.

## Nightly manifest (cron)

No scheduled job owns this yet, so record the manifest via cron on the lab box,
**after** the nightly `pg_dump` + object-store rsync complete. Wire it with
`crontab -e` alongside the [`deploy_lan.md` §5](deploy_lan.md) lines and under
the same `PATH=` header: the crontab mechanics — PATH, `mkdir -p`, sourcing
`deploy/.env` (so `$CRICAI_PARENT_TOKEN` is actually set), `flock`, the log
dir — live there as the single source, so this line follows the same shape:

```cron
30 2 * * * cd ~/cricai && mkdir -p .cricai-run && flock -n .cricai-run/backup-manifest.lock sh -c 'set -a && . deploy/.env && set +a && curl -sf -X POST http://lab.local:8000/storage/backup/manifest -H "Authorization: Bearer $CRICAI_PARENT_TOKEN"' >> ~/cricai/.cricai-run/backup_manifest.log 2>&1
```

Sourcing `deploy/.env` is not optional: under cron `$CRICAI_PARENT_TOKEN` is
otherwise empty and the POST gets a silent 401 — an unverifiable-backup
incident per the checklist below.

Order matters: dump the DB and sync `sessions/` objects to the backup target
**first**, then record the manifest so it describes what was just backed up.
Copy the `backups/` prefix to the backup target as well — the manifest must
survive loss of the primary store.

Nightly checklist:

1. `pg_dump` of the cricAI database → backup target.
2. rsync/copy of the object store `sessions/` tree → backup target.
3. `POST /storage/backup/manifest`; note the returned `sha256` + `manifest_key`.
4. Copy `backups/` prefix → backup target.
5. Confirm the `backup_manifest` audit row exists (any nightly gap is an
   incident: the backup for that day is unverifiable).

## Quarterly restore drill

Do this on a **staging** machine, never the live lab server:

1. Pick last night's manifest key from the `backup_manifest` audit trail (or
   list `backups/` on the backup target).
2. Restore the DB dump from the same night into a fresh Postgres instance.
3. Restore the object-store copy (the `sessions/` tree and `backups/` prefix)
   into a fresh storage root.
4. Start the API against the restored DB + store.
5. `POST /storage/backup/verify` with `{"manifest_key": "backups/manifest-….json"}`
   (Parent token).
6. **Expectation: `{"ok": true, "discrepancies": []}` — bit-exact.** Any
   discrepancy (missing table, row-count mismatch, missing/extra object,
   checksum mismatch) fails the drill; treat the backup pipeline as broken
   until the cause is found and a re-drill passes.
7. The drill itself is audited: verify writes an `AuditLog` row with
   `action="backup_verify"` including the discrepancy list. Keep it — it is
   the evidence the drill happened.

## Bit-exact expectation

- Row fingerprints hash **all** columns except `id`; any edited field after
  the backup shows up as a checksum mismatch even when counts match.
- `verify_restore` compares entity counts, object counts and the overall
  sha256 — an empty discrepancy list is the only passing result.
- Retention deletions between backup and drill legitimately change state; run
  drills against the restored snapshot, not the live system.

## Related protections

- Ground-truth clips (`ball_tags.ground_truth_eligible = true`) are protected
  from retention by server-derived prefixes `sessions/{session_id}/balls/{ball_no}/`
  — eval-set evidence survives retention and therefore stays restorable.
- Privacy deletions (US-L3) purge live rows/objects; their audit entries drive
  the backup-purge step so restored copies do not resurrect deleted data.
