"""Exec-mode integration: os.execvpe is mocked so the test process itself
isn't replaced. Exec mode keeps the tighter 0600 for `file:` targets
(same-process consumption), unlike sidecar mode's 0644 — see
test_main_sidecar.py for that half of the permission regression test.
"""

import os
import stat

import pytest

import init

MANIFEST = dict(vault="kv-test", tenant_id="tenant-1", client_id="client-1")


class _Execed(Exception):
    """Raised by the mocked os.execvpe to capture its arguments without exec'ing.

    Deliberately does not use the name ``args`` for the captured command list —
    ``BaseException.args`` is a special attribute the interpreter repopulates
    from the constructor's positional arguments, silently discarding a same-named
    instance attribute assigned in __init__.
    """

    def __init__(self, file, command, env):
        self.file = file
        self.command = command
        self.env = env


@pytest.fixture
def mock_execvpe(monkeypatch):
    def _fake(file, args, env):
        raise _Execed(file, args, env)

    monkeypatch.setattr(init.os, "execvpe", _fake)


def test_exec_mode_merges_secrets_into_env_and_execs_command(
    write_manifest, fake_azure, monkeypatch, mock_execvpe, tmp_path
):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    monkeypatch.setenv("EXISTING_VAR", "already-there")
    fake_azure({"api-key": "sk-123"})
    manifest = write_manifest(**MANIFEST, secrets=[{"name": "api-key", "env": "API_KEY"}])

    with pytest.raises(_Execed) as excinfo:
        init.main(["init.py", str(manifest), "--", "node", "server.js"])

    execed = excinfo.value
    assert execed.file == "node"
    assert execed.command == ["node", "server.js"]
    assert execed.env["API_KEY"] == "sk-123"
    assert execed.env["EXISTING_VAR"] == "already-there"


def test_exec_mode_file_targets_stay_0600(
    write_manifest, fake_azure, monkeypatch, mock_execvpe, tmp_path
):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    fake_azure({"cert": "cert-body"})
    cert_path = tmp_path / "cert.pem"
    manifest = write_manifest(**MANIFEST, secrets=[{"name": "cert", "file": str(cert_path)}])

    with pytest.raises(_Execed):
        init.main(["init.py", str(manifest), "--", "true"])

    mode = stat.S_IMODE(os.stat(cert_path).st_mode)
    assert mode == 0o600
