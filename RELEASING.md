# Release flow — `mcp-server-odoo-ei`

How to cut a new release of this fork to PyPI.

## TL;DR

1. Bump `version` in `pyproject.toml`.
2. Move the `[Unreleased]` block in `CHANGELOG.md` under a dated `[X.Y.Z]` heading.
3. Commit on `main`, tag `vX.Y.Z`, push tag, **then create the GitHub release**.
4. The GitHub release triggers `.github/workflows/publish.yml` → uploads to PyPI.

Pushing only the tag does **not** publish — `publish.yml` listens on
`release: published`. Use this on purpose to stage a tag without publishing.

## Pre-flight

```powershell
# clean tree, on main, in sync
git status
git checkout main
git pull --ff-only origin main

# tests must pass with the merge venv (or any clean venv)
.\.venv-merge\Scripts\python.exe -m pytest -m "not yolo and not mcp" -q

# format + lint must match CI
.\.venv-merge\Scripts\python.exe -m ruff format --check .
.\.venv-merge\Scripts\python.exe -m ruff check .
```

If pre-commit is installed it runs `ruff-format` + `ruff` automatically on
commit, mirroring CI. **Do not add black** — it disagrees with `ruff format`
on `assert x, msg` and will fight CI.

## Versioning

- We are a **fork**: ahead of upstream `ivnvxd/mcp-server-odoo`. Pick a version
  greater than both upstream's latest and our last release.
- Upstream's tags (`v0.5.x`, `v0.6.0`) get fetched into the local repo when
  `git fetch upstream` runs. Those tags **do not** exist on our `origin`. If
  one collides with the version you want, delete it locally before tagging:
  `git tag -d vX.Y.Z`.
- Patch bumps (`X.Y.Z+1`) for fixes; minor (`X.Y+1.0`) for new tools/features.

## Steps

```powershell
# 1. bump
# Edit pyproject.toml: version = "X.Y.Z"
# Edit CHANGELOG.md: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
#   and add a fresh empty `## [Unreleased]` on top.

# 2. commit
git add pyproject.toml CHANGELOG.md
git commit -m "chore: release vX.Y.Z"
git push origin main

# 3. tag (annotated)
git tag -a vX.Y.Z -m "Release X.Y.Z — <one-line summary>"
git push origin vX.Y.Z

# 4. GitHub release → triggers PyPI publish
gh release create vX.Y.Z `
  --title "vX.Y.Z — <one-line summary>" `
  --notes-file release-notes-X.Y.Z.md  # or paste inline with --notes
```

`release-notes-*.md` is throwaway — copy the relevant section of CHANGELOG.

## Verifying the publish

```powershell
gh run list --workflow=publish.yml --limit 1
gh run watch <run-id> --exit-status

# After ~30s the package shows up
uvx --refresh mcp-server-odoo-ei --version
```

## Aborting a release before it publishes

If you've pushed a tag but **not yet** created the GitHub release, nothing
publishes — `publish.yml` listens on `release: published`. Safe to delete the
tag and revert the bump:

```powershell
git push --delete origin vX.Y.Z
git tag -d vX.Y.Z
git revert <bump-commit-sha>
git push origin main
```

If you've already created the GitHub release and the workflow ran:
- A successful PyPI upload is irreversible — you can only **yank** the version
  on https://pypi.org/manage/project/mcp-server-odoo-ei/. Yanked versions are
  hidden from `pip install mcp-server-odoo-ei` but stay reachable for
  `==X.Y.Z` pins. Then bump and release the next patch with the fix.
- A failed workflow run can simply be re-tried after fixing whatever broke.

## Auth

`publish.yml` authenticates to PyPI via `secrets.PYPI_API_TOKEN`. The token
lives in repo settings → Secrets → Actions. PyPI suggests migrating to
**Trusted Publishers** (OIDC) — open ticket, low priority.
