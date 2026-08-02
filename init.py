#!/usr/bin/env python3
"""Fetch secrets from Azure Key Vault per a small manifest. Two modes:

  Exec mode (wrap another process's own container):
    init.py <manifest.yaml> -- <command> [args...]
    init.py -- <command> [args...]     # manifest path from SECRETS_MANIFEST,
                                        # default ./secrets.yaml
    Injects secrets into the environment/filesystem, then execs <command>.
    Use as a container's ENTRYPOINT.

  Sidecar mode (a dedicated one-shot container, app image untouched):
    init.py <manifest.yaml> --out <path>
    Writes every `env:` target as a KEY=value line to a dotenv-style file at
    <path> (`file:` targets still write to their own path as usual), then
    exits 0. Pair with `depends_on: condition: service_completed_successfully`
    and a shared volume; the app reads <path> itself at its own startup
    (e.g. Node's dotenv pointed at that path) — no ENTRYPOINT/image change
    needed on the app side at all.

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
        file: <path>                   # write the raw value to this file (0600),
                                        # same in both modes

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
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

API_VERSION = "7.4"


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


def _write_file_target(path, value):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(value)
    os.chmod(path, 0o600)


def main(argv=None):
    mode, manifest_path, target = _parse_args(argv if argv is not None else sys.argv)
    env_entries, file_entries = _fetch_all(manifest_path)

    for path, value in file_entries:
        _write_file_target(path, value)

    if mode == "sidecar":
        out_path = target
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as f:
            for key, value in env_entries:
                f.write(f"{key}={_dotenv_quote(value)}\n")
        os.chmod(out_path, 0o600)
        return

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
