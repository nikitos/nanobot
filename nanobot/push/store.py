"""Persistent store for Web Push subscriptions.

Subscriptions are kept as a JSON list under the instance data directory
(``~/.nanobot/push/subscriptions.json``). The gateway is a single process,
so a ``threading.Lock`` is sufficient to serialize access from worker
threads and the event loop.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nanobot.config.paths import get_runtime_subdir

_SUBSCRIPTIONS_FILE_NAME = "subscriptions.json"


@dataclass
class PushSubscription:
    """A single Web Push subscription as reported by the browser."""

    endpoint: str
    p256dh: str
    auth: str
    created_at: float

    def to_webpush_dict(self) -> dict[str, Any]:
        """Shape expected by ``webpush.webpush``."""
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }


def _default_subscriptions_path() -> Path:
    return get_runtime_subdir("push") / _SUBSCRIPTIONS_FILE_NAME


class SubscriptionStore:
    """Thread-safe JSON-backed collection of push subscriptions."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_subscriptions_path()
        self._lock = threading.Lock()
        self._subscriptions: list[PushSubscription] = []
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, list):
            return
        entries: list[Any] = raw
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                self._subscriptions.append(
                    PushSubscription(
                        endpoint=str(entry["endpoint"]),
                        p256dh=str(entry["p256dh"]),
                        auth=str(entry["auth"]),
                        created_at=float(entry.get("created_at", 0.0)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue

    def _save_locked(self) -> None:
        payload = json.dumps(
            [asdict(sub) for sub in self._subscriptions],
            indent=2,
        ).encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".subscriptions-"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- operations --------------------------------------------------------

    def add(self, endpoint: str, p256dh: str, auth: str) -> bool:
        """Add a subscription. Returns True if it was newly added."""
        if not endpoint or not p256dh or not auth:
            raise ValueError("endpoint, p256dh and auth are required")
        with self._lock:
            if any(sub.endpoint == endpoint for sub in self._subscriptions):
                return False
            self._subscriptions.append(
                PushSubscription(
                    endpoint=endpoint,
                    p256dh=p256dh,
                    auth=auth,
                    created_at=time.time(),
                )
            )
            self._save_locked()
            return True

    def remove(self, endpoint: str) -> bool:
        """Remove a subscription by endpoint. Returns True if it existed."""
        with self._lock:
            before = len(self._subscriptions)
            self._subscriptions = [
                sub for sub in self._subscriptions if sub.endpoint != endpoint
            ]
            if len(self._subscriptions) == before:
                return False
            self._save_locked()
            return True

    def list(self) -> list[PushSubscription]:
        with self._lock:
            return list(self._subscriptions)

    def prune_invalid(self, endpoints: list[str]) -> int:
        """Drop subscriptions whose endpoints the push service rejected."""
        if not endpoints:
            return 0
        doomed = set(endpoints)
        with self._lock:
            before = len(self._subscriptions)
            self._subscriptions = [
                sub for sub in self._subscriptions if sub.endpoint not in doomed
            ]
            removed = before - len(self._subscriptions)
            if removed:
                self._save_locked()
            return removed

    def __len__(self) -> int:
        with self._lock:
            return len(self._subscriptions)
