import pytest

from init import _parse_duration


@pytest.mark.parametrize(
    "text,expected_seconds",
    [
        ("30s", 30),
        ("5m", 300),
        ("60m", 3600),
        ("1h", 3600),
        ("45", 45),
        ("  5m  ", 300),
        ("5M", 300),
    ],
)
def test_parse_duration(text, expected_seconds):
    assert _parse_duration(text) == expected_seconds
