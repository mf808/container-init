import os
import stat

from init import _dotenv_quote, _write_file_target


def test_dotenv_quote_plain_value():
    assert _dotenv_quote("sk-abc123") == '"sk-abc123"'


def test_dotenv_quote_escapes_backslash_quote_and_newline():
    assert _dotenv_quote('back\\slash "quoted"\nline') == '"back\\\\slash \\"quoted\\"\\nline"'


def test_write_file_target_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "deeper" / "secret.pem"

    _write_file_target(str(target), "cert-body", 0o644)

    assert target.read_text() == "cert-body"


def test_write_file_target_applies_requested_perm(tmp_path):
    target = tmp_path / "secret"

    _write_file_target(str(target), "value", 0o600)

    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600
