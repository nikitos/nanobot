"""Web Push service for the nanobot PWA.

Sends browser push notifications (via the ``webpush`` library) for
significant events: cron job completion, subagent results, and
agent-turn completion while the tab is in the background.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from webpush import WebPush, WebPushException, WebPushSubscription  # type: ignore[import-untyped]

from nanobot.push.store import SubscriptionStore
from nanobot.push.vapid import VapidKeys, load_or_create_vapid_keys

log = logging.getLogger(__name__)

# HTTP statuses from the push service that mean the subscription is dead.
_DEAD_STATUSES = {404, 410}
# 429 = rate limited: skip, but keep the subscription.
_SKIP_STATUSES = {429}

# VAPID requires a "sub" claim; use a stable mailto URI.
_VAPID_SUBSCRIBER = "mailto:nanobot@localhost"
_REQUEST_TIMEOUT = 10.0


class PushService:
    """Fan-out Web Push notifications to all stored subscriptions."""

    def __init__(
        self,
        *,
        vapid_keys: VapidKeys | None = None,
        store: SubscriptionStore | None = None,
    ) -> None:
        self._vapid = vapid_keys or load_or_create_vapid_keys()
        # NB: SubscriptionStore defines __len__, so an empty store is falsy —
        # use an explicit None check instead of `or`.
        self._store = store if store is not None else SubscriptionStore()

    @property
    def public_key(self) -> str:
        """url-safe base64 VAPID public key for ``pushManager.subscribe``."""
        return self._vapid.public_key

    @property
    def store(self) -> SubscriptionStore:
        return self._store

    # -- sending -----------------------------------------------------------

    def _send_one(self, subscription: dict[str, Any], payload: str) -> int:
        """Encrypt and deliver one push message.

        Returns the HTTP status code from the push service.
        Raises ``WebPushException`` on encryption failure and
        ``httpx.HTTPError`` on transport failure.
        """
        builder = WebPush(
            private_key=self._vapid.private_key_pem.encode("ascii"),
            public_key=self._vapid.public_key_pem.encode("ascii"),
            subscriber=_VAPID_SUBSCRIBER,
        )
        message = builder.get(
            payload,
            WebPushSubscription(
                endpoint=subscription["endpoint"],
                keys=subscription["keys"],
            ),
        )
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            response = client.post(
                subscription["endpoint"],
                content=message.encrypted,
                headers={
                    "Authorization": message.headers["authorization"],
                    "Content-Encoding": message.headers["content-encoding"],
                    "TTL": message.headers["ttl"],
                },
            )
        return response.status_code

    def notify_all(
        self,
        title: str,
        body: str,
        *,
        url: str = "/",
        tag: str | None = None,
    ) -> dict[str, int]:
        """Send a notification to every subscription (blocking).

        Returns counters: ``sent``, ``failed``, ``pruned``.
        """
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "url": url,
                "tag": tag,
            },
            ensure_ascii=False,
        )
        sent = 0
        failed = 0
        dead_endpoints: list[str] = []
        for sub in self._store.list():
            try:
                status = self._send_one(sub.to_webpush_dict(), payload)
            except WebPushException as exc:
                failed += 1
                log.warning("push encrypt failed: %s", exc)
                continue
            except httpx.HTTPError:
                failed += 1
                log.exception("push transport error for %s", sub.endpoint[:48])
                continue
            if status in _DEAD_STATUSES:
                dead_endpoints.append(sub.endpoint)
            elif status in _SKIP_STATUSES:
                log.warning("push rate-limited for %s", sub.endpoint[:48])
            elif 200 <= status < 300:
                sent += 1
            else:
                failed += 1
                log.warning("push failed (status=%s)", status)
        pruned = self._store.prune_invalid(dead_endpoints)
        if pruned:
            log.info("pruned %d dead push subscription(s)", pruned)
        return {"sent": sent, "failed": failed, "pruned": pruned}

    async def notify_all_async(
        self,
        title: str,
        body: str,
        *,
        url: str = "/",
        tag: str | None = None,
    ) -> dict[str, int]:
        """Async wrapper: runs the blocking send in a worker thread."""
        return await asyncio.to_thread(
            self.notify_all, title, body, url=url, tag=tag
        )

    def notify_test(self) -> dict[str, int]:
        """Send a test notification to all subscriptions."""
        return self.notify_all(
            "nanobot",
            "Web Push is working — you can close this tab and still get notified.",
            tag="nanobot-test",
        )

    async def notify_test_async(self) -> dict[str, int]:
        return await asyncio.to_thread(self.notify_test)


# -- singleton -------------------------------------------------------------

_service: PushService | None = None


def get_push_service() -> PushService:
    """Return the process-wide PushService, creating it on first use."""
    global _service
    if _service is None:
        _service = PushService()
    return _service


def register_push_service(service: PushService | None) -> None:
    """Override the singleton (used by tests and the gateway)."""
    global _service
    _service = service


def notify_all(
    title: str,
    body: str,
    *,
    url: str = "/",
    tag: str | None = None,
) -> dict[str, int]:
    """Convenience: send a notification to all subscriptions (blocking)."""
    return get_push_service().notify_all(title, body, url=url, tag=tag)
