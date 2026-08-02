"""Sidecar-mode integration + the permission regression test for v1.2.1.

v1.2.0 wrote every sidecar-mode output file (both the dotenv file and any
`file:` targets) as 0600, owned by whichever user runs this fetcher (root,
in the published image). A consuming app container running as a *different*,
non-root user (e.g. node:20-alpine's `node`, uid 1000) could not read the
file at all — dotenv silently failed and the app booted with no secrets, no
error surfaced. This is exactly the bug the fix in v1.2.1 addresses, so it
gets a dedicated, permanent regression test.
"""

import os
import stat

import init

MANIFEST = dict(vault="kv-test", tenant_id="tenant-1", client_id="client-1")


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_sidecar_mode_writes_world_readable_dotenv_and_file_targets(
    write_manifest, fake_azure, monkeypatch, tmp_path
):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    fake_azure({"api-key": "sk-123", "cert": "cert-body"})
    manifest = write_manifest(
        **MANIFEST,
        secrets=[
            {"name": "api-key", "env": "API_KEY"},
            {"name": "cert", "file": str(tmp_path / "certs" / "cert.pem")},
        ],
    )
    out_path = tmp_path / "out" / "secrets.env"

    init.main(["init.py", str(manifest), "--out", str(out_path)])

    # The regression: both must be 0644 (world-readable), not 0600.
    assert _mode(out_path) == 0o644
    assert _mode(tmp_path / "certs" / "cert.pem") == 0o644
    assert out_path.read_text() == 'API_KEY="sk-123"\n'
    assert (tmp_path / "certs" / "cert.pem").read_text() == "cert-body"


def test_sidecar_mode_with_no_secrets_writes_empty_readable_file(
    write_manifest, monkeypatch, tmp_path
):
    monkeypatch.delenv("AZURE_CLIENT_SECRET", raising=False)
    manifest = write_manifest(**MANIFEST, secrets=[])
    out_path = tmp_path / "out" / "secrets.env"

    init.main(["init.py", str(manifest), "--out", str(out_path)])

    assert out_path.read_text() == ""
    assert _mode(out_path) == 0o644


def test_sidecar_mode_quotes_dotenv_values(write_manifest, fake_azure, monkeypatch, tmp_path):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    fake_azure({"tricky": 'has "quotes" and\nnewline'})
    manifest = write_manifest(**MANIFEST, secrets=[{"name": "tricky", "env": "TRICKY"}])
    out_path = tmp_path / "secrets.env"

    init.main(["init.py", str(manifest), "--out", str(out_path)])

    assert out_path.read_text() == 'TRICKY="has \\"quotes\\" and\\nnewline"\n'
