#!/usr/bin/env python3
"""Generic entrypoint wrapper: fetch secrets from Azure Key Vault per a small
manifest, inject them into the environment or filesystem, then exec the real
command. The same script runs identically whether the manifest lists 0 or 100
secrets — what to fetch is data (the manifest), not logic in this file.

Usage:
    init.py <manifest.yaml> -- <command> [args...]
    init.py -- <command> [args...]     # manifest path from SECRETS_MANIFEST,
                                        # default ./secrets.yaml

Manifest (YAML):
    vault: <key-vault-name>
    tenant_id: <azure-ad-tenant-id>     # not secret, safe to commit
    client_id: <service-principal-app-id>   # not secret, safe to commit
    secrets:
      - name: <key-vault-secret-name>
        env: <ENV_VAR_NAME>            # set as an env var for the child process
      - name: <key-vault-secret-name>
        file: <path>                   # write the raw value to this file (0600)

The service principal's client secret is never in the manifest — it must be
set in the environment as AZURE_CLIENT_SECRET (the one bootstrap credential
each host/app is given out-of-band). Whether Azure gets contacted at all is
gated on AZURE_CLIENT_SECRET being present, not on whether the manifest lists
secrets — the same image + manifest can run unmodified in local dev (no
AZURE_CLIENT_SECRET set: skip straight to exec, a one-line stderr notice, no
error) and in production (set: fetch for real). This is deliberate: a
manifest is "secrets this app might need," not "secrets this run must fetch."

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


def _parse_args(argv):
    if "--" not in argv:
        sys.exit(__doc__)
    sep = argv.index("--")
    manifest_path = argv[1] if sep > 1 else os.environ.get("SECRETS_MANIFEST", "secrets.yaml")
    command = argv[sep + 1:]
    if not command:
        sys.exit("No command given after --")
    return manifest_path, command


def main(argv=None):
    manifest_path, command = _parse_args(argv if argv is not None else sys.argv)

    with open(manifest_path) as f:
        manifest = yaml.safe_load(f) or {}

    entries = manifest.get("secrets") or []
    env = dict(os.environ)
    client_secret = os.environ.get("AZURE_CLIENT_SECRET")

    if entries and not client_secret:
        print(
            "init.py: AZURE_CLIENT_SECRET not set, skipping Key Vault fetch (local dev?)",
            file=sys.stderr,
        )
        entries = []

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
                env[entry["env"]] = value
            elif "file" in entry:
                path = entry["file"]
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w") as f:
                    f.write(value)
                os.chmod(path, 0o600)
            else:
                sys.exit(f"secret '{name}': manifest entry needs 'env' or 'file'")

    # exec, not subprocess.run: this script's job is done, the real process
    # should become PID 1's actual child directly (correct signal handling —
    # e.g. SIGTERM on `docker stop` reaching the app — and no wrapper process
    # left sitting around).
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    main()
