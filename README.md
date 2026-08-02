# container-init

A generic entrypoint wrapper: fetch secrets from Azure Key Vault per a small
manifest, inject them into the environment or filesystem, then `exec` the
real command. Zero dependencies beyond PyYAML — no Azure SDK.

The point: an entrypoint's job is to *start* the app, not to encode policy
about which secrets it needs. That belongs in a manifest — data, not script
logic — so this file never changes no matter how many secrets an app has.

## Two modes

**Exec mode** — wrap the app's own container. Zero-touch for the app (secrets
arrive as real env vars), but couples the fetcher's runtime (Python) into
the app's own image:

```
init.py <manifest.yaml> -- <command> [args...]
init.py -- <command> [args...]     # manifest path from $SECRETS_MANIFEST,
                                    # default ./secrets.yaml
```

Drop `init.py` into an image and make it the `ENTRYPOINT`:

```dockerfile
ADD https://raw.githubusercontent.com/mf808/container-init/v1.2.1/init.py /app/init.py
RUN pip install pyyaml   # if not already a dependency
ENTRYPOINT ["python", "/app/init.py", "/app/secrets.yaml", "--"]
CMD ["streamlit", "run", "app.py"]
```

**Sidecar mode** — a long-lived container (this repo's own image) that runs
continuously alongside the app, zero changes to the app's image/Dockerfile at
all. Better fit when the app isn't Python (no runtime to add):

```
init.py <manifest.yaml> --out <path>
```

Unlike exec mode, sidecar mode never exits on its own — it's a small
always-on daemon in the style of tools like External Secrets Operator, not a
one-shot init step. On a fixed interval — **set from outside the container**,
never hardcoded, via the `REFRESH_INTERVAL` env var (e.g. `30s`/`5m`/`60m`,
default `60m`) — it re-fetches every secret and writes every
`env:` target as a `KEY=value` line to a dotenv-style file at `<path>`
(`file:` targets still write to their own path, same as exec mode) —
**but only if that fetch actually succeeds**. A failed refresh (network
blip, Azure outage, expired credential) is logged and otherwise ignored: the
previous, last-known-good secrets are left completely untouched on disk, and
the container keeps running and retries on the next interval. It never
crashes and never blanks out a secret just because one refresh attempt
failed. Deliberately minimal footprint for something meant to run
indefinitely: no dependency beyond PyYAML, one sleep loop, no background HTTP
server.

```yaml
services:
  secrets-fetcher:
    image: ghcr.io/mf808/container-init:v2.0.1
    environment:
      REFRESH_INTERVAL: 5m   # optional — overrides the 60m default, set per-deploy
    volumes:
      - secrets:/out
      - ./secrets.yaml:/secrets.yaml:ro
    env_file: .env   # AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET
    command: ["/secrets.yaml", "--out", "/out/secrets.env"]

  app:
    image: your-app:latest
    depends_on:
      secrets-fetcher:
        condition: service_healthy
    volumes:
      - secrets:/secrets:ro
    # app's own startup loads /secrets/secrets.env itself, e.g. Node:
    #   require('dotenv').config({ path: '/secrets/secrets.env' })

volumes:
  secrets:
```

**`condition: service_healthy`, not `service_completed_successfully`** —
this container intentionally never exits, so "completed successfully" would
never become true and the app would never start. Health comes from this
repo's own Dockerfile `HEALTHCHECK`, which checks for a readiness file
(`READY_FILE` env var, default `/tmp/container-init-ready`) created only
after the first successful fetch — the same "app waits for secrets to exist"
guarantee the old one-shot version gave, just expressed as a healthcheck
instead of a container exit code.

A secret rotated in the vault is picked up automatically on the next
`REFRESH_INTERVAL` tick — no manual `docker compose up --force-recreate`
needed anymore.

Pin image/URL references to a tag, not a branch — this tool is meant to be
identical across every app that uses it, and a tag is how you control when
that changes.

**Don't `COPY secrets.yaml` into the image.** The manifest is deploy-time
config — which secrets exist, where they map — not build content. Baking it
in means changing one mapping requires a full rebuild+republish+redeploy.
Bind-mount it at runtime instead, alongside however you already deploy the
app's compose file:

```yaml
services:
  app:
    image: your-app:latest
    volumes:
      - ./secrets.yaml:/app/secrets.yaml:ro
```

The manifest still belongs in the app's repo for version control — it's just
deployed as a file next to the production compose file, not `COPY`'d into
the image.

## Manifest

```yaml
vault: <key-vault-name>
tenant_id: <azure-ad-tenant-id>          # not secret, safe to commit
client_id: <service-principal-app-id>    # not secret, safe to commit
secrets:
  - name: <key-vault-secret-name>
    env: SOME_ENV_VAR       # set as an env var for the child process
  - name: <key-vault-secret-name>
    file: /path/to/file     # write the raw value to this file — mode 0600 in
                             # exec mode (same-process consumption), 0644 in
                             # sidecar mode (a different, non-root container
                             # is meant to read it)
```

A secret's *value* can be anything — a token, a whole YAML config blob, a
JSON file — the manifest only says where it goes, not what's inside it. An
empty (or missing) `secrets` list is a valid manifest: no Azure calls are
made at all, and `AZURE_CLIENT_SECRET` isn't required.

## Auth

The service principal's `tenant_id` and `client_id` are plain identifiers,
not secret — they're meant to be committed in the manifest. The one thing
that must never be committed is the client **secret**, which this script
always reads from the `AZURE_CLIENT_SECRET` environment variable — the one
bootstrap credential each host/app is handed out-of-band (e.g. via `.env`,
`chmod 600`, never in git).

## Requires

- Python 3.9+
- `PyYAML` (only for parsing the manifest — everything else is stdlib
  `urllib`, no Azure SDK)
- A Key Vault, `enableRbacAuthorization: true`, and a service principal
  granted `Key Vault Secrets User` scoped to whichever secrets it needs
  (scope it per-secret if you want per-app/per-host isolation on a shared
  vault — that's the intended usage, not a vault per app).
