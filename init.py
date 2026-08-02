#!/usr/bin/env python3
"""Fetch secrets from Azure Key Vault per a small manifest. Two modes:

  Exec mode (wrap another process's own container):
    init.py <manifest.yaml> -- <command> [args...]
    init.py -- <command> [args...]     # manifest path from SECRETS_MANIFEST,
                                        # default ./secrets.yaml
    Fetches once, injects secrets into the environment/filesystem, then execs
    <command>. One-shot by design: the wrapped process is what stays alive.
    Use as a container's ENTRYPOINT.

  Sidecar mode (a long-lived container, app image untouched):
    init.py <manifest.yaml> --out <path>
    Runs forever alongside the app rather than exiting after one fetch —
    a small always-on daemon in the style of tools like External Secrets
    Operator, not a one-shot init step. On a fixed interval (REFRESH_INTERVAL
    env var, e.g. "30s"/"5m"/"1h", default "5m") it re-fetches every secret
    and, only if that fetch actually succeeds (a live connection to Azure was
    established), atomically rewrites every `env:` target as a KEY=value
    line in a dotenv-style file at <path> (`file:` targets still write to
    their own path as usual). A failed refresh (network blip, Azure outage,
    expired credential) is logged and otherwise ignored — the previous,
    last-known-good secrets are left completely untouched on disk, and the
    process keeps running and retries on the next interval. It never crashes
    the container and never blanks out a secret just because one refresh
    attempt failed.

    After the first successful fetch, a readiness file (READY_FILE env var,
    default /tmp/container-init-ready) is created/touched — this repo's own
    Dockerfile HEALTHCHECK checks for it, so callers should use
    `depends_on: condition: service_healthy` (NOT service_completed_successfully
    — this container intentionally never exits on its own). Pair with a
    shared volume; the app reads <path> itself at its own startup (e.g.
    Node's dotenv pointed at that path) — no ENTRYPOINT/image change needed
    on the app side at all.

    Deliberately minimal footprint for something meant to run indefinitely
    next to an app: no dependency beyond PyYAML, one thread-free sleep loop,
    no background HTTP server — the whole idle cost between refreshes is a
    blocked `Event.wait()`.

The same script runs identically whether the manifest lists 0 or 100
secrets — what to fetch is data (the manifest), not logic in this file.

Manifest (YAML):
    vault: <key-vault-name>
    tenant_id: <azure-ad-tenant-id>     # not secret, safe to commit
    client_id: <service-principal-app-id>   # not secret, safe to commit
    secrets:
      - name: <key-vault-secret-name>
        env: <ENV_VAR_NAME>            # exec mode: set as an env var
                                        # sidecar mode: one line in the dotenv file
      - name: <key-vault-secret-name>
        file: <path>                   # write the raw value to this file (0600
                                        # in exec mode, 0644 in sidecar mode —
                                        # a different, non-root container is
                                        # meant to read it there)

The service principal's client secret is never in the manifest — it must be
set in the environment as AZURE_CLIENT_SECRET (the one bootstrap credential
each host/app is given out-of-band). Whether Azure gets contacted at all is
gated on AZURE_CLIENT_SECRET being present, not on whether the manifest lists
secrets — the same image + manifest can run unmodified in local dev (no
AZURE_CLIENT_SECRET set: skip straight to exec / write an empty file, a
one-line stderr notice, no error) and in production (set: fetch for real).
This is deliberate: a manifest is "secrets this app might need," not
"secrets this run must fetch."

Requires PyYAML for the manifest; everything else is stdlib (urllib) — no
Azure SDK, so this drops into any Python image unmodified.
"""
import json
import os
import signal
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import yaml

API_VERSION = "7.4"
DEFAULT_READY_FILE = "/tmp/container-init-ready"


def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def get_token(tenant_id, client_id, client_secret):
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    resp = _post_form(url, {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://vault.azure.net/.default",
    })
    return resp["access_token"]


def get_secret(vault, name, token):
    url = f"https://{vault}.vault.azure.net/secrets/{name}?api-version={API_VERSION}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)["value"]


def _dotenv_quote(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _parse_duration(text):
    """Parse '30s' / '5m' / '1h' into seconds. Bare digits are seconds."""
    text = text.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    if text and text[-1] in units:
        return int(text[:-1]) * units[text[-1]]
    return int(text)


def _parse_args(argv):
    if "--" in argv:
        sep = argv.index("--")
        manifest_path = argv[1] if sep > 1 else os.environ.get("SECRETS_MANIFEST", "secrets.yaml")
        command = argv[sep + 1:]
        if not command:
            sys.exit("No command given after --")
        return "exec", manifest_path, command
    if "--out" in argv:
        out_idx = argv.index("--out")
        try:
            out_path = argv[out_idx + 1]
        except IndexError:
            sys.exit("--out requires a path")
        manifest_path = argv[1] if out_idx > 1 else os.environ.get("SECRETS_MANIFEST", "secrets.yaml")
        return "sidecar", manifest_path, out_path
    sys.exit(__doc__)


def _fetch_all(manifest_path):
    """Returns (env_entries, file_entries) as lists of (name/target, value) —
    empty lists if AZURE_CLIENT_SECRET isn't set or the manifest has none."""
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f) or {}

    entries = manifest.get("secrets") or []
    client_secret = os.environ.get("AZURE_CLIENT_SECRET")

    if entries and not client_secret:
        print(
            "init.py: AZURE_CLIENT_SECRET not set, skipping Key Vault fetch (local dev?)",
            file=sys.stderr,
        )
        entries = []

    env_entries, file_entries = [], []
    if entries:
        try:
            token = get_token(manifest["tenant_id"], manifest["client_id"], client_secret)
        except urllib.error.HTTPError as e:
            sys.exit(f"Azure AD token request failed: {e.code} {e.read().decode(errors='replace')}")

        vault = manifest["vault"]
        for entry in entries:
            name = entry["name"]
            try:
                value = get_secret(vault, name, token)
            except urllib.error.HTTPError as e:
                sys.exit(f"fetching secret '{name}' failed: {e.code} {e.read().decode(errors='replace')}")

            if "env" in entry:
                env_entries.append((entry["env"], value))
            elif "file" in entry:
                file_entries.append((entry["file"], value))
            else:
                sys.exit(f"secret '{name}': manifest entry needs 'env' or 'file'")

    return env_entries, file_entries


def _write_file_target(path, value, perm):
    """Writes via a temp file + atomic rename so a reader never sees a
    partially-written file, then applies perm to the final path."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        f.write(value)
    os.chmod(tmp_path, perm)
    os.replace(tmp_path, path)


def _write_dotenv(out_path, env_entries):
    directory = os.path.dirname(out_path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{out_path}.tmp"
    with open(tmp_path, "w") as f:
        for key, value in env_entries:
            f.write(f"{key}={_dotenv_quote(value)}\n")
    os.chmod(tmp_path, 0o644)
    os.replace(tmp_path, out_path)


def _sidecar_tick(manifest_path, out_path, ready_path):
    """One refresh attempt. Returns True if secrets on disk were updated
    (a live Azure connection was established this cycle), False if the
    attempt failed — in which case last-known-good secrets are left
    completely untouched."""
    try:
        env_entries, file_entries = _fetch_all(manifest_path)
    except SystemExit as e:
        print(f"init.py: refresh failed, keeping last-known-good secrets: {e}", file=sys.stderr)
        return False

    for path, value in file_entries:
        _write_file_target(path, value, 0o644)
    _write_dotenv(out_path, env_entries)
    with open(ready_path, "w"):
        pass
    return True


def _run_sidecar(manifest_path, out_path, interval, stop_event, ready_path=None):
    ready_path = ready_path or os.environ.get("READY_FILE", DEFAULT_READY_FILE)
    while True:
        _sidecar_tick(manifest_path, out_path, ready_path)
        if stop_event.wait(interval):
            return


def main(argv=None):
    mode, manifest_path, target = _parse_args(argv if argv is not None else sys.argv)

    if mode == "sidecar":
        interval = _parse_duration(os.environ.get("REFRESH_INTERVAL", "5m"))
        stop_event = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())
        _run_sidecar(manifest_path, target, interval, stop_event)
        return

    # Exec mode: one-shot, unchanged — the wrapped command is the long-lived
    # process here, not this script.
    env_entries, file_entries = _fetch_all(manifest_path)
    for path, value in file_entries:
        _write_file_target(path, value, 0o600)

    command = target
    env = dict(os.environ)
    for key, value in env_entries:
        env[key] = value
    # exec, not subprocess.run: this script's job is done, the real process
    # should become PID 1's actual child directly (correct signal handling —
    # e.g. SIGTERM on `docker stop` reaching the app — and no wrapper process
    # left sitting around).
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    main()
