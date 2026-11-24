from __future__ import annotations

import pytest

from cc_switcher import oauth
from conftest import ACCOUNT_A, make_login

NOW_MS = 1_800_000_000_000


@pytest.mark.parametrize(
    "expires_at, skew, expected",
    [
        (NOW_MS + 3_600_000, 300, False),
        (NOW_MS + 299_000, 300, True),
        (NOW_MS + 301_000, 300, False),
        (NOW_MS, 0, True),
        (NOW_MS - 1, 0, True),
        (NOW_MS + 1, 0, False),
        (None, 300, False),
        ("garbage", 300, True),
        (True, 300, True),
        (str(NOW_MS + 3_600_000), 300, False),
    ],
)
def test_is_expired(expires_at, skew, expected):
    login = make_login(ACCOUNT_A)
    login["expiresAt"] = expires_at
    assert oauth.is_expired(login, skew_seconds=skew, now_ms=NOW_MS) is expected


def test_is_expired_without_access_token():
    login = make_login(ACCOUNT_A)
    login["accessToken"] = ""
    assert oauth.is_expired(login, now_ms=NOW_MS) is True
    assert oauth.is_expired({}, now_ms=NOW_MS) is True


def test_is_expired_uses_wall_clock_by_default():
    far_future = make_login(ACCOUNT_A, expires_at_ms=4_102_444_800_000)
    long_ago = make_login(ACCOUNT_A, expires_at_ms=1_000)
    assert oauth.is_expired(far_future) is False
    assert oauth.is_expired(long_ago) is True
