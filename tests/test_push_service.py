"""Tests for the Web Push service (VAPID keys, subscription store, fan-out)."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
import httpx

from nanobot.push.service import PushService, get_push_service, register_push_service
from nanobot.push.store import SubscriptionStore
from nanobot.push.vapid import VapidKeys, load_or_create_vapid_keys


# -- VAPID ------------------------------------------------------------------


def test_vapid_keys_generated_and_persisted(tmp_path: Path) -> None:
    path = tmp_path / "vapid.json"
    keys = load_or_create_vapid_keys(path)
    assert path.exists()
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    # P-256 uncompressed point: 65 bytes -> 88 urlsafe base64 chars.
    raw = base64.urlsafe_b64decode(keys.public_key + "=" * (-len(keys.public_key) % 4))
    assert len(raw) == 65 and raw[0] == 0x04
    assert "PRIVATE KEY" in keys.private_key_pem
    # Second load returns the same keys.
    again = load_or_create_vapid_keys(path)
    assert again.public_key == keys.public_key


def test_vapid_corrupt_file_regenerates(tmp_path: Path) -> None:
    path = tmp_path / "vapid.json"
    path.write_text("not json", encoding="utf-8")
    keys = load_or_create_vapid_keys(path)
    assert keys.public_key


# -- Subscription store ------------------------------------------------------


def _store(tmp_path: Path) -> SubscriptionStore:
    return SubscriptionStore(tmp_path / "subscriptions.json")


def test_store_add_dedup_and_remove(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.add("https://push.example/1", "p256dh", "auth") is True
    assert store.add("https://push.example/1", "p256dh", "auth") is False
    assert len(store) == 1
    assert store.remove("https://push.example/1") is True
    assert store.remove("https://push.example/1") is False
    assert len(store) == 0


def test_store_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "subscriptions.json"
    _store(tmp_path).add("https://push.example/2", "p256dh", "auth")
    reloaded = SubscriptionStore(path)
    subs = reloaded.list()
    assert len(subs) == 1
    assert subs[0].endpoint == "https://push.example/2"
    assert subs[0].to_webpush_dict() == {
        "endpoint": "https://push.example/2",
        "keys": {"p256dh": "p256dh", "auth": "auth"},
    }


def test_store_prune_invalid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add("https://push.example/a", "p256dh", "auth")
    store.add("https://push.example/b", "p256dh", "auth")
    assert store.prune_invalid(["https://push.example/a"]) == 1
    assert [s.endpoint for s in store.list()] == ["https://push.example/b"]
    assert store.prune_invalid([]) == 0


def test_store_rejects_empty_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.add("", "p256dh", "auth")


# -- PushService -------------------------------------------------------------


def _service(tmp_path: Path) -> PushService:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    keys = VapidKeys(public_key="pub", private_key_pem=private_pem, public_key_pem=public_pem)
    return PushService(vapid_keys=keys, store=_store(tmp_path))


def _valid_p256dh() -> str:
    """A valid base64url-encoded P-256 public point (what browsers send)."""
    import base64

    from cryptography.hazmat.primitives.asymmetric import ec

    pub = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    raw = b"\x04" + pub.x.to_bytes(32, "big") + pub.y.to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _valid_auth() -> str:
    """A valid 16-byte base64url-encoded auth secret."""
    import base64

    return base64.urlsafe_b64encode(b"\x00" * 16).rstrip(b"=").decode("ascii")


def test_notify_all_sends_to_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    service.store.add("https://push.example/1", _valid_p256dh(), _valid_auth())
    service.store.add("https://push.example/2", _valid_p256dh(), _valid_auth())
    calls: list[dict] = []

    class FakeResponse:
        status_code = 201

    def fake_post(self, url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr("nanobot.push.service.httpx.Client.post", fake_post)
    result = service.notify_all("t", "b", url="/chat/1", tag="turn")
    assert result == {"sent": 2, "failed": 0, "pruned": 0}
    assert len(calls) == 2
    assert calls[0]["url"] == "https://push.example/1"
    assert calls[0]["headers"]["Content-Encoding"] == "aes128gcm"
    assert calls[0]["headers"]["Authorization"].startswith("vapid ")
    # payload is encrypted (not plain JSON) — just check it's bytes
    assert isinstance(calls[0]["content"], (bytes, bytearray))


def test_notify_all_prunes_dead_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.store.add("https://push.example/dead", _valid_p256dh(), _valid_auth())
    service.store.add("https://push.example/live", _valid_p256dh(), _valid_auth())

    def fake_post(self, url, **kwargs):
        class R:
            status_code = 410 if url.endswith("/dead") else 201
        return R()

    monkeypatch.setattr("nanobot.push.service.httpx.Client.post", fake_post)
    result = service.notify_all("t", "b")
    assert result["sent"] == 1
    assert result["pruned"] == 1
    assert [s.endpoint for s in service.store.list()] == ["https://push.example/live"]


def test_notify_all_keeps_rate_limited_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.store.add("https://push.example/slow", _valid_p256dh(), _valid_auth())

    def fake_post(self, url, **kwargs):
        class R:
            status_code = 429
        return R()

    monkeypatch.setattr("nanobot.push.service.httpx.Client.post", fake_post)
    result = service.notify_all("t", "b")
    assert result["pruned"] == 0
    assert result["sent"] == 0
    assert len(service.store) == 1


def test_notify_all_counts_transport_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.store.add("https://push.example/err", _valid_p256dh(), _valid_auth())

    def fake_post(self, url, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr("nanobot.push.service.httpx.Client.post", fake_post)
    result = service.notify_all("t", "b")
    assert result == {"sent": 0, "failed": 1, "pruned": 0}
    assert len(service.store) == 1


def test_singleton_register_and_get() -> None:
    original = get_push_service()
    try:
        replacement = PushService(
            vapid_keys=VapidKeys(public_key="x", private_key_pem="y", public_key_pem="z"),
            store=SubscriptionStore(),
        )
        register_push_service(replacement)
        assert get_push_service() is replacement
    finally:
        register_push_service(original)
