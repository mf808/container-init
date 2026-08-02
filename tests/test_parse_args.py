import pytest

from init import _parse_args


def test_exec_mode_with_manifest():
    mode, manifest, command = _parse_args(["init.py", "m.yaml", "--", "node", "app.js"])
    assert mode == "exec"
    assert manifest == "m.yaml"
    assert command == ["node", "app.js"]


def test_exec_mode_manifest_from_env(monkeypatch):
    monkeypatch.setenv("SECRETS_MANIFEST", "/etc/secrets.yaml")
    mode, manifest, command = _parse_args(["init.py", "--", "streamlit", "run", "app.py"])
    assert mode == "exec"
    assert manifest == "/etc/secrets.yaml"
    assert command == ["streamlit", "run", "app.py"]


def test_exec_mode_manifest_default(monkeypatch):
    monkeypatch.delenv("SECRETS_MANIFEST", raising=False)
    _, manifest, _ = _parse_args(["init.py", "--", "true"])
    assert manifest == "secrets.yaml"


def test_exec_mode_no_command_exits():
    with pytest.raises(SystemExit, match="No command given after --"):
        _parse_args(["init.py", "m.yaml", "--"])


def test_sidecar_mode_with_manifest():
    mode, manifest, out = _parse_args(["init.py", "m.yaml", "--out", "/out/secrets.env"])
    assert mode == "sidecar"
    assert manifest == "m.yaml"
    assert out == "/out/secrets.env"


def test_sidecar_mode_manifest_from_env(monkeypatch):
    monkeypatch.setenv("SECRETS_MANIFEST", "/etc/secrets.yaml")
    _, manifest, _ = _parse_args(["init.py", "--out", "/out/secrets.env"])
    assert manifest == "/etc/secrets.yaml"


def test_sidecar_mode_missing_path_exits():
    with pytest.raises(SystemExit, match="--out requires a path"):
        _parse_args(["init.py", "m.yaml", "--out"])


def test_no_mode_flag_exits_with_usage():
    with pytest.raises(SystemExit):
        _parse_args(["init.py", "m.yaml"])
