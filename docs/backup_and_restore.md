# Backup and Restore

## Service

`app/ops/backup.py` — `BackupService`

## Create

```
POST /backup/create
python -m app.cli backup create
```

Writes zip under `backups/` with:

- Non-secret config snapshot
- Exports: `daily_performance`, `portfolio_snapshots`, `configuration_history`
- Prompt files from `PROMPTS_ROOT`
- Manifest with SHA256 hashes

**Secrets are never included** (.env, tokens, credentials filtered).

## Verify

```
POST /backup/verify?path=backups/20260804T120000Z.zip
python -m app.cli backup verify --path backups/....zip
```

## Restore

`BackupService.restore(..., confirm=True)` — requires explicit confirm; refuses same-database restore without it.

## Not supported

- Hot PostgreSQL pg_dump integration
- Encrypted backups at rest
- Automated S3 upload
