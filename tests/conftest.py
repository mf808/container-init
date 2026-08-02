"""Shared fixtures.

All tests are static and repeatable: no real network access. Azure calls go
through ``urllib.request.urlopen``, mocked here at that exact boundary via
``FakeAzure`` so ``init.py``'s own HTTP-building code (URL construction,
header, error handling) still runs for real.
"""

import io
import json
import urllib.error

import pytest
import yaml

import init


class FakeAzure:
    """Stands in for Azure AD token + Key Vault secret endpoints.

    Dispatches on URL shape, matching what ``get_token``/``get_secret`` in
    init.py actually request. ``secrets`` maps secret name -> value.
    """

    def __init__(self, secrets, *, token_error=None, secret_errors=None):
        self.secrets = secrets
        self.token_error = token_error
        self.secret_errors = secret_errors or {}
        self.requested_urls = []

    def __call__(self, req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else req
        self.requested_urls.append(url)

        if "login.microsoftonline.com" in url:
            if self.token_error:
                raise self.token_error
            return _FakeResponse({"access_token": "fake-token"})

        # https://{vault}.vault.azure.net/secrets/{name}?api-version=...
        name = url.split("/secrets/")[1].split("?")[0]
        if name in self.secret_errors:
            raise self.secret_errors[name]
        return _FakeResponse({"value": self.secrets[name]})


class _FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


def _http_error(code, message="error"):
    return urllib.error.HTTPError(
        "https://example.invalid", code, message, {}, io.BytesIO(message.encode())
    )


@pytest.fixture
def http_error():
    return _http_error


@pytest.fixture
def fake_azure(monkeypatch):
    """Patch init.py's urlopen call; returns the FakeAzure so tests can configure it."""

    def _install(secrets=None, *, token_error=None, secret_errors=None):
        fake = FakeAzure(secrets or {}, token_error=token_error, secret_errors=secret_errors)
        monkeypatch.setattr(init.urllib.request, "urlopen", fake)
        return fake

    return _install


@pytest.fixture
def write_manifest(tmp_path):
    def _write(**fields):
        path = tmp_path / "secrets.yaml"
        path.write_text(yaml.dump(fields))
        return path

    return _write
