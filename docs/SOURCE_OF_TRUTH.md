# Source of truth and cloud release workflow

## Repository roles

- `davidbenizri25-wq/trading-elite-system` is the private canonical source. Product changes, tests, release notes, and cloud-manifest changes start here.
- `davidbenizri25-wq/trading-autopilot-cloud` is a generated deployment mirror. Do not develop or repair product code directly in that repository.
- `deploy/cloud_manifest.txt` is the complete, explicit cloud allowlist. A file not listed there is not copied.
- `deploy/.cloud-mirror-state.json` is generated in the cloud repository. Commit it with each mirror release because it records managed-file hashes plus the exact canonical repository, commit, and version used to generate the release.

This separation keeps private notes, local data, credentials, `.git`, `.codex`, and Streamlit secrets out of the public deployment repository. Deployment secrets stay in the hosting provider's secret manager; they are never source files.

## Release a canonical change

Use two clean, separate checkouts and work on branches. Never push a release directly to `main`. Run the canonical tests, commit the canonical release branch, and leave that worktree clean before generating the public mirror; the public state records that exact commit.

```bash
CANONICAL_REPO=/absolute/path/to/trading-elite-system
CLOUD_REPO=/absolute/path/to/trading-autopilot-cloud
SOURCE_COMMIT=$(git -C "$CANONICAL_REPO" rev-parse HEAD)

python3 "$CANONICAL_REPO/tools/sync_cloud_mirror.py" \
  --target "$CLOUD_REPO" \
  --source-commit "$SOURCE_COMMIT" \
  --mode dry-run

python3 "$CANONICAL_REPO/tools/sync_cloud_mirror.py" \
  --target "$CLOUD_REPO" \
  --source-commit "$SOURCE_COMMIT" \
  --mode apply

python3 "$CANONICAL_REPO/tools/sync_cloud_mirror.py" \
  --target "$CLOUD_REPO" \
  --source-commit "$SOURCE_COMMIT" \
  --mode check
```

Then, in each repository:

1. Run the public mirror's full test suite and compile check.
2. Review `git status` and the diff. The cloud diff should contain only allowlisted files plus the generated mirror state.
3. Open the canonical pull request for the already committed source branch.
4. Commit the generated cloud branch and open its pull request, referencing the exact canonical commit and pull request.
5. Merge only after CI passes. Merge canonical first, then the cloud mirror.
6. Confirm the hosted Streamlit app is healthy on desktop and mobile before closing the release.

`--mode check` exits with status 1 if an allowlisted file differs, a previously managed file is due for removal, or the generated state is stale. Unexpected files that were never recorded as managed are preserved and must be reviewed manually.

`apply` and `check` also refuse a dirty canonical worktree, a partial SHA, or a `--source-commit` that does not exactly equal canonical `HEAD`. This prevents an uncommitted working tree from being published under a misleading commit identity.

## Safety guarantees

The sync tool:

- requires an absolute path to a separate checked-out Git target;
- requires a clean canonical Git worktree and exact full source commit for apply/check;
- copies regular files only from the explicit allowlist;
- rejects path traversal, target-directory symlink escapes, `.git`, credential-like files, environment files, and Streamlit secrets;
- never deletes an untracked or unmanaged target file;
- refuses to guess ownership when the generated state is corrupt;
- writes copied files and mirror state atomically.

The private `config/risk_config.json` is intentionally not allowlisted. The public mirror receives fail-closed zero defaults from `risk_config.py`, while the canonical checkout can load private limits locally. Persistent app state is also opt-in: set both `APP_ACCESS_CODE` and `AUTOPILOT_PRIVATE_STATE_ENABLED=true` in the private deployment secret manager. Without both values, all visitor state remains session-only even if state-path variables are present.

On the first migration from a manually maintained cloud repository, no prior state exists, so the tool deletes nothing. Review any legacy extra files explicitly; after the first generated state is committed, future removals are deterministic.

## Rollback

Before applying a cloud release, record the current cloud `origin/main` commit:

```bash
git -C "$CLOUD_REPO" fetch origin main
git -C "$CLOUD_REPO" rev-parse origin/main
```

If production regresses, revert the cloud mirror release commit on a new branch, run CI, and merge the rollback through a pull request. Redeploy that resulting `main`; do not force-push or reset the shared branch.

If the canonical change is also wrong, revert it separately in the private repository through its own pull request. Then rerun the mirror tool from the corrected canonical `main` and publish a fresh cloud mirror release. This preserves both repositories' audit trails and keeps the public code derived from the private source.
