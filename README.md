# container-init

A generic entrypoint wrapper: fetch secrets from Azure Key Vault per a small
manifest, inject them into the environment or filesystem, then `exec` the
real command. Zero dependencies beyond PyYAML — no Azure SDK.

The point: an entrypoint's job is to *start* the app, not to encode policy
about which secrets it needs. That belongs in a manifest — data, not script
logic — so this file never changes no matter how many secrets an app has.

## Usage

```
init.py <manifest.yaml> -- <command> [args...]
init.py -- <command> [args...]     # manifest path from $SECRETS_MANIFEST,
                                    # default ./secrets.yaml
```

Drop `init.py` into an image and make it the `ENTRYPOINT`:

```dockerfile
ADD https://raw.githubusercontent.com/mf808/container-init/v1.1.0/init.py /app/init.py
RUN pip install pyyaml   # if not already a dependency
ENTRYPOINT ["python", "/app/init.py", "/app/secrets.yaml", "--"]
CMD ["streamlit", "run", "app.py"]
```

Pin the URL to a tag, not a branch — this file is meant to be identical
across every app that uses it, and a tag is how you control when that
changes.

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
    file: /path/to/file     # write the raw value to this file, mode 0600
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
