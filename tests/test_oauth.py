from __future__ import annotations

import io
import json
import urllib.error

import pytest

from cc_switcher import oauth, paths, usage

from conftest import ACCOUNT_A, make_login

NOW_MS = 1_800_000_000_000  # fixed "now" for deterministic expiry maths


@pytest.fixture(autouse=True)
def _no_real_version_probe(monkeypatch):
    monkeypatch.setattr(usage, "_version_cache", "2.1.330")


class FakePost:
    """http_post fake: replies with a canned (status, body) and records the request."""

    def __init__(self, status=200, body=None, raises=None):
        self.status, self.body, self.raises = status, body, raises
        self.calls: list[dict] = []

    def __call__(self, url, payload, *, headers, timeout):
        self.calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        if self.raises:
            raise self.raises
        return self.status, self.body


def ok_body(**overrides):
    body = {"access_token": "sk-ant-oat01-NEW", "refresh_token": "sk-ant-ort01-NEW", "expires_in": 28800}
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- is_expired


@pytest.mark.parametrize(
    "expires_at, skew, expected",
    [
        (NOW_MS + 3_600_000, 300, False),  # an hour left
        (NOW_MS + 299_000, 300, True),  # inside the 5 min skew
        (NOW_MS + 301_000, 300, False),  # just outside it
        (NOW_MS, 0, True),  # exactly now counts as expired
        (NOW_MS - 1, 0, True),
        (NOW_MS + 1, 0, False),
        (None, 300, False),  # no expiry recorded: non-expiring, like Claude Code
        ("garbage", 300, True),  # unreadable expiry: refresh to be safe
        (True, 300, True),  # a bool is not a timestamp
        (str(NOW_MS + 3_600_000), 300, False),  # numeric string tolerated
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


# --------------------------------------------------------------------------- refresh: success


def test_refresh_sends_the_documented_request():
    login = make_login(ACCOUNT_A)
    post = FakePost(body=ok_body())

    oauth.refresh_login(login, http_post=post, timeout=7, now_ms=NOW_MS)

    (call,) = post.calls
    assert call["url"] == "https://platform.claude.com/v1/oauth/token"
    assert call["payload"] == {
        "grant_type": "refresh_token",
        "refresh_token": "sk-ant-ort01-fake-1",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "scope": "user:inference user:profile user:sessions:claude_code user:mcp_servers",
    }
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["User-Agent"] == "claude-code/2.1.330"
    assert call["timeout"] == 7


def test_refresh_uses_the_login_specific_oauth_client_when_present():
    """Claude Code passes a saved clientId; replacing it can reject an otherwise valid grant."""
    login = make_login(ACCOUNT_A)
    login["clientId"] = "custom-client-id"
    post = FakePost(body=ok_body())
    oauth.refresh_login(login, http_post=post, now_ms=NOW_MS)
    assert post.calls[0]["payload"]["client_id"] == "custom-client-id"


def test_refresh_returns_updated_copy_and_keeps_identity():
    login = make_login(ACCOUNT_A, expires_at_ms=NOW_MS - 1000)
    original = json.loads(json.dumps(login))

    new = oauth.refresh_login(login, http_post=FakePost(body=ok_body(expires_in=28800)), now_ms=NOW_MS)

    assert login == original, "input must not be mutated"
    assert new["accessToken"] == "sk-ant-oat01-NEW"
    assert new["refreshToken"] == "sk-ant-ort01-NEW"
    assert new["expiresAt"] == NOW_MS + 28_800_000  # epoch ms, from expires_in seconds
    # everything that is not a token comes through untouched
    for key in ("accountUuid", "organizationUuid", "subscriptionType", "rateLimitTier", "scopes"):
        assert new[key] == login[key]


def test_absent_refresh_token_keeps_the_old_one():
    login = make_login(ACCOUNT_A)
    body = ok_body()
    del body["refresh_token"]

    new = oauth.refresh_login(login, http_post=FakePost(body=body), now_ms=NOW_MS)

    assert new["refreshToken"] == "sk-ant-ort01-fake-1"
    assert new["accessToken"] == "sk-ant-oat01-NEW"


def test_refresh_token_expiry_and_scope_follow_the_response():
    login = make_login(ACCOUNT_A)
    body = ok_body(refresh_token_expires_in=86400, scope="user:inference user:profile")

    new = oauth.refresh_login(login, http_post=FakePost(body=body), now_ms=NOW_MS)

    assert new["refreshTokenExpiresAt"] == NOW_MS + 86_400_000
    assert new["scopes"] == ["user:inference", "user:profile"]


def test_rotated_refresh_token_without_ttl_clears_stale_refresh_expiry():
    login = make_login(ACCOUNT_A)
    login["refreshTokenExpiresAt"] = NOW_MS + 1000

    new = oauth.refresh_login(login, http_post=FakePost(body=ok_body()), now_ms=NOW_MS)

    assert new["refreshTokenExpiresAt"] is None


def test_unrotated_refresh_token_keeps_its_expiry():
    login = make_login(ACCOUNT_A)
    login["refreshTokenExpiresAt"] = NOW_MS + 1000
    body = ok_body()
    del body["refresh_token"]

    new = oauth.refresh_login(login, http_post=FakePost(body=body), now_ms=NOW_MS)

    assert new["refreshTokenExpiresAt"] == NOW_MS + 1000


def test_missing_expires_in_falls_back_instead_of_discarding_a_rotated_token():
    login = make_login(ACCOUNT_A)
    body = ok_body()
    del body["expires_in"]

    new = oauth.refresh_login(login, http_post=FakePost(body=body), now_ms=NOW_MS)

    assert new["refreshToken"] == "sk-ant-ort01-NEW"
    assert new["expiresAt"] == NOW_MS + oauth.DEFAULT_EXPIRES_IN_SECONDS * 1000


def test_scope_omitted_when_login_has_none():
    login = make_login(ACCOUNT_A)
    login["scopes"] = []
    post = FakePost(body=ok_body())

    oauth.refresh_login(login, http_post=post, now_ms=NOW_MS)

    assert "scope" not in post.calls[0]["payload"]


def test_body_may_be_bytes_or_text():
    login = make_login(ACCOUNT_A)
    for body in (json.dumps(ok_body()).encode(), json.dumps(ok_body())):
        new = oauth.refresh_login(login, http_post=FakePost(body=body), now_ms=NOW_MS)
        assert new["accessToken"] == "sk-ant-oat01-NEW"


# --------------------------------------------------------------------------- refresh: rejected


def test_missing_refresh_token_is_rejected_without_a_request():
    login = make_login(ACCOUNT_A)
    login["refreshToken"] = ""
    post = FakePost(body=ok_body())

    with pytest.raises(oauth.RefreshRejected):
        oauth.refresh_login(login, http_post=post)

    assert post.calls == []


@pytest.mark.parametrize("status", [400, 401])
def test_400_and_401_mean_the_user_must_re_add_the_account(status):
    post = FakePost(status=status, body={"error": "invalid_grant"})

    with pytest.raises(oauth.RefreshRejected) as exc:
        oauth.refresh_login(make_login(ACCOUNT_A), http_post=post)

    assert exc.value.status == status
    assert "invalid_grant" in str(exc.value)


def test_invalid_grant_is_rejection_whatever_the_status():
    post = FakePost(status=403, body={"error": "invalid_grant"})
    with pytest.raises(oauth.RefreshRejected):
        oauth.refresh_login(make_login(ACCOUNT_A), http_post=post)


def test_refresh_rejected_is_an_oauth_error():
    assert issubclass(oauth.RefreshRejected, oauth.OAuthError)


def test_error_messages_never_contain_tokens():
    secret = "sk-ant-ort01-fake-1"
    body = {"error": "invalid_grant", "error_description": f"Refresh token {secret} not found"}
    with pytest.raises(oauth.RefreshRejected) as exc:
        oauth.refresh_login(make_login(ACCOUNT_A), http_post=FakePost(status=400, body=body))
    assert secret not in str(exc.value)
    assert "Refresh token" not in str(exc.value)  # the free-text description is dropped entirely


def test_server_error_code_cannot_echo_a_refresh_token():
    login = make_login(ACCOUNT_A)
    secret = login["refreshToken"]
    post = FakePost(status=429, body={"error": {"type": secret}})
    with pytest.raises(oauth.OAuthError) as exc:
        oauth.refresh_login(login, http_post=post)
    assert secret not in str(exc.value)


def test_error_code_is_sanitised():
    body = {"error": "bad\n\x1b[31mcode with spaces" + "x" * 500}
    with pytest.raises(oauth.RefreshRejected) as exc:
        oauth.refresh_login(make_login(ACCOUNT_A), http_post=FakePost(status=400, body=body))
    assert "\n" not in str(exc.value) and "\x1b" not in str(exc.value)
    assert len(str(exc.value)) < 200


# --------------------------------------------------------------------------- refresh: transient


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_server_trouble_is_transient_not_a_rejection(status):
    with pytest.raises(oauth.OAuthError) as exc:
        oauth.refresh_login(make_login(ACCOUNT_A), http_post=FakePost(status=status, body="oops"))
    assert not isinstance(exc.value, oauth.RefreshRejected)
    assert exc.value.status == status


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("no route"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
        OSError("network is down"),
    ],
)
def test_transport_failures_are_transient(error):
    with pytest.raises(oauth.OAuthError) as exc:
        oauth.refresh_login(make_login(ACCOUNT_A), http_post=FakePost(raises=error))
    assert not isinstance(exc.value, oauth.RefreshRejected)
    assert exc.value.status is None


@pytest.mark.parametrize("body", ["<html>gateway</html>", "[]", "", None, {"expires_in": 5}, {"access_token": ""}])
def test_unusable_200_reply_is_transient(body):
    with pytest.raises(oauth.OAuthError) as exc:
        oauth.refresh_login(make_login(ACCOUNT_A), http_post=FakePost(status=200, body=body))
    assert not isinstance(exc.value, oauth.RefreshRejected)


# --------------------------------------------------------------------------- default transport


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body, self.status = body, status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_default_transport_posts_json_via_urlopen(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["data"] = json.loads(request.data)
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["timeout"] = timeout
        return FakeResponse(json.dumps(ok_body()).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    new = oauth.refresh_login(make_login(ACCOUNT_A), timeout=3, now_ms=NOW_MS)

    assert new["accessToken"] == "sk-ant-oat01-NEW"
    assert seen["url"] == paths.OAUTH_TOKEN_URL
    assert seen["method"] == "POST"
    assert seen["data"]["grant_type"] == "refresh_token"
    assert seen["headers"]["content-type"] == "application/json"
    assert seen["timeout"] == 3


def test_default_transport_turns_http_errors_into_status_and_body(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant"}')
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(oauth.RefreshRejected):
        oauth.refresh_login(make_login(ACCOUNT_A))


def test_oauth_rate_limit_preserves_server_retry_after(monkeypatch):
    """Dropping the OAuth response header makes CCS retry before the server allows it."""
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", {"Retry-After": "7200"},
            io.BytesIO(b'{"error":{"type":"rate_limit_error"}}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(oauth.OAuthError) as exc:
        oauth.refresh_login(make_login(ACCOUNT_A))
    assert exc.value.status == 429
    assert exc.value.retry_after == 7200


def test_default_transport_lets_url_errors_become_transient(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("dns failure")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(oauth.OAuthError):
        oauth.refresh_login(make_login(ACCOUNT_A))


def test_conftest_blocks_the_real_network_by_default():
    with pytest.raises(AssertionError, match="network blocked"):
        oauth.refresh_login(make_login(ACCOUNT_A))
