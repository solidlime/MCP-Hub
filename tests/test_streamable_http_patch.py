"""Unit tests for FIX-2 hardening in streamable_http_patch.

RED tests (must fail before fix, pass after):
- absolute Location is rejected without polling
- poll headers strip Authorization / Cookie / X-API-Key
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import mcp_hub.streamable_http_patch as patch_mod


class _FakeStreamResp:
    def __init__(self, status=202, headers=None):
        self.status_code = status
        self.headers = headers or {}


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakePollResp:
    def __init__(self, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {"content-type": "application/json"}


class _FakeClient:
    def __init__(self, stream_resp, poll_resps=None):
        self._stream_resp = stream_resp
        self._poll_resps = list(poll_resps or [])
        self.post_calls: list[tuple[str, dict]] = []

    def stream(self, method, url, json=None, headers=None):
        return _FakeStreamCtx(self._stream_resp)

    async def post(self, url, headers=None):
        self.post_calls.append((url, dict(headers or {})))
        if self._poll_resps:
            return self._poll_resps.pop(0)
        return _FakePollResp(status=202)


def _make_self(url="http://localhost:8000/mcp"):
    async def _never_called(*a, **k):  # pragma: no cover
        raise AssertionError("_process_response should not be called")

    return SimpleNamespace(
        url=url,
        _prepare_headers=lambda: {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "authorization": "Bearer secret",
            "cookie": "session=abc",
            "X-API-Key": "key123",
        },
        _is_initialization_request=lambda m: False,
        _maybe_extract_session_id_from_response=lambda r: None,
    )


def _make_ctx(client):
    msg = SimpleNamespace(
        model_dump=lambda **k: {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    return SimpleNamespace(
        client=client,
        session_message=SimpleNamespace(message=msg),
        metadata=None,
        read_stream_writer=SimpleNamespace(send=None),
    )


@pytest.mark.asyncio
async def test_absolute_location_rejected_without_poll(monkeypatch):
    """Absolute-URL Location must not be polled (SSRF guard)."""
    monkeypatch.setattr(patch_mod, "_poll_delay", lambda attempt: 0)
    patch_mod._poll_in_flight.clear()
    stream_resp = _FakeStreamResp(202, {"Location": "http://evil.example/poll/1"})
    client = _FakeClient(stream_resp)
    await patch_mod._patched_handle_post_request(
        _make_self(), _make_ctx(client)  # type: ignore[arg-type]
    )
    assert client.post_calls == [], f"must not poll absolute URL, got {client.post_calls}"


@pytest.mark.asyncio
async def test_poll_headers_strip_auth(monkeypatch):
    """Poll POST must not forward Authorization / Cookie / X-API-Key."""
    monkeypatch.setattr(patch_mod, "_poll_delay", lambda attempt: 0)
    patch_mod._poll_in_flight.clear()
    seen: dict = {}

    async def _fake_process(self, response, ctx, message, is_init):
        seen["headers"] = dict(ctx.client.post_calls[-1][1])
        seen["url"] = ctx.client.post_calls[-1][0]

    monkeypatch.setattr(patch_mod, "_process_response", _fake_process)
    stream_resp = _FakeStreamResp(202, {"Location": "/poll/123"})
    client = _FakeClient(stream_resp, [_FakePollResp(status=200)])
    await patch_mod._patched_handle_post_request(
        _make_self(), _make_ctx(client)  # type: ignore[arg-type]
    )
    assert client.post_calls, "expected one poll POST"
    url, headers = client.post_calls[0]
    assert url == "http://localhost:8000/poll/123", url
    lowered = {k.lower(): v for k, v in headers.items()}
    assert "authorization" not in lowered, headers
    assert "cookie" not in lowered, headers
    assert "x-api-key" not in lowered, headers
    assert seen.get("url") == url
