# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A generic container entrypoint wrapper (`init.py`, single file, stdlib `urllib` + PyYAML, no Azure SDK): fetches secrets from Azure Key Vault per a small YAML manifest, then either sets them as env vars and `exec`s the real app command (**exec mode**) or writes them to a dotenv-style file and exits (**sidecar mode**, for a one-shot init container alongside an app whose image shouldn't need Python). See `README.md` for the manifest format and both modes' usage; both are covered in detail in `init.py`'s own module docstring.

## Development workflow (non-negotiable)

This repo deploys itself: a push to `main` auto-versions, builds a Docker image, and publishes it to GHCR (`ghcr.io/mf808/container-init`), which other repos' `deploy/docker-compose.yaml` sidecar services and Dockerfile `ADD` lines pin to a specific tag. To keep `main` — and therefore `latest` — always safe to consume, **every change goes through a gated PR. Never push to `main` directly** (branch protection enforces this server-side too, admins included).

For any code change:
1. Branch off `main`: `git switch -c <type>/<slug>`.
2. Implement, then run `python -m pytest` locally.
3. Push the branch and open a PR whose **title is a Conventional Commit** — this drives the version bump: `fix:` → patch, `feat:` → minor, `feat!:` / `BREAKING CHANGE` → major.
4. `.github/workflows/ci.yml` runs the suite on the PR; branch protection blocks merge until it is green.
5. **Squash-merge.** `.github/workflows/release.yml` then tags `vX.Y.Z`, builds, and pushes `ghcr.io/mf808/container-init:vX.Y.Z` + `:latest`, and cuts a GitHub Release.

Dependabot PRs are fully hands-off: `.github/workflows/auto-merge.yml` enables auto-merge for **every** bump (any ecosystem, any level — patch/minor/major), authenticated with the `AUTOMERGE_TOKEN` repo secret (a real personal token, not `GITHUB_TOKEN` — see comment in that workflow for why: bot-authored merges don't trigger `on: push` workflows, which would silently stop releases from being cut). The CI gate — the full test suite in `tests/` — is the sole decider. A 7-day Dependabot cooldown (`.github/dependabot.yml`) is the only guard against a freshly-published malicious/broken release, since automerge is total. Versions are derived automatically from commit messages — write Conventional-Commit PR titles and **never hand-edit a version number**.

**Consuming repos pin an exact tag** (image reference or `ADD .../vX.Y.Z/init.py`), never `latest`/a branch — that's the whole point of tagging here. When this repo cuts a new version that consuming repos should adopt, bump the reference in each of their compose files / Dockerfiles / READMEs as a separate, deliberate step (grep for the old tag across `~/dev/*`), not automatically.

**AUTOMERGE_TOKEN care:** it's a real personal token with `repo`+`workflow` scope, stored as a repo secret so Dependabot merges actually cascade into releases. If it expires or is rotated, regenerate via `gh auth token` (or a dedicated fine-grained PAT) and `gh secret set AUTOMERGE_TOKEN`.

## Tests

`python -m pytest` runs `tests/` — deterministic, no real network calls (Azure AD/Key Vault HTTP is monkeypatched at the `urllib.request.urlopen` boundary via `FakeAzure` in `conftest.py`, so `init.py`'s own URL-building and error-handling code still runs for real). Structure:
- `tests/test_parse_args.py` — exec vs. sidecar mode detection, `SECRETS_MANIFEST` env fallback, missing-argument exits.
- `tests/test_fetch_all.py` — the Azure fetch path: skips cleanly with no `AZURE_CLIENT_SECRET` (local dev), splits `env:`/`file:` targets correctly, exits on a malformed entry or an Azure HTTP error.
- `tests/test_write_and_quote.py` — `_dotenv_quote` escaping, `_write_file_target` perm/parent-dir handling.
- `tests/test_main_sidecar.py` / `tests/test_main_exec.py` — full `main()` integration per mode (`os.execvpe` is monkeypatched in the exec tests so the test process itself isn't replaced).

**`test_main_sidecar.py`/`test_main_exec.py` together are the permanent regression test for the v1.2.1 file-permission bug**: v1.2.0 wrote every sidecar-mode output file as `0600`, unreadable by a consuming app container running as a different, non-root user — the app silently booted with no secrets. The fix (`init.py`'s `file_mode = 0o644 if mode == "sidecar" else 0o600`) is asserted directly on file permissions in both test files; don't let a future change relax that assertion without understanding why it's there.

## Architecture

Single file, deliberately: `init.py`'s docstring is the primary spec (manifest format, both modes' exact CLI shape). `_parse_args` distinguishes modes by presence of `--` (exec) vs `--out <path>` (sidecar) in argv. `_fetch_all` is gated on `AZURE_CLIENT_SECRET` being set, not on manifest contents — so the same image + manifest runs unmodified in local dev (env var unset: skip the vault, one stderr notice, no error) and production (env var set: fetch for real). `main()` writes `file:` targets first (mode depends on exec vs. sidecar), then either `execvpe`s the real command with `env:` targets merged into the environment (exec) or writes an `env:`-targets dotenv file and returns (sidecar).
