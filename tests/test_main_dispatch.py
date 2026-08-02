"""main()'s sidecar-mode dispatch: interval parsing and wiring into
_run_sidecar, without ever entering the real infinite loop or installing
real process signal handlers (which would leak past the test)."""

import signal

import pytest

import init


@pytest.fixture
def mock_run_sidecar(monkeypatch):
    calls = []

    def _fake(manifest_path, out_path, interval, stop_event, ready_path=None):
        calls.append((manifest_path, out_path, interval))

    monkeypatch.setattr(init, "_run_sidecar", _fake)
    monkeypatch.setattr(signal, "signal", lambda *a, **kw: None)
    return calls


def test_main_sidecar_uses_default_refresh_interval(
    write_manifest, monkeypatch, mock_run_sidecar, tmp_path
):
    monkeypatch.delenv("REFRESH_INTERVAL", raising=False)
    manifest = write_manifest(vault="v", tenant_id="t", client_id="c", secrets=[])
    out_path = tmp_path / "secrets.env"

    init.main(["init.py", str(manifest), "--out", str(out_path)])

    [(m, o, interval)] = mock_run_sidecar
    assert m == str(manifest)
    assert o == str(out_path)
    assert interval == 300  # 5m default


def test_main_sidecar_reads_refresh_interval_env(
    write_manifest, monkeypatch, mock_run_sidecar, tmp_path
):
    monkeypatch.setenv("REFRESH_INTERVAL", "1h")
    manifest = write_manifest(vault="v", tenant_id="t", client_id="c", secrets=[])
    out_path = tmp_path / "secrets.env"

    init.main(["init.py", str(manifest), "--out", str(out_path)])

    [(_, _, interval)] = mock_run_sidecar
    assert interval == 3600
