"""Sidecar-mode integration: single-cycle behavior (_sidecar_tick) and the
loop wrapper (_run_sidecar), plus the permission regression test for v1.2.1.

Since sidecar mode is now a long-lived daemon (main() would block forever),
these tests exercise the per-cycle building blocks directly rather than
main() itself — see test_main_exec.py's docstring for the equivalent
reasoning on the exec-mode side (there it's os.execvpe that's mocked out;
here it's simply that we call the non-looping pieces).

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
import threading
import urllib.parse

from init import _run_sidecar, _sidecar_tick

MANIFEST = dict(vault="kv-test", tenant_id="tenant-1", client_id="client-1")


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_tick_writes_world_readable_dotenv_and_file_targets_and_marks_ready(
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
    ready_path = tmp_path / "ready"

    updated = _sidecar_tick(str(manifest), str(out_path), str(ready_path))

    assert updated is True
    # The regression: both must be 0644 (world-readable), not 0600.
    assert _mode(out_path) == 0o644
    assert _mode(tmp_path / "certs" / "cert.pem") == 0o644
    assert out_path.read_text() == 'API_KEY="sk-123"\n'
    assert (tmp_path / "certs" / "cert.pem").read_text() == "cert-body"
    assert ready_path.exists()


def test_tick_with_no_secrets_writes_empty_readable_file(write_manifest, monkeypatch, tmp_path):
    monkeypatch.delenv("AZURE_CLIENT_SECRET", raising=False)
    manifest = write_manifest(**MANIFEST, secrets=[])
    out_path = tmp_path / "out" / "secrets.env"
    ready_path = tmp_path / "ready"

    updated = _sidecar_tick(str(manifest), str(out_path), str(ready_path))

    assert updated is True
    assert out_path.read_text() == ""
    assert _mode(out_path) == 0o644
    assert ready_path.exists()


def test_tick_quotes_dotenv_values(write_manifest, fake_azure, monkeypatch, tmp_path):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    fake_azure({"tricky": 'has "quotes" and\nnewline'})
    manifest = write_manifest(**MANIFEST, secrets=[{"name": "tricky", "env": "TRICKY"}])
    out_path = tmp_path / "secrets.env"

    _sidecar_tick(str(manifest), str(out_path), str(tmp_path / "ready"))

    assert out_path.read_text() == 'TRICKY="has \\"quotes\\" and\\nnewline"\n'


def test_tick_on_azure_failure_leaves_last_known_good_untouched(
    write_manifest, fake_azure, monkeypatch, tmp_path, http_error, capsys
):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    manifest = write_manifest(**MANIFEST, secrets=[{"name": "api-key", "env": "API_KEY"}])
    out_path = tmp_path / "secrets.env"
    ready_path = tmp_path / "ready"

    fake_azure({"api-key": "sk-first"})
    assert _sidecar_tick(str(manifest), str(out_path), str(ready_path)) is True
    good_content = out_path.read_text()
    assert good_content == 'API_KEY="sk-first"\n'

    # Simulate a transient outage on the next refresh: the token endpoint fails.
    fake_azure({}, token_error=http_error(503, "vault unavailable"))
    updated = _sidecar_tick(str(manifest), str(out_path), str(ready_path))

    assert updated is False
    assert out_path.read_text() == good_content  # untouched, not blanked or stale-overwritten
    assert "refresh failed" in capsys.readouterr().err


def test_run_sidecar_ticks_at_least_once_even_if_already_stopped(
    write_manifest, fake_azure, monkeypatch, tmp_path
):
    """A stop signal arriving immediately must not skip the initial fetch —
    callers depend on service_healthy only ever becoming true after a real
    attempt, matching the old one-shot bootstrap guarantee."""
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    fake_azure({"api-key": "sk-123"})
    manifest = write_manifest(**MANIFEST, secrets=[{"name": "api-key", "env": "API_KEY"}])
    out_path = tmp_path / "secrets.env"
    stop_event = threading.Event()
    stop_event.set()  # already stopped before the loop even starts

    _run_sidecar(str(manifest), str(out_path), interval=999, stop_event=stop_event,
                 ready_path=str(tmp_path / "ready"))

    assert out_path.read_text() == 'API_KEY="sk-123"\n'


def test_run_sidecar_refreshes_on_each_interval(write_manifest, fake_azure, monkeypatch, tmp_path):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    manifest = write_manifest(**MANIFEST, secrets=[{"name": "api-key", "env": "API_KEY"}])
    out_path = tmp_path / "secrets.env"
    stop_event = threading.Event()

    call_count = {"n": 0}

    def on_request(url):
        if urllib.parse.urlparse(url).hostname == "login.microsoftonline.com":
            call_count["n"] += 1
            if call_count["n"] >= 3:
                stop_event.set()

    fake_azure({"api-key": "sk-123"}, on_request=on_request)

    _run_sidecar(str(manifest), str(out_path), interval=0, stop_event=stop_event,
                 ready_path=str(tmp_path / "ready"))

    assert call_count["n"] == 3
