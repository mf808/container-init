import pytest

from init import _fetch_all


MANIFEST_COMMON = dict(vault="kv-test", tenant_id="tenant-1", client_id="client-1")


def test_no_secrets_in_manifest_makes_no_azure_calls(write_manifest, fake_azure, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    fake = fake_azure({})
    manifest = write_manifest(**MANIFEST_COMMON, secrets=[])

    env_entries, file_entries = _fetch_all(str(manifest))

    assert env_entries == []
    assert file_entries == []
    assert fake.requested_urls == []


def test_missing_client_secret_skips_fetch(write_manifest, fake_azure, monkeypatch, capsys):
    monkeypatch.delenv("AZURE_CLIENT_SECRET", raising=False)
    fake = fake_azure({"a-secret": "value"})
    manifest = write_manifest(
        **MANIFEST_COMMON, secrets=[{"name": "a-secret", "env": "A_SECRET"}]
    )

    env_entries, file_entries = _fetch_all(str(manifest))

    assert env_entries == []
    assert file_entries == []
    assert fake.requested_urls == []
    assert "AZURE_CLIENT_SECRET not set" in capsys.readouterr().err


def test_fetches_env_and_file_targets(write_manifest, fake_azure, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    fake_azure({"api-key": "sk-123", "cert": "-----BEGIN CERT-----"})
    manifest = write_manifest(
        **MANIFEST_COMMON,
        secrets=[
            {"name": "api-key", "env": "API_KEY"},
            {"name": "cert", "file": "/out/cert.pem"},
        ],
    )

    env_entries, file_entries = _fetch_all(str(manifest))

    assert env_entries == [("API_KEY", "sk-123")]
    assert file_entries == [("/out/cert.pem", "-----BEGIN CERT-----")]


def test_entry_missing_env_and_file_exits(write_manifest, fake_azure, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    fake_azure({"orphan": "value"})
    manifest = write_manifest(**MANIFEST_COMMON, secrets=[{"name": "orphan"}])

    with pytest.raises(SystemExit, match="needs 'env' or 'file'"):
        _fetch_all(str(manifest))


def test_token_http_error_exits(write_manifest, fake_azure, monkeypatch, http_error):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "wrong")
    fake_azure({}, token_error=http_error(401, "invalid_client"))
    manifest = write_manifest(
        **MANIFEST_COMMON, secrets=[{"name": "a-secret", "env": "A_SECRET"}]
    )

    with pytest.raises(SystemExit, match="401"):
        _fetch_all(str(manifest))


def test_secret_http_error_exits(write_manifest, fake_azure, monkeypatch, http_error):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "shh")
    fake_azure({}, secret_errors={"missing-secret": http_error(404, "not found")})
    manifest = write_manifest(
        **MANIFEST_COMMON, secrets=[{"name": "missing-secret", "env": "X"}]
    )

    with pytest.raises(SystemExit, match="404"):
        _fetch_all(str(manifest))
