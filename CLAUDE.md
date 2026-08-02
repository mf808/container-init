# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A generic container entrypoint wrapper (`init.py`, single file, stdlib `urllib` + `signal`/`threading` + PyYAML, no Azure SDK): fetches secrets from Azure Key Vault per a small YAML manifest, then either sets them as env vars and `exec`s the real app command (**exec mode**, one-shot) or keeps re-fetching on an interval and writing them to a dotenv-style file (**sidecar mode**, a long-lived daemon alongside an app whose image shouldn't need Python — since v2.0.0 it never exits on its own, in the style of tools like External Secrets Operator). See `README.md` for the manifest format and both modes' usage; both are covered in detail in `init.py`'s own module docstring.

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
- `tests/test_parse_duration.py` — `"30s"`/`"5m"`/`"1h"`/bare-digits parsing for `REFRESH_INTERVAL`.
- `tests/test_main_exec.py` — full exec-mode `main()` integration (`os.execvpe` is monkeypatched so the test process itself isn't replaced).
- `tests/test_main_sidecar.py` — sidecar mode's per-cycle building blocks (`_sidecar_tick`, `_run_sidecar`) directly, since `main()` itself would block forever in sidecar mode now. Covers a normal refresh, the empty-manifest/local-dev path, dotenv quoting, and — the important one — that a failed refresh (`_sidecar_tick` returns `False`) leaves the last-known-good secrets on disk completely untouched rather than blanking or crashing.
- `tests/test_main_dispatch.py` — `main()`'s sidecar-mode wiring (`REFRESH_INTERVAL` parsing, args passed to `_run_sidecar`) with `_run_sidecar` and `signal.signal` both mocked, so no real loop runs and no real process signal handler gets installed by the test suite.

**`test_main_sidecar.py`'s permission assertions are the permanent regression test for the v1.2.1 file-permission bug**: v1.2.0 wrote every sidecar-mode output file as `0600`, unreadable by a consuming app container running as a different, non-root user — the app silently booted with no secrets. The fix (`_write_file_target`'s `perm` argument, `0o644` in sidecar mode vs `0o600` in exec mode) is asserted directly on file permissions there; don't let a future change relax that assertion without understanding why it's there.

## Architecture

Single file, deliberately: `init.py`'s docstring is the primary spec (manifest format, both modes' exact CLI shape and semantics). `_parse_args` distinguishes modes by presence of `--` (exec) vs `--out <path>` (sidecar) in argv. `_fetch_all` is gated on `AZURE_CLIENT_SECRET` being set, not on manifest contents — so the same image + manifest runs unmodified in local dev (env var unset: skip the vault, one stderr notice, no error) and production (env var set: fetch for real); it raises `SystemExit` on any hard Azure error (bad manifest entry, token failure, secret-fetch failure).

**Exec mode** (`main()`'s non-sidecar branch): one-shot, unchanged in spirit since v1.0.0. Writes `file:` targets at `0600`, merges `env:` targets into a copy of `os.environ`, then `os.execvpe`s the real command — this script's job is done and the real process becomes PID 1's actual child (correct `SIGTERM` handling, no wrapper process left around). A `SystemExit` from `_fetch_all` here is fatal and immediate, same as always — a one-shot startup wrapper should fail fast on a bad manifest or Azure error.

**Sidecar mode** (`_run_sidecar`, since v2.0.0): a long-lived daemon, not a one-shot init step. `_sidecar_tick(manifest_path, out_path, ready_path)` is one refresh attempt — it calls `_fetch_all` and, only if that succeeds, atomically writes (`_write_file_target`/`_write_dotenv`, both temp-file-then-`os.replace`) the `file:` targets at `0644` and the `env:` targets as a dotenv file, then touches the readiness file. If `_fetch_all` raises `SystemExit`, `_sidecar_tick` catches it, logs to stderr, and returns `False` — **it deliberately does not let a transient Azure failure crash the container or touch already-written secrets**. `_run_sidecar` just loops `_sidecar_tick` on a `threading.Event.wait(interval)` cadence until `SIGTERM`/`SIGINT` sets the event (installed in `main()`), ticking at least once even if a stop signal is already pending — callers need that first attempt to happen for the readiness file (hence the Dockerfile `HEALTHCHECK`, and `depends_on: condition: service_healthy` on the consumer side — never `service_completed_successfully`, since this container no longer exits on its own).
