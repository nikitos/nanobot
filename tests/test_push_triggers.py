"""Tests for the fire-and-forget Web Push triggers."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nanobot.push import triggers


class _FakeStore:
    def __init__(self, size: int = 1) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size


class _FakeService:
    def __init__(self, size: int = 1) -> None:
        self.store = _FakeStore(size)
        self.calls: list[dict[str, Any]] = []

    async def notify_all_async(
        self,
        title: str,
        body: str,
        *,
        url: str = "/",
        tag: str | None = None,
    ) -> dict[str, int]:
        self.calls.append({"title": title, "body": body, "url": url, "tag": tag})
        return {"sent": self._size, "failed": 0, "pruned": 0}


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> _FakeService:
    service = _FakeService()
    monkeypatch.setattr(triggers, "get_push_service", lambda: service, raising=False)
    # triggers imports get_push_service inside _notify; patch the source module.
    import nanobot.push.service as service_module

    monkeypatch.setattr(service_module, "get_push_service", lambda: service)
    return service


def _drain() -> None:
    """Let pending background tasks run to completion."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()


def test_webui_channel_sends(fake_service: _FakeService) -> None:
    triggers.push_webui_notification(
        "websocket", "Hello", "World", url="/chat", tag="t1"
    )
    _drain()
    assert fake_service.calls == [
        {"title": "Hello", "body": "World", "url": "/chat", "tag": "t1"}
    ]


def test_non_webui_channel_is_noop(fake_service: _FakeService) -> None:
    triggers.push_webui_notification("telegram", "Hello", "World")
    triggers.push_webui_notification(None, "Hello", "World")
    _drain()
    assert fake_service.calls == []


def test_empty_store_skips_send(fake_service: _FakeService) -> None:
    fake_service.store = _FakeStore(0)
    triggers.push_webui_notification("websocket", "Hello", "World")
    _drain()
    assert fake_service.calls == []


def test_cron_completed_ok(fake_service: _FakeService) -> None:
    triggers.push_cron_completed("nightly", "ok", channel="websocket")
    _drain()
    assert fake_service.calls == [
        {
            "title": "Cron: nightly",
            "body": "Completed successfully.",
            "url": "/",
            "tag": "cron-nightly",
        }
    ]


def test_cron_completed_error_includes_message(fake_service: _FakeService) -> None:
    triggers.push_cron_completed(
        "nightly", "error", channel="websocket", error="boom"
    )
    _drain()
    call = fake_service.calls[0]
    assert call["title"] == "Cron: nightly"
    assert "error" in call["body"] and "boom" in call["body"]
    assert call["tag"] == "cron-nightly"


def test_subagent_completed(fake_service: _FakeService) -> None:
    triggers.push_subagent_completed("build", "ok", channel="websocket")
    _drain()
    assert fake_service.calls[0]["title"] == "Subagent: build"
    assert fake_service.calls[0]["tag"] == "subagent-build"


def test_trigger_never_raises_on_service_failure() -> None:
    import nanobot.push.service as service_module

    def _boom() -> _FakeService:
        raise RuntimeError("no service")

    service_module.get_push_service = _boom  # type: ignore[assignment]
    try:
        # Must not raise, even without a running event loop.
        triggers.push_webui_notification("websocket", "t", "b")
    finally:
        # Restore the real singleton accessor.
        from nanobot.push import service as real

        service_module.get_push_service = real.get_push_service
