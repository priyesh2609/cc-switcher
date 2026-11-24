from __future__ import annotations

import copy
import http.client
import io
import json
import socket
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cc_switcher import usage
from cc_switcher.models import BreakdownRow, ExtraUsage, UsageSnapshot

NOW = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)
TOKEN = "sk-ant-oat01-SECRET-ACCESS"


def realistic_payload() -> dict:
    """Shaped like what the endpoint returns today, including codenames and passthrough keys."""
    return {
        "five_hour": {"utilization": 37.0, "resets_at": "2026-11-22T14:59:59.943648+00:00"},
        "seven_day": {"utilization": 62.0, "resets_at": "2026-11-27T08:00:00+00:00"},
        "seven_day_oauth_apps": None,
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 12.0, "resets_at": "2026-11-27T08:00:00Z"},
        "cinder_cove": {"utilization": 5.5, "resets_at": "2026-11-27T08:00:00+00:00"},
        "wattle_ember": {
            "label": "Holiday credit",
            "limit_dollars": 50.0,
            "used_dollars": 12.5,
            "remaining_dollars": 37.5,
            "utilization": 25.0,
            "resets_at": None,
            "periods": [
                {"starts_at": "2026-11-01T00:00:00Z", "ends_at": "2026-11-30T00:00:00Z"},
                {"starts_at": "2026-12-01T00:00:00Z", "ends_at": "2026-12-31T00:00:00Z"},
            ],
        },
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": 5000,
            "used_credits": 1234.5,
            "utilization": 24.69,
            "currency": "USD",
            "disabled_reason": None,
        },
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 37,
                "resets_at": "2026-11-22T14:59:59+00:00",
                "severity": "ok",
                "is_active": True,
                "scope": None,
            },
            {
                "kind": "weekly",
                "group": "weekly",
                "percent": 62,
                "resets_at": "2026-11-27T08:00:00+00:00",
                "severity": "approaching",
                "is_active": True,
                "scope": {"model": None, "surface": None},
            },
            {
                "kind": "weekly",
                "group": "model",
                "percent": 41.0,
                "resets_at": "2026-11-27T08:00:00+00:00",
                "severity": "approaching",
                "is_active": True,
                "scope": {"model": {"display_name": "Fable"}, "surface": None},
            },
            {
                "kind": "weekly",
                "group": "surface",
                "percent": 8,
                "resets_at": "2026-11-27T08:00:00+00:00",
                "severity": "ok",
                "is_active": True,
                "scope": {"model": None, "surface": {"display_name": "Claude Code"}},
            },
        ],
        "some_future_flag": True,
        "another_passthrough": {"foo": "bar"},
    }


@pytest.fixture(autouse=True)
def _version(monkeypatch):
    monkeypatch.setattr(usage, "_version_cache", "2.1.330")


# --------------------------------------------------------------------------- fakes


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status = status
        self.headers = headers or {}
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


class RecordingOpener:
    """Callable opener: returns a response or raises, and records each request."""

    def __init__(self, response=None, raises=None):
        self.response, self.raises = response, raises
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.raises:
            raise self.raises
        return self.response


def http_error(code, headers=None, body=b"{}"):
    return urllib.error.HTTPError("https://api.anthropic.com/api/oauth/usage", code, "err", headers or {}, io.BytesIO(body))


def fetch_error(**opener_kwargs) -> usage.UsageError:
    with pytest.raises(usage.UsageError) as exc:
        usage.fetch_usage_raw(TOKEN, opener=RecordingOpener(**opener_kwargs))
    return exc.value


# --------------------------------------------------------------------------- fetch: success


def test_fetch_sends_the_documented_request_and_returns_the_json():
    payload = realistic_payload()
    response = FakeResponse(json.dumps(payload).encode())
    opener = RecordingOpener(response)

    raw = usage.fetch_usage_raw(TOKEN, opener=opener, timeout=4)

    assert raw == payload
    (request,) = opener.requests
    assert request.full_url == "https://api.anthropic.com/api/oauth/usage"
    assert request.get_method() == "GET"
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert "user-agent" not in headers
    assert opener.timeouts == [4]
    assert response.closed


def test_fetch_accepts_an_opener_object_with_an_open_method():
    class Director:
        def open(self, request, timeout=None):
            return FakeResponse(b'{"five_hour": null}')

    assert usage.fetch_usage_raw(TOKEN, opener=Director()) == {"five_hour": None}


def test_fetch_accepts_text_bodies_and_getcode_responses():
    class Legacy:
        def read(self):
            return '{"ok": 1}'

        def getcode(self):
            return 200

    assert usage.fetch_usage_raw(TOKEN, opener=lambda request, timeout=None: Legacy()) == {"ok": 1}


def test_fetch_defaults_to_no_user_agent_transport(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request.full_url)
        return FakeResponse(b"{}")

    monkeypatch.setattr("cc_switcher.http_transport.open_without_user_agent", fake_urlopen)
    assert usage.fetch_usage_raw(TOKEN) == {}
    assert seen == ["https://api.anthropic.com/api/oauth/usage"]


def test_conftest_blocks_the_real_network_by_default():
    with pytest.raises(AssertionError, match="network blocked"):
        usage.fetch_usage_raw(TOKEN)


# --------------------------------------------------------------------------- fetch: failures


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures(status):
    error = fetch_error(raises=http_error(status))
    assert error.code == "auth_expired"
    assert error.retry_after is None


def test_429_parses_retry_after_seconds():
    error = fetch_error(raises=http_error(429, {"Retry-After": "30"}))
    assert (error.code, error.retry_after) == ("rate_limited", 30.0)


def test_429_parses_retry_after_http_date(monkeypatch):
    monkeypatch.setattr(usage, "utcnow", lambda: NOW)
    when = "Sun, 22 Nov 2026 10:02:00 GMT"  # two minutes after NOW
    error = fetch_error(raises=http_error(429, {"Retry-After": when}))
    assert error.code == "rate_limited"
    assert error.retry_after == pytest.approx(120.0)


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "soon"}, None])
def test_429_without_a_usable_retry_after(headers):
    error = fetch_error(raises=http_error(429, headers))
    assert (error.code, error.retry_after) == ("rate_limited", None)


def test_retry_after_never_shortens_a_server_wait():
    assert fetch_error(raises=http_error(429, {"Retry-After": "-5"})).retry_after == 0.0
    assert fetch_error(raises=http_error(429, {"Retry-After": "99999999"})).retry_after == 99999999.0


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_other_http_errors_carry_their_code(status):
    assert fetch_error(raises=http_error(status)).code == f"http_{status}"


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("dns failure"),
        TimeoutError("timed out"),
        socket.timeout("timed out"),
        ConnectionResetError("reset"),
        http.client.RemoteDisconnected("closed"),
    ],
)
def test_network_failures(error):
    assert fetch_error(raises=error).code == "network"


def test_non_200_success_status_is_an_http_error():
    assert fetch_error(response=FakeResponse(b"", status=204)).code == "http_204"


@pytest.mark.parametrize("body", [b"<html>cloudflare</html>", b"", b"[1, 2]", b'"text"', b"\xff\xfe", b"null"])
def test_bad_bodies(body):
    assert fetch_error(response=FakeResponse(body)).code == "bad_response"


def test_oversized_body_is_rejected():
    body = b'{"x": "' + b"a" * (usage.MAX_RESPONSE_BYTES + 1) + b'"}'
    assert fetch_error(response=FakeResponse(body)).code == "bad_response"


def test_errors_never_contain_the_access_token():
    for kwargs in (
        {"raises": http_error(401, body=TOKEN.encode())},
        {"raises": urllib.error.URLError(f"proxy echoed {TOKEN}")},
        {"raises": OSError(f"proxy echoed {TOKEN}")},
        {"response": FakeResponse(b"nope")},
    ):
        assert TOKEN not in str(fetch_error(**kwargs))


# --------------------------------------------------------------------------- parse: realistic payload


def test_parse_realistic_payload_windows():
    snap = usage.parse_usage(realistic_payload(), "acct-1", fetched_at=NOW)

    assert snap.ok and snap.error is None
    assert snap.account_id == "acct-1" and snap.fetched_at == NOW
    keys = [w.key for w in snap.windows]
    # known windows first in WINDOW_LABELS order (nulls skipped), then codenames, then scoped limits
    assert keys == [
        "five_hour",
        "seven_day",
        "seven_day_sonnet",
        "cinder_cove",
        "limit:weekly:Fable",
        "limit:weekly:Claude Code",
    ]
    assert snap.session.utilization == 37.0
    assert snap.session.label == "Session (5h)"
    assert snap.session.resets_at == datetime(2026, 11, 22, 14, 59, 59, 943648, tzinfo=timezone.utc)
    assert snap.weekly.utilization == 62.0
    assert snap.weekly.resets_at == datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
    assert snap.window("seven_day_sonnet").label == "Weekly · Sonnet"
    assert snap.window("seven_day_sonnet").resets_at == datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
    assert snap.window("seven_day_opus") is None
    assert snap.window("seven_day_oauth_apps") is None


def test_unknown_codename_window_is_kept_with_a_readable_label():
    snap = usage.parse_usage(realistic_payload(), "a", NOW)
    window = snap.window("cinder_cove")
    assert (window.label, window.utilization) == ("Cinder Cove", 5.5)  # not "cinder_cove"


def test_unknown_window_uses_its_own_label_when_it_has_one():
    raw = {"cinder_cove": {"label": "Opus 5 research", "utilization": 9, "resets_at": "2026-11-27T08:00:00Z"}}
    assert usage.parse_usage(raw, "a", NOW).window("cinder_cove").label == "Opus 5 research"


def test_scoped_limits_become_labelled_windows():
    snap = usage.parse_usage(realistic_payload(), "a", NOW)

    fable = snap.window("limit:weekly:Fable")
    assert fable.label == "Weekly · Fable"
    assert fable.utilization == 41.0
    assert fable.severity == "approaching"
    assert fable.resets_at == datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)

    surface = snap.window("limit:weekly:Claude Code")
    assert surface.label == "Weekly · Claude Code"
    assert surface.utilization == 8


def test_unscoped_limits_do_not_duplicate_the_headline_windows():
    snap = usage.parse_usage(realistic_payload(), "a", NOW)
    assert not [w for w in snap.windows if w.key.startswith("limit:session") or w.key == "limit:weekly:"]


def test_scoped_limit_matching_a_dedicated_window_adds_only_its_severity():
    raw = {
        "seven_day_opus": {"utilization": 20.0, "resets_at": "2026-11-27T08:00:00Z"},
        "limits": [
            {
                "kind": "weekly",
                "percent": 20.0,
                "resets_at": "2026-11-27T08:00:00Z",
                "severity": "ok",
                "scope": {"model": {"display_name": "Opus"}},
            }
        ],
    }
    snap = usage.parse_usage(raw, "a", NOW)

    assert [w.key for w in snap.windows] == ["seven_day_opus"]
    assert snap.window("seven_day_opus").severity == "ok"


def test_limit_kind_decides_the_label_prefix():
    raw = {
        "limits": [
            {"kind": "session", "percent": 10, "scope": {"model": {"display_name": "Fable"}}},
            {"kind": "five_hour", "percent": 11, "scope": {"model": {"display_name": "Opus"}}},
            {"kind": "monthly", "percent": 12, "scope": {"model": {"display_name": "Haiku"}}},
            {"kind": "burst_pool", "percent": 13, "scope": {"model": {"display_name": "Sonnet"}}},
        ]
    }
    labels = [w.label for w in usage.parse_usage(raw, "a", NOW).windows]
    assert labels == [
        "Session (5h) · Fable",
        "Session (5h) · Opus",
        "Monthly · Haiku",
        "Burst pool · Sonnet",
    ]


def test_model_and_surface_scope_together_keep_keys_unique():
    raw = {
        "limits": [
            {"kind": "weekly", "percent": 1, "scope": {"model": {"display_name": "Fable"}, "surface": {"display_name": "Claude Code"}}},
            {"kind": "weekly", "percent": 2, "scope": {"model": {"display_name": "Fable"}, "surface": {"display_name": "claude.ai"}}},
        ]
    }
    snap = usage.parse_usage(raw, "a", NOW)
    assert [w.key for w in snap.windows] == ["limit:weekly:Fable/Claude Code", "limit:weekly:Fable/claude.ai"]
    assert snap.windows[0].label == "Weekly · Fable · Claude Code"


def test_promo_credit_from_wattle_ember():
    snap = usage.parse_usage(realistic_payload(), "a", NOW)

    assert "wattle_ember" not in [w.key for w in snap.windows]
    (promo,) = snap.promos
    assert promo.key == "wattle_ember"
    assert promo.label == "Holiday credit"
    assert (promo.limit_dollars, promo.used_dollars, promo.remaining_dollars) == (50.0, 12.5, 37.5)
    assert promo.utilization == 25.0
    # NOW (Nov 22) falls inside the November period, so the credit resets when that period ends ...
    assert promo.resets_at == datetime(2026, 11, 30, tzinfo=timezone.utc)
    # ... and the promo as a whole ends with its last period.
    assert promo.ends_at == datetime(2026, 12, 31, tzinfo=timezone.utc)


def test_promo_derives_missing_fields_and_ignores_periods_outside_now():
    raw = {
        "wattle_ember": {
            "limit_dollars": 40,
            "used_dollars": 10,
            "periods": [{"starts_at": "2027-01-01T00:00:00Z", "ends_at": "2027-01-31T00:00:00Z"}],
        }
    }
    (promo,) = usage.parse_usage(raw, "a", NOW).promos
    assert promo.label == "Wattle Ember"  # no label sent: readable, not the raw key
    assert promo.remaining_dollars == 30.0
    assert promo.utilization == 25.0
    assert promo.resets_at is None  # no period covers NOW
    assert promo.ends_at == datetime(2027, 1, 31, tzinfo=timezone.utc)


def test_promo_prefers_an_explicit_resets_at():
    raw = {"wattle_ember": {"limit_dollars": 5, "used_dollars": 5, "resets_at": "2026-11-23T00:00:00Z"}}
    (promo,) = usage.parse_usage(raw, "a", NOW).promos
    assert promo.resets_at == datetime(2026, 11, 23, tzinfo=timezone.utc)
    assert promo.remaining_dollars == 0.0


def test_extra_usage_block_is_in_minor_units():
    extra = usage.parse_usage(realistic_payload(), "a", NOW).extra_usage

    assert extra.enabled is True
    assert extra.monthly_limit == 5000  # cents
    assert extra.used_credits == 1234.5
    assert extra.utilization == 24.69
    assert extra.currency == "USD"
    assert extra.disabled_reason is None
    assert extra.remaining == 3765.5
    assert usage.format_money(extra.used_credits, extra.currency) == "$12.35"
    assert usage.format_money(extra.monthly_limit, extra.currency) == "$50.00"


def test_disabled_extra_usage_keeps_the_reason():
    raw = {
        "extra_usage": {
            "is_enabled": False,
            "monthly_limit": None,
            "used_credits": None,
            "utilization": None,
            "currency": None,
            "disabled_reason": "overage_not_provisioned",
        }
    }
    extra = usage.parse_usage(raw, "a", NOW).extra_usage
    assert extra.enabled is False
    assert extra.disabled_reason == "overage_not_provisioned"
    assert extra.remaining is None


def test_extra_usage_utilization_is_derived_when_absent():
    raw = {"extra_usage": {"is_enabled": True, "monthly_limit": 2000, "used_credits": 500, "currency": "EUR"}}
    assert usage.parse_usage(raw, "a", NOW).extra_usage.utilization == 25.0


def test_extra_usage_absent_or_null():
    assert usage.parse_usage({}, "a", NOW).extra_usage is None
    assert usage.parse_usage({"extra_usage": None}, "a", NOW).extra_usage is None


def test_raw_response_is_kept_and_unknown_keys_survive():
    payload = realistic_payload()
    snap = usage.parse_usage(payload, "a", NOW)
    assert snap.raw == payload
    assert snap.raw["some_future_flag"] is True
    assert snap.raw is not payload  # a copy, so later mutation of the response cannot change the snapshot


def test_token_looking_strings_never_reach_the_stored_raw():
    payload = realistic_payload()
    payload["note"] = "echo sk-ant-oat01-LEAKED-TOKEN-123 end"
    payload["nested"] = {"items": ["sk-ant-ort01-ANOTHER"]}
    snap = usage.parse_usage(payload, "a", NOW)
    dumped = json.dumps(snap.raw)
    assert "LEAKED" not in dumped and "ANOTHER" not in dumped
    assert "[redacted]" in dumped


def test_fetched_at_defaults_to_now():
    before = datetime.now(timezone.utc)
    snap = usage.parse_usage({}, "a")
    assert before <= snap.fetched_at <= datetime.now(timezone.utc)


def test_empty_payload_is_ok_with_no_windows():
    snap = usage.parse_usage({}, "a", NOW)
    assert snap.ok and snap.windows == () and snap.promos == ()
    assert snap.max_utilization() is None


def test_utilization_over_100_is_preserved():
    snap = usage.parse_usage({"five_hour": {"utilization": 103.2, "resets_at": None}}, "a", NOW)
    assert snap.session.utilization == 103.2
    assert snap.session.resets_at is None


def test_zero_utilization_is_a_real_value_not_missing():
    snap = usage.parse_usage({"five_hour": {"utilization": 0, "resets_at": None}}, "a", NOW)
    assert snap.session is not None and snap.session.utilization == 0.0


def test_window_with_nothing_known_is_skipped():
    snap = usage.parse_usage({"five_hour": {"utilization": None, "resets_at": None}, "cinder_cove": {}}, "a", NOW)
    assert snap.windows == ()


def test_resets_at_accepts_epoch_seconds_and_milliseconds():
    seconds = 1_795_000_000
    raw = {
        "five_hour": {"utilization": 1, "resets_at": seconds},
        "seven_day": {"utilization": 2, "resets_at": seconds * 1000},
        "seven_day_sonnet": {"utilization": 3, "resets_at": str(seconds)},
    }
    snap = usage.parse_usage(raw, "a", NOW)
    expected = datetime.fromtimestamp(seconds, timezone.utc)
    assert [w.resets_at for w in snap.windows] == [expected, expected, expected]


def test_numeric_strings_are_accepted_for_utilization():
    snap = usage.parse_usage({"five_hour": {"utilization": "42.5", "resets_at": None}}, "a", NOW)
    assert snap.session.utilization == 42.5


@pytest.mark.parametrize(
    "raw",
    [
        [],
        None,
        "five_hour",
        42,
    ],
)
def test_non_object_payload_is_a_bad_response_not_an_exception(raw):
    snap = usage.parse_usage(raw, "a", NOW)
    assert (snap.ok, snap.error, snap.windows) == (False, "bad_response", ())


@pytest.mark.parametrize(
    "raw",
    [
        {"five_hour": []},
        {"five_hour": "37%"},
        {"five_hour": 37},
        {"five_hour": {"utilization": "abc", "resets_at": "not a date"}},
        {"five_hour": {"utilization": float("nan"), "resets_at": float("inf")}},
        {"five_hour": {"utilization": True, "resets_at": True}},
        {"five_hour": {"utilization": {"nested": 1}, "resets_at": ["x"]}},
        {"five_hour": {"utilization": 5, "resets_at": 10**30}},
        {"limits": "none"},
        {"limits": {"kind": "weekly"}},
        {"limits": [None, 3, "x", [], {}, {"kind": None, "percent": "x", "scope": "model"}]},
        {"limits": [{"kind": 7, "percent": 5, "scope": {"model": {"display_name": 99}}}]},
        {"limits": [{"kind": "weekly", "percent": 5, "scope": {"model": "Fable"}}]},
        {"extra_usage": []},
        {"extra_usage": {"is_enabled": "yes", "monthly_limit": "lots", "used_credits": {}, "currency": 5}},
        {"wattle_ember": {"limit_dollars": "x", "used_dollars": None, "periods": "forever"}},
        {"wattle_ember": {"limit_dollars": 5, "periods": [None, 3, {"starts_at": 1, "ends_at": "bad"}]}},
        {"cinder_cove": {"utilization": None}},
        {"x": {"y": {"z": {}}}, "list": [1, 2, 3], "n": None},
        {1: {"utilization": 5}},
    ],
)
def test_parse_never_raises_on_unknown_shapes(raw):
    snap = usage.parse_usage(raw, "a", NOW)
    assert isinstance(snap, UsageSnapshot) and snap.ok


def test_parse_does_not_mutate_its_input():
    payload = realistic_payload()
    before = copy.deepcopy(payload)
    usage.parse_usage(payload, "a", NOW)
    assert payload == before


# --------------------------------------------------------------------------- format_reset


@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(seconds=-5), "now"),
        (timedelta(0), "now"),
        (timedelta(seconds=20), "1m"),  # never "0m"
        (timedelta(minutes=42, seconds=59), "42m"),
        (timedelta(hours=1), "1h 00m"),
        (timedelta(hours=3, minutes=5, seconds=30), "3h 05m"),
        (timedelta(hours=23, minutes=59), "23h 59m"),
        (timedelta(days=1), "1d 0h"),
        (timedelta(days=2, hours=4, minutes=59), "2d 4h"),
    ],
)
def test_format_reset(delta, expected):
    assert usage.format_reset(NOW + delta, now=NOW) == expected


def test_format_reset_unknown_and_naive_inputs():
    assert usage.format_reset(None) == "—"
    naive = datetime(2026, 11, 22, 11, 0, 0)  # treated as UTC
    assert usage.format_reset(naive, now=NOW) == "1h 00m"


def test_format_reset_defaults_to_the_current_time():
    assert usage.format_reset(datetime.now(timezone.utc) + timedelta(minutes=30, seconds=30)) == "30m"


# --------------------------------------------------------------------------- format_money / percent


@pytest.mark.parametrize(
    "minor, currency, expected",
    [
        (1234, "USD", "$12.34"),
        (500, "EUR", "€5.00"),
        (1234, "GBP", "12.34 GBP"),
        (0, "USD", "$0.00"),
        (5, "USD", "$0.05"),
        (1234, None, "$12.34"),  # no currency reported: USD
        (1234, "", "$12.34"),
        (1234, "usd", "$12.34"),
        (123456789, "USD", "$1,234,567.89"),
        (1234.5, "USD", "$12.35"),  # half rounds up
        (1234.4, "USD", "$12.34"),
        (-250, "USD", "-$2.50"),
        (1500, "JPY", "1,500 JPY"),  # zero-decimal currency: minor == major
        (None, "USD", "—"),
        (float("nan"), "USD", "—"),
    ],
)
def test_format_money(minor, currency, expected):
    assert usage.format_money(minor, currency) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "0%"),
        (42, "42%"),
        (42.4, "42%"),
        (42.5, "43%"),
        (99.6, "100%"),
        (103.2, "103%"),
        (-3, "0%"),
        (None, "—"),
        (float("nan"), "—"),
        ("42", "42%"),
        (True, "—"),
    ],
)
def test_format_percent(value, expected):
    assert usage.format_percent(value) == expected


# --------------------------------------------------------------------------- parse_datetime


@pytest.mark.parametrize(
    "value",
    ["2026-11-22T10:00:00Z", "2026-11-22T10:00:00z", "2026-11-22T10:00:00+00:00", "2026-11-22T12:00:00+02:00", "2026-11-22T10:00:00", 1795341600],
)
def test_parse_datetime_same_instant(value):
    assert usage.parse_datetime(value) == datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)


def test_parse_datetime_always_returns_aware_utc():
    for value in ("2026-11-22T10:00:00", "2026-11-22T10:00:00+05:30", 1_795_000_000):
        parsed = usage.parse_datetime(value)
        assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize("value", [None, "", "  ", "tomorrow", True, [], {}, float("inf"), 10**30])
def test_parse_datetime_garbage_is_none(value):
    assert usage.parse_datetime(value) is None


# --------------------------------------------------------------------------- claude_code_version


def runner_returning(stdout="2.1.330 (Claude Code)\n", returncode=0, calls=None):
    def run(argv):
        if calls is not None:
            calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    return run


@pytest.fixture
def fresh_version(monkeypatch):
    monkeypatch.setattr(usage, "_version_cache", None)
    monkeypatch.setattr(usage, "_find_claude", lambda: "/opt/homebrew/bin/claude")


def test_usage_finds_the_explicit_app_claude_binary(monkeypatch, tmp_path):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\\n")
    binary.chmod(0o755)
    monkeypatch.setenv("CC_SWITCHER_CLAUDE_PATH", str(binary))
    monkeypatch.setattr(usage.shutil, "which", lambda name: "/older/bin/claude")
    assert usage._find_claude() == str(binary)


def test_version_is_parsed_from_claude_version_output(fresh_version):
    calls = []
    assert usage.claude_code_version(runner_returning("2.7.14 (Claude Code)\n", calls=calls)) == "2.7.14"
    assert calls == [["/opt/homebrew/bin/claude", "--version"]]


def test_version_is_cached(fresh_version):
    calls = []
    run = runner_returning("2.7.14 (Claude Code)", calls=calls)
    usage.claude_code_version(run)
    usage.claude_code_version(run)
    assert len(calls) == 1


@pytest.mark.parametrize("stdout, returncode", [("", 0), ("command not found", 0), ("2.7.14", 1)])
def test_version_falls_back_on_unusable_output(fresh_version, stdout, returncode):
    assert usage.claude_code_version(runner_returning(stdout, returncode)) == "2.1.330"


def test_version_falls_back_when_the_runner_raises(fresh_version):
    def boom(argv):
        raise FileNotFoundError("claude")

    assert usage.claude_code_version(boom) == "2.1.330"


def test_version_without_a_binary_or_runner_does_not_spawn_anything(monkeypatch):
    monkeypatch.setattr(usage, "_version_cache", None)
    monkeypatch.setattr(usage, "_find_claude", lambda: None)
    # conftest makes any real subprocess raise AssertionError; the fallback must not even try
    assert usage.claude_code_version() == "2.1.330"


def test_version_with_a_runner_but_no_binary_still_asks_the_runner(monkeypatch):
    monkeypatch.setattr(usage, "_version_cache", None)
    monkeypatch.setattr(usage, "_find_claude", lambda: None)
    calls = []
    assert usage.claude_code_version(runner_returning("2.9.0", calls=calls)) == "2.9.0"
    assert calls == [["claude", "--version"]]


def test_version_swallows_the_blocked_default_runner(monkeypatch):
    monkeypatch.setattr(usage, "_version_cache", None)
    monkeypatch.setattr(usage, "_find_claude", lambda: "/usr/local/bin/claude")
    assert usage.claude_code_version() == "2.1.330"  # subprocess is blocked in tests -> fallback


def test_user_agent_uses_the_version(monkeypatch):
    monkeypatch.setattr(usage, "_version_cache", "2.5.1")
    assert usage.user_agent() == "claude-code/2.5.1"


# =========================================================================== redeemable resets (cedar_ember)

from pathlib import Path  # noqa: E402

from cc_switcher.models import ResetGrant, ResetStatus  # noqa: E402

RESETS_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"


def cedar_block() -> dict:
    """Shaped like the `cedar_ember` block `/limit-reset` reads: usable, paused, expired and broken grants."""
    return {
        "eligible": True,
        "ineligible_reason": None,
        "at_limit": True,
        "exhausted": ["seven_day"],
        "grants": [
            {
                "id": "grant_weekly_01",
                "label": "Weekly reset",
                "type": "weekly",
                "resets_total": 3,
                "resets_left": 2,
                "starts_at": "2026-11-01T00:00:00Z",
                "ends_at": "2026-12-01T00:00:00+00:00",
                "clears": ["seven_day", "seven_day_sonnet"],
                "paused": False,
                "usable_now": True,
                "use_requires_limit": True,
                "percent_used": {"seven_day": 99.0},
                "blocking": [],
                "gives_back_weekly": True,
                "unavailable_reason": None,
            },
            {
                "id": "grant_session_paused",
                "label": "Session reset",
                "type": "session",
                "resets_total": 5,
                "resets_left": 5,
                "starts_at": "2026-11-01T00:00:00Z",
                "ends_at": "2026-12-15T00:00:00Z",
                "paused": True,
                "usable_now": False,
                "use_requires_limit": True,
                "gives_back_weekly": False,
                "unavailable_reason": "paused_by_admin",
            },
            {
                "id": "grant_full_expired",
                "label": "Full reset",
                "type": "full",
                "resets_total": 1,
                "resets_left": 1,
                "starts_at": "2026-09-01T00:00:00Z",
                "ends_at": "2026-10-01T00:00:00Z",
                "paused": False,
                "usable_now": False,
                "unavailable_reason": "expired",
            },
            {"id": "broken_negative", "label": "Broken", "resets_left": -1},
            {"id": "broken_string", "label": "Broken", "resets_left": "2"},
            {"id": "broken_missing", "label": "Broken"},
            "not a grant",
            None,
        ],
        "next_grant_id": "grant_weekly_01",
        "weekly_resets_at": "2026-11-27T08:00:00Z",
        "cooldown_until": "2026-11-22T18:00:00+00:00",
        "event_props": {"surface": "cli", "anything": [1, 2, 3]},
    }


def grants_by_id(status: ResetStatus) -> dict[str, ResetGrant]:
    return {g.id: g for g in status.grants}


def test_parse_resets_realistic_block():
    status = usage.parse_resets(cedar_block())

    assert isinstance(status, ResetStatus)
    assert status.eligible is True
    assert status.ineligible_reason is None
    assert status.at_limit is True
    assert status.weekly_resets_at == datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
    assert status.cooldown_until == datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)
    # usable, paused and expired grants are kept (the UI decides what to show); the five broken rows are not
    assert [g.id for g in status.grants] == ["grant_weekly_01", "grant_session_paused", "grant_full_expired"]


def test_a_usable_grant():
    grant = grants_by_id(usage.parse_resets(cedar_block()))["grant_weekly_01"]
    assert grant == ResetGrant(
        id="grant_weekly_01",
        label="Weekly reset",
        type="weekly",
        resets_total=3,
        resets_left=2,
        usable_now=True,
        use_requires_limit=True,
        gives_back_weekly=True,
        paused=False,
        starts_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 12, 1, tzinfo=timezone.utc),
        unavailable_reason=None,
    )


def test_a_paused_grant_is_kept_and_flagged():
    grant = grants_by_id(usage.parse_resets(cedar_block()))["grant_session_paused"]
    assert (grant.paused, grant.usable_now, grant.resets_left, grant.type) == (True, False, 5, "session")
    assert grant.unavailable_reason == "paused_by_admin"


def test_an_expired_grant_is_kept_with_its_past_end_date():
    grant = grants_by_id(usage.parse_resets(cedar_block()))["grant_full_expired"]
    assert grant.ends_at == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert (grant.usable_now, grant.unavailable_reason, grant.gives_back_weekly) == (False, "expired", False)


def test_model_helpers_over_the_parsed_status():
    status = usage.parse_resets(cedar_block())
    assert status.resets_left == 3  # 2 usable + 1 expired-but-unpaused; the paused 5 are excluded
    assert status.next_expiry == datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("block", [None, [], "cedar_ember", 7, True, [{"eligible": True}]])
def test_missing_or_non_object_block_is_none(block):
    assert usage.parse_resets(block) is None


def test_empty_object_is_an_ineligible_status_not_none():
    status = usage.parse_resets({})
    assert status == ResetStatus(eligible=False)
    assert status.grants == () and status.resets_left == 0 and status.next_expiry is None


def test_ineligible_block_keeps_its_reason():
    status = usage.parse_resets({"eligible": False, "ineligible_reason": "plan_not_supported", "grants": []})
    assert (status.eligible, status.ineligible_reason) == (False, "plan_not_supported")


def test_missing_eligible_flag_defaults_to_having_a_grant():
    with_grant = usage.parse_resets({"grants": [{"resets_left": 1}]})
    without = usage.parse_resets({"grants": [{"resets_left": "x"}]})
    assert with_grant.eligible is True and without.eligible is False
    assert usage.parse_resets({"eligible": "yes", "grants": []}).eligible is False  # non-bool is ignored


def test_a_grant_needs_only_resets_left_everything_else_has_defaults():
    (grant,) = usage.parse_resets({"grants": [{"resets_left": 0}]}).grants
    assert grant == ResetGrant(
        id="grant:0",
        label="Limit reset",
        type=None,
        resets_total=0,
        resets_left=0,
        usable_now=False,
        use_requires_limit=True,  # the safe default: only usable at a limit
        gives_back_weekly=False,
        paused=False,
        starts_at=None,
        ends_at=None,
        unavailable_reason=None,
    )


@pytest.mark.parametrize(
    "value, expected",
    [(0, 0), (1, 1), (7, 7), (2.0, 2), (1.5, None), (-1, None), (-0.5, None), ("2", None), (True, None), (None, None), ([2], None), (float("nan"), None), (float("inf"), None)],
)
def test_resets_left_must_be_a_non_negative_whole_number(value, expected):
    status = usage.parse_resets({"grants": [{"id": "g", "resets_left": value}]})
    if expected is None:
        assert status.grants == ()
    else:
        assert status.grants[0].resets_left == expected
        assert isinstance(status.grants[0].resets_left, int)


def test_resets_total_never_drops_below_resets_left():
    grants = usage.parse_resets(
        {
            "grants": [
                {"id": "a", "resets_left": 3, "resets_total": 1},  # server inconsistency
                {"id": "b", "resets_left": 2, "resets_total": "lots"},  # unusable total
                {"id": "c", "resets_left": 2, "resets_total": 5},
            ]
        }
    ).grants
    assert [(g.resets_left, g.resets_total) for g in grants] == [(3, 3), (2, 2), (2, 5)]


def test_grant_types_and_default_labels():
    grants = usage.parse_resets(
        {
            "grants": [
                {"id": "a", "resets_left": 1, "type": "session"},
                {"id": "b", "resets_left": 1, "type": "Weekly"},
                {"id": "c", "resets_left": 1, "type": "full", "label": "Everything"},
                {"id": "d", "resets_left": 1, "type": "monthly"},  # unknown type: None, label falls back
                {"id": "e", "resets_left": 1, "type": 5},
            ]
        }
    ).grants
    assert [(g.type, g.label) for g in grants] == [
        ("session", "Session reset"),
        ("weekly", "Weekly reset"),
        ("full", "Everything"),
        (None, "Limit reset"),
        (None, "Limit reset"),
    ]


def test_grant_ids_are_kept_stringified_or_synthesised_by_position():
    grants = usage.parse_resets(
        {"grants": [{"id": 7, "resets_left": 1}, {"resets_left": 1}, {"id": True, "resets_left": 1}, {"id": "", "resets_left": 1}]}
    ).grants
    assert [g.id for g in grants] == ["7", "grant:1", "grant:2", "grant:3"]


def test_non_boolean_flags_fall_back_to_their_defaults():
    (grant,) = usage.parse_resets(
        {"grants": [{"resets_left": 1, "paused": "yes", "usable_now": 1, "use_requires_limit": None, "gives_back_weekly": "true"}]}
    ).grants
    assert (grant.paused, grant.usable_now, grant.use_requires_limit, grant.gives_back_weekly) == (False, False, True, False)


def test_unparseable_dates_become_none_without_dropping_the_grant():
    status = usage.parse_resets(
        {"weekly_resets_at": "soon", "cooldown_until": {}, "grants": [{"resets_left": 1, "starts_at": "x", "ends_at": []}]}
    )
    assert (status.weekly_resets_at, status.cooldown_until) == (None, None)
    assert len(status.grants) == 1
    assert (status.grants[0].starts_at, status.grants[0].ends_at) == (None, None)


@pytest.mark.parametrize("grants", ["none", {"id": "x", "resets_left": 1}, 5, None])
def test_grants_that_are_not_a_list_mean_no_grants(grants):
    assert usage.parse_resets({"eligible": True, "grants": grants}).grants == ()


@pytest.mark.parametrize(
    "block",
    [
        {"grants": [[], 3, "x", None, {}, {"resets_left": {}}, {"resets_left": 1, "label": 5, "type": [], "starts_at": {}, "unavailable_reason": 9}]},
        {"eligible": [], "ineligible_reason": 5, "at_limit": "no", "weekly_resets_at": [], "cooldown_until": {}},
        {"grants": [{"resets_left": 10**400}]},
        {1: 2, None: 3},
    ],
)
def test_parse_resets_never_raises(block):
    assert usage.parse_resets(block) is None or isinstance(usage.parse_resets(block), ResetStatus)


def test_parse_resets_does_not_mutate_its_input():
    block = cedar_block()
    before = copy.deepcopy(block)
    usage.parse_resets(block)
    assert block == before


def test_fields_we_do_not_display_are_ignored():
    status = usage.parse_resets(cedar_block())
    assert not hasattr(status, "event_props") and not hasattr(status, "exhausted")


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"utilization": 3.0, "resets_at": "2026-11-27T08:00:00Z"},  # a future server that adds window-like keys
        {"limit_dollars": 5, "used_dollars": 1},  # ... or promo-like keys
    ],
)
def test_a_cedar_ember_block_inside_a_plain_usage_payload_is_not_mistaken_for_a_window_or_promo(extra):
    raw = {"five_hour": {"utilization": 5, "resets_at": None}, "cedar_ember": {**cedar_block(), **extra}}
    snap = usage.parse_usage(raw, "a", NOW)
    assert [w.key for w in snap.windows] == ["five_hour"]
    assert snap.promos == ()
    assert snap.resets is None  # resets come from the dedicated request, never from parse_usage


# --------------------------------------------------------------------------- fetch_resets_raw


def test_fetch_resets_is_a_read_only_get_of_the_documented_url():
    payload = {"five_hour": None, "cedar_ember": cedar_block()}
    opener = RecordingOpener(FakeResponse(json.dumps(payload).encode()))

    raw = usage.fetch_resets_raw(TOKEN, opener=opener, timeout=3)

    assert raw == payload
    (request,) = opener.requests
    assert request.full_url == RESETS_URL
    assert request.get_method() == "GET"
    assert request.data is None  # no body: nothing is being claimed or spent
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert "user-agent" not in headers
    assert opener.timeouts == [3]


def test_plain_usage_requests_carry_no_query_string():
    opener = RecordingOpener(FakeResponse(b"{}"))
    usage.fetch_usage_raw(TOKEN, opener=opener)
    assert opener.requests[0].full_url == "https://api.anthropic.com/api/oauth/usage"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&at_wall=1",
        "https://api.anthropic.com/api/oauth/usage?AT_WALL=1",
        "https://api.anthropic.com/api/organizations/org-1/reset_rate_limits",
        "https://api.anthropic.com/api/organizations/org-1/Reset_Rate_Limits?x=1",
    ],
)
def test_requests_with_side_effects_are_refused_before_anything_is_sent(url):
    opener = RecordingOpener(FakeResponse(b"{}"))
    with pytest.raises(ValueError, match="side effects"):
        usage._get_json(url, TOKEN, opener=opener, timeout=1)
    assert opener.requests == []


def test_no_module_requests_the_impression_variant_or_posts_a_reset():
    package = Path(usage.__file__).parent
    for name in ("usage.py", "monitor.py", "tokens.py", "oauth.py", "autoswitch.py"):
        text = (package / name).read_text()
        assert "at_wall=1" not in text, name
    usage_text = (package / "usage.py").read_text()
    assert usage_text.count("reset_rate_limits") == 1  # only the refusal list names it
    assert 'method="POST"' not in usage_text  # the usage module never POSTs anything


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"raises": http_error(401)}, "auth_expired"),
        ({"raises": http_error(403)}, "auth_expired"),
        ({"raises": http_error(429, {"Retry-After": "90"})}, "rate_limited"),
        ({"raises": http_error(500)}, "http_500"),
        ({"raises": urllib.error.URLError("down")}, "network"),
        ({"response": FakeResponse(b"<html>")}, "bad_response"),
    ],
)
def test_fetch_resets_maps_errors_like_the_usage_fetch(kwargs, code):
    with pytest.raises(usage.UsageError) as exc:
        usage.fetch_resets_raw(TOKEN, opener=RecordingOpener(**kwargs))
    assert exc.value.code == code
    assert TOKEN not in str(exc.value)


def test_fetch_resets_retry_after_is_carried():
    with pytest.raises(usage.UsageError) as exc:
        usage.fetch_resets_raw(TOKEN, opener=RecordingOpener(raises=http_error(429, {"Retry-After": "90"})))
    assert exc.value.retry_after == 90.0


# =========================================================================== a synthetic example response

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_FETCHED_AT = datetime(2025, 2, 1, 4, 2, 11, tzinfo=timezone.utc)


def example_usage() -> dict:
    """A synthetic example of a plain usage response. A fresh copy per call."""
    return json.loads((FIXTURES / "usage_example.json").read_text())


def example_snapshot(raw: dict | None = None) -> UsageSnapshot:
    return usage.parse_usage(example_usage() if raw is None else raw, "acct", EXAMPLE_FETCHED_AT)


def test_the_fixture_really_is_the_response_we_think_it_is():
    """Guards the tests below against a replaced fixture turning them vacuous."""
    raw = example_usage()
    assert sum(1 for value in raw.values() if value is None) >= 25  # ~30 codename / future keys, all null
    assert {"tangelo", "iguana_necktie", "amber_ladder", "pace", "indicator", "cedar_ember"} <= set(raw)
    assert raw["extra_usage"]["spend_limit_reached"] is True and raw["spend"]["percent"] == 100


# --------------------------------------------------------------------------- windows


def test_example_response_with_its_30_odd_null_keys_parses_to_exactly_two_windows():
    snap = example_snapshot()

    assert snap.ok and snap.error is None
    assert [(w.key, w.utilization) for w in snap.windows] == [("five_hour", 7.0), ("seven_day", 31.0)]
    assert [w.label for w in snap.windows] == ["Session (5h)", "Weekly"]
    assert snap.promos == ()
    assert snap.resets is None  # parse_usage never reads cedar_ember


def test_example_reset_times_are_iso_with_microseconds_and_a_utc_offset():
    five, seven = example_snapshot().windows
    assert five.resets_at == datetime(2025, 2, 1, 9, 0, 0, 123456, tzinfo=timezone.utc)
    assert seven.resets_at == datetime(2025, 2, 5, 23, 0, 0, 234567, tzinfo=timezone.utc)
    assert usage.format_reset(five.resets_at, now=EXAMPLE_FETCHED_AT) == "4h 57m"


def test_limits_rows_without_a_scope_add_no_duplicate_windows():
    snap = example_snapshot()
    keys = [w.key for w in snap.windows]
    assert len(keys) == len(set(keys)) == 2
    assert not [k for k in keys if k.startswith("limit:")]


def test_limits_severity_is_carried_onto_the_headline_windows():
    five, seven = example_snapshot().windows
    assert (five.severity, seven.severity) == ("normal", "normal")  # limits[kind=session / weekly_all]


def test_a_non_normal_limits_severity_reaches_the_right_window():
    raw = example_usage()
    raw["limits"][1]["severity"] = "critical"  # weekly_all
    five, seven = example_snapshot(raw).windows
    assert (five.severity, seven.severity) == ("normal", "critical")


def test_a_windows_own_severity_wins_over_the_limits_row():
    raw = example_usage()
    raw["seven_day"]["severity"] = "approaching"
    raw["limits"][1]["severity"] = "critical"
    assert example_snapshot(raw).window("seven_day").severity == "approaching"


@pytest.mark.parametrize("kind, key", [("session", "five_hour"), ("five_hour", "five_hour"), ("weekly_all", "seven_day"), ("weekly", "seven_day"), ("Seven_Day", "seven_day")])
def test_unscoped_limit_kinds_map_to_their_headline_window(kind, key):
    raw = {
        "five_hour": {"utilization": 1, "resets_at": None},
        "seven_day": {"utilization": 2, "resets_at": None},
        "limits": [{"kind": kind, "percent": 9, "severity": "used_up", "scope": None}],
    }
    snap = usage.parse_usage(raw, "a", NOW)
    assert {w.key: w.severity for w in snap.windows} == {"five_hour": None, "seven_day": None} | {key: "used_up"}


def test_unscoped_limit_with_an_unknown_kind_or_no_matching_window_changes_nothing():
    raw = {
        "five_hour": {"utilization": 1, "resets_at": None},
        "limits": [
            {"kind": "mystery", "severity": "used_up", "scope": None},
            {"kind": "weekly_all", "severity": "used_up", "scope": None},  # no seven_day window to attach to
            {"kind": "session", "severity": 5, "scope": None},  # not a string
        ],
    }
    snap = usage.parse_usage(raw, "a", NOW)
    assert [(w.key, w.severity) for w in snap.windows] == [("five_hour", None)]  # and no new window was invented


def test_a_scoped_limits_row_still_makes_its_own_window_beside_the_real_ones():
    raw = example_usage()
    raw["limits"].append({"kind": "weekly_model", "group": "weekly", "percent": 40, "severity": "normal",
                          "resets_at": "2026-11-27T22:59:59+00:00", "scope": {"model": {"display_name": "Fable"}}, "is_active": False})
    assert [w.key for w in example_snapshot(raw).windows] == ["five_hour", "seven_day", "limit:weekly_model:Fable"]


def test_example_windows_ignore_their_null_dollar_and_lock_fields():
    raw = example_usage()
    assert raw["five_hour"]["limit_dollars"] is None and raw["five_hour"]["locked_reason"] is None
    assert example_snapshot(raw).promos == ()


# --------------------------------------------------------------------------- a window-shaped block with null dollar fields is a window


def test_an_unknown_window_with_null_dollar_fields_is_a_window_not_an_empty_promo():
    """The example five_hour/seven_day carry limit_dollars: null etc., so a future codename window will too."""
    raw = {
        "seven_day_cowork": {
            "utilization": 12.0,
            "resets_at": "2025-02-05T23:00:00.234567+00:00",
            "limit_dollars": None,
            "used_dollars": None,
            "remaining_dollars": None,
            "locked_reason": None,
        }
    }
    snap = usage.parse_usage(raw, "a", NOW)
    assert [(w.key, w.utilization) for w in snap.windows] == [("seven_day_cowork", 12.0)]
    assert snap.promos == ()


def test_a_block_with_real_dollar_amounts_is_still_a_promo():
    raw = {"wattle_ember": {"utilization": 25.0, "limit_dollars": 50, "used_dollars": 12.5}}
    snap = usage.parse_usage(raw, "a", NOW)
    assert snap.windows == () and [p.key for p in snap.promos] == ["wattle_ember"]


@pytest.mark.parametrize("amounts", [{"limit_dollars": None, "used_dollars": None}, {"limit_dollars": "n/a"}, {}])
def test_a_block_with_no_numbers_at_all_is_ignored(amounts):
    snap = usage.parse_usage({"amber_ladder": {"label": "x", "periods": [], **amounts}}, "a", NOW)
    assert snap.windows == () and snap.promos == ()


# --------------------------------------------------------------------------- extra usage


def test_example_extra_usage_the_cap_was_exceeded_and_extra_usage_is_off():
    extra = example_snapshot().extra_usage

    assert extra == ExtraUsage(
        enabled=False,
        monthly_limit=5000.0,
        used_credits=6125.0,
        utilization=100.0,
        currency="USD",
        disabled_reason="org_level_disabled_until",
        spend_limit_reached=True,
        user_disabled=False,
        decimal_places=2,
    )
    assert extra.remaining == 0.0  # used 61.25 of 50.00: nothing left, never negative


def test_example_extra_usage_amounts_are_minor_units():
    extra = example_snapshot().extra_usage
    assert usage.format_money(extra.used_credits, extra.currency, extra.decimal_places) == "$61.25"
    assert usage.format_money(extra.monthly_limit, extra.currency, extra.decimal_places) == "$50.00"


@pytest.mark.parametrize(
    "field, junk, expected",
    [
        ("spend_limit_reached", "yes", None),
        ("spend_limit_reached", 1, None),
        ("user_disabled", None, None),
        ("user_disabled", "false", None),
        ("decimal_places", "2", None),
        ("decimal_places", 2.5, None),
        ("decimal_places", -1, None),
        ("decimal_places", 7, None),
        ("decimal_places", 10**9, None),
        ("decimal_places", True, None),
        ("decimal_places", 0, 0),
        ("decimal_places", 3, 3),
        ("decimal_places", 2.0, 2),
    ],
)
def test_the_new_extra_usage_fields_are_validated(field, junk, expected):
    raw = example_usage()
    raw["extra_usage"][field] = junk
    assert getattr(example_snapshot(raw).extra_usage, field) == expected


def test_extra_usage_from_an_older_response_has_none_for_the_new_fields():
    raw = {"extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 10, "utilization": 0.2, "currency": "USD", "disabled_reason": None}}
    extra = usage.parse_usage(raw, "a", NOW).extra_usage
    assert (extra.spend_limit_reached, extra.user_disabled, extra.decimal_places) == (None, None, None)


# --------------------------------------------------------------------------- spend (fallback for extra_usage)


def test_spend_builds_extra_usage_when_extra_usage_is_absent():
    raw = example_usage()
    del raw["extra_usage"]

    extra = example_snapshot(raw).extra_usage

    assert extra == ExtraUsage(
        enabled=False,
        monthly_limit=5000.0,
        used_credits=6125.0,
        utilization=100.0,
        currency="USD",
        disabled_reason="org_level_disabled_until",
        spend_limit_reached=None,  # spend has no such flag: unknown, not guessed
        user_disabled=None,
        decimal_places=2,
    )
    assert extra.remaining == 0.0


@pytest.mark.parametrize("absent", [None, [], "off", 5])
def test_a_null_or_non_object_extra_usage_also_falls_back_to_spend(absent):
    raw = example_usage()
    raw["extra_usage"] = absent
    extra = example_snapshot(raw).extra_usage
    assert extra is not None and (extra.monthly_limit, extra.used_credits, extra.decimal_places) == (5000.0, 6125.0, 2)


def test_extra_usage_is_preferred_when_both_blocks_exist():
    raw = example_usage()
    raw["spend"]["used"]["amount_minor"] = 5
    raw["spend"]["limit"]["amount_minor"] = 50
    raw["spend"]["enabled"] = True

    extra = example_snapshot(raw).extra_usage

    assert (extra.monthly_limit, extra.used_credits, extra.enabled, extra.spend_limit_reached) == (5000.0, 6125.0, False, True)


def test_spend_exponent_is_the_decimal_places_and_drives_the_amounts():
    raw = {"spend": {"used": {"amount_minor": 1500, "currency": "JPY", "exponent": 0},
                     "limit": {"amount_minor": 20000, "currency": "JPY", "exponent": 0}, "enabled": True, "percent": 7.5}}
    extra = usage.parse_usage(raw, "a", NOW).extra_usage
    assert (extra.decimal_places, extra.currency, extra.enabled, extra.utilization) == (0, "JPY", True, 7.5)
    assert usage.format_money(extra.used_credits, extra.currency, extra.decimal_places) == "1,500 JPY"


def test_spend_with_different_exponents_is_brought_to_one_unit():
    raw = {"spend": {"used": {"amount_minor": 250, "currency": "USD", "exponent": 2},
                     "limit": {"amount_minor": 10000, "currency": "USD", "exponent": 3}}}
    extra = usage.parse_usage(raw, "a", NOW).extra_usage
    assert (extra.decimal_places, extra.monthly_limit, extra.used_credits) == (3, 10000.0, 2500.0)  # $2.50 of $10.000
    assert extra.utilization == 25.0  # derived: no percent was sent


def test_spend_with_only_a_limit_or_only_an_enabled_flag_is_still_something():
    only_limit = usage.parse_usage({"spend": {"limit": {"amount_minor": 1000, "currency": "USD", "exponent": 2}}}, "a", NOW).extra_usage
    assert (only_limit.monthly_limit, only_limit.used_credits, only_limit.enabled, only_limit.remaining) == (1000.0, None, False, None)

    only_flag = usage.parse_usage({"spend": {"enabled": True}}, "a", NOW).extra_usage
    assert only_flag.enabled is True and only_flag.monthly_limit is None


@pytest.mark.parametrize(
    "spend",
    [
        {},
        {"used": None, "limit": None, "percent": 50, "severity": "critical"},
        {"used": {"amount_minor": "lots"}, "limit": "none"},
        {"used": [], "limit": 5, "enabled": "yes"},
        [],
        "spend",
        5,
    ],
)
def test_a_spend_block_with_nothing_usable_gives_no_extra_usage(spend):
    assert usage.parse_usage({"spend": spend}, "a", NOW).extra_usage is None


def test_spend_never_becomes_a_window_or_promo():
    raw = example_usage()
    raw["spend"]["utilization"] = 100.0  # even if the server one day adds window-like keys
    raw["spend"]["resets_at"] = "2026-12-01T00:00:00Z"
    raw["spend"]["limit_dollars"] = 10
    snap = example_snapshot(raw)
    assert [w.key for w in snap.windows] == ["five_hour", "seven_day"] and snap.promos == ()


# --------------------------------------------------------------------------- weekly breakdown


def test_example_breakdown_rows_and_as_of():
    snap = example_snapshot()

    assert snap.breakdown == (
        BreakdownRow("claude_code", "Claude Code", 100.0, 31.0),
        BreakdownRow("chat", "Chats", 0.0, 0.0),
        BreakdownRow("cowork", "Cowork", 0.0, 0.0),
        BreakdownRow("other", "Other", 0.0, 0.0),
    )
    assert snap.breakdown_as_of == datetime(2025, 2, 1, 4, 2, 10, 345678, tzinfo=timezone.utc)
    # consistent with the weekly window it explains: Claude Code is all of the usage, 31% of the limit
    assert snap.breakdown[0].percent_of_limit == snap.weekly.utilization


def test_breakdown_is_empty_when_the_block_is_absent_or_unusable():
    for raw in ({}, {"seven_day_breakdown": None}, {"seven_day_breakdown": []}, {"seven_day_breakdown": "x"},
                {"seven_day_breakdown": {"as_of": "2026-11-23T04:12:09Z"}}, {"seven_day_breakdown": {"rows": "none"}},
                {"seven_day_breakdown": {"as_of": "2026-11-23T04:12:09Z", "rows": []}}):
        snap = usage.parse_usage(raw, "a", NOW)
        assert (snap.breakdown, snap.breakdown_as_of) == ((), None), raw


def test_malformed_breakdown_rows_are_skipped_and_the_good_ones_kept():
    raw = example_usage()
    raw["seven_day_breakdown"]["rows"] = [
        None, 5, "x", [], {},
        {"key": "", "percent": 5},  # no key
        {"key": 7, "percent": 5},  # key is not text
        {"display_name": "Nameless", "percent": 5},  # no key at all
        {"key": "ghost", "display_name": "Ghost", "percent": None, "percent_of_limit": "n/a"},  # nothing to show
        {"key": "claude_code", "display_name": "Claude Code", "percent": 100, "percent_of_limit": 26},
    ]
    snap = example_snapshot(raw)
    assert [r.key for r in snap.breakdown] == ["claude_code"]
    assert snap.breakdown_as_of is not None


def test_breakdown_row_defaults_and_partial_numbers():
    raw = {"seven_day_breakdown": {"rows": [
        {"key": "claude_code", "percent": 60},  # no display_name, no percent_of_limit
        {"key": "chat", "display_name": "  ", "percent_of_limit": "12.5"},  # blank name, numeric string
        {"key": "x", "display_name": "X", "percent": True, "percent_of_limit": 3},  # a bool is not a number
    ]}}
    rows = usage.parse_usage(raw, "a", NOW).breakdown
    assert rows == (
        BreakdownRow("claude_code", "Claude Code", 60.0, None),
        BreakdownRow("chat", "Chat", None, 12.5),
        BreakdownRow("x", "X", None, 3.0),
    )


def test_breakdown_with_an_unparseable_as_of_keeps_the_rows():
    raw = example_usage()
    raw["seven_day_breakdown"]["as_of"] = "yesterday-ish"
    snap = example_snapshot(raw)
    assert len(snap.breakdown) == 4 and snap.breakdown_as_of is None


def test_parse_breakdown_is_usable_on_its_own_and_does_not_mutate():
    block = example_usage()["seven_day_breakdown"]
    before = copy.deepcopy(block)
    rows, as_of = usage.parse_breakdown(block)
    assert len(rows) == 4 and as_of is not None and block == before
    assert usage.parse_breakdown(None) == ((), None)


def test_the_breakdown_block_is_never_mistaken_for_a_window_or_promo():
    raw = example_usage()
    raw["seven_day_breakdown"].update({"utilization": 26.0, "resets_at": "2026-11-27T22:59:59Z", "limit_dollars": 10})
    snap = example_snapshot(raw)
    assert [w.key for w in snap.windows] == ["five_hour", "seven_day"] and snap.promos == ()
    assert len(snap.breakdown) == 4


# --------------------------------------------------------------------------- nothing in the example response can make parse_usage raise


@pytest.mark.parametrize("key", ["extra_usage", "spend", "seven_day_breakdown", "limits", "five_hour", "seven_day"])
@pytest.mark.parametrize("junk", [None, [], {}, "x", 7, True, [None], {"rows": {"a": 1}}, {"used": {"exponent": "x"}}])
def test_replacing_any_example_block_with_junk_never_raises(key, junk):
    raw = example_usage()
    raw[key] = junk
    snap = example_snapshot(raw)
    assert snap.ok and isinstance(snap, UsageSnapshot)


def test_example_response_is_not_mutated_and_is_kept_verbatim_in_raw():
    raw = example_usage()
    before = copy.deepcopy(raw)
    snap = example_snapshot(raw)
    assert raw == before
    assert snap.raw == before  # nothing in it looks like a token, so scrubbing changed nothing
    assert snap.raw["spend"]["disclaimer"].startswith("Usage credits cover you")


# --------------------------------------------------------------------------- format_money with the server's decimal_places


@pytest.mark.parametrize(
    "minor, currency, places, expected",
    [
        (6125, "USD", 2, "$61.25"),
        (6125, "USD", None, "$61.25"),  # old callers: exponent derived from the currency
        (1500, "USD", 0, "$1,500"),
        (12345, "KWD", 3, "12.345 KWD"),
        (1500, "JPY", None, "1,500 JPY"),
        (1500, "JPY", 2, "15.00 JPY"),  # the server's exponent wins over our table
        (6125, "USD", -1, "$61.25"),  # unusable exponents fall back to the currency
        (6125, "USD", 99, "$61.25"),
        (6125, "USD", True, "$61.25"),
        (6125, "USD", "2", "$61.25"),
        (6125, "USD", 2.0, "$61.25"),
        (None, "USD", 2, "—"),
    ],
)
def test_format_money_decimal_places(minor, currency, places, expected):
    assert usage.format_money(minor, currency, places) == expected


# --------------------------------------------------------------------------- the example resets block (ineligible account)


def example_resets_block() -> dict:
    """tests/fixtures/resets_example_ineligible.json: {"cedar_ember": <stored, already-parsed status>}."""
    return json.loads((FIXTURES / "resets_example_ineligible.json").read_text())["cedar_ember"]


def test_example_ineligible_resets_parse_to_an_empty_ineligible_status():
    status = usage.parse_resets(example_resets_block())

    assert status == ResetStatus(eligible=False, ineligible_reason="surface", at_limit=False, grants=(),
                                 weekly_resets_at=None, cooldown_until=None)
    assert status.grants == () and status.resets_left == 0 and status.next_expiry is None


def test_example_ineligible_resets_survive_the_cache_round_trip_unchanged():
    from cc_switcher import monitor

    status = usage.parse_resets(example_resets_block())
    assert usage.parse_resets(json.loads(json.dumps(monitor.resets_to_dict(status)))) == status
    assert monitor.resets_to_dict(status) == example_resets_block()  # the fixture is exactly our stored form


# =========================================================================== window labels


def cowork_block(utilization=12.0) -> dict:
    """Shaped like a seven_day window: window fields plus the null *_dollars / locked_reason keys."""
    return {
        "utilization": utilization,
        "resets_at": "2025-02-05T23:00:00.234567+00:00",
        "limit_dollars": None,
        "used_dollars": None,
        "remaining_dollars": None,
        "locked_reason": None,
    }


def test_seven_day_cowork_is_a_known_window_with_its_own_label():
    raw = example_usage()
    raw["seven_day_cowork"] = cowork_block()

    snap = example_snapshot(raw)

    assert [(w.key, w.label, w.utilization) for w in snap.windows] == [
        ("five_hour", "Session (5h)", 7.0),
        ("seven_day", "Weekly", 31.0),
        ("seven_day_cowork", "Weekly · Cowork", 12.0),
    ]
    assert snap.promos == ()  # its null dollar fields do not make it a promo credit


def test_cowork_sits_among_the_known_windows_before_any_codename():
    raw = {"cinder_cove": cowork_block(1.0), "seven_day_cowork": cowork_block(2.0), "seven_day_sonnet": cowork_block(3.0), "five_hour": cowork_block(4.0)}
    assert [w.key for w in usage.parse_usage(raw, "a", NOW).windows] == ["five_hour", "seven_day_sonnet", "seven_day_cowork", "cinder_cove"]


@pytest.mark.parametrize(
    "key, expected",
    [
        ("cinder_cove", "Cinder Cove"),
        ("iguana_necktie", "Iguana Necktie"),
        ("tangelo", "Tangelo"),
        ("seven_day_omelette", "Weekly · Omelette"),  # follows the existing "Weekly · X" pattern
        ("seven_day_new_model_x", "Weekly · New Model X"),
        ("five_hour_burst", "Session (5h) · Burst"),
        ("opus_5_research", "Opus 5 Research"),
        ("pool_5h", "Pool 5h"),  # not "5H"
        ("__odd__key__", "Odd Key"),
        ("seven_day_", "Seven Day"),  # a bare prefix is not a pattern
    ],
)
def test_unknown_window_keys_get_a_readable_label(key, expected):
    assert usage.window_label(key) == expected
    assert "_" not in usage.window_label(key)


def test_known_and_composite_keys_keep_their_labels():
    assert usage.window_label("five_hour") == "Session (5h)"
    assert usage.window_label("seven_day_cowork") == "Weekly · Cowork"
    assert usage.window_label("seven_day_oauth_apps") == "Weekly · Apps"
    assert usage.window_label("limit:weekly:Fable") == "limit:weekly:Fable"  # built from display names already


def test_an_unknown_window_in_a_response_is_labelled_readably_and_its_own_label_still_wins():
    raw = {
        "amber_ladder": {"utilization": 9.0, "resets_at": "2026-11-27T08:00:00Z"},
        "seven_day_omelette": {"utilization": 8.0, "resets_at": "2026-11-27T08:00:00Z", "limit_dollars": None},
        "juniper_tide": {"label": "Opus 5 research", "utilization": 7.0, "resets_at": "2026-11-27T08:00:00Z"},
    }
    labels = {w.key: w.label for w in usage.parse_usage(raw, "a", NOW).windows}
    assert labels == {"amber_ladder": "Amber Ladder", "seven_day_omelette": "Weekly · Omelette", "juniper_tide": "Opus 5 research"}


def test_no_window_label_from_the_real_response_or_the_realistic_payload_contains_an_underscore():
    for raw in (example_usage(), realistic_payload()):
        snap = usage.parse_usage(raw, "a", NOW)
        assert all("_" not in w.label for w in snap.windows), [w.label for w in snap.windows]
        assert all("_" not in p.label for p in snap.promos)


def test_humanize_key():
    assert usage.humanize_key("claude_code") == "Claude Code"
    assert usage.humanize_key("  chat ") == "Chat"
    assert usage.humanize_key("___") == "___"  # nothing to humanise: left alone rather than emptied
