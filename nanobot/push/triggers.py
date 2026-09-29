"""Safe fire-and-forget Web Push triggers.

These helpers are called from hot paths (cron completion, subagent
completion). They must never raise, never block the caller, and only
send when the originating channel is the WebUI (websocket).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)

_WEBUI_CHANNEL = "websocket"


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    """Run a coroutine in the background; log and swallow all errors."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop (e.g. CLI context) — run synchronously.
        try:
            asyncio.run(coro)
        except Exception:
            log.exception("push trigger (sync) failed")
        return
    task = loop.create_task(coro)
    task.add_done_callback(_log_task_error)


def _log_task_error(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("push trigger failed: %s", exc)


async def _notify(
    title: str,
    body: str,
    *,
    url: str = "/",
    tag: str | None = None,
) -> None:
    from nanobot.push.service import get_push_service

    service = get_push_service()
    if len(service.store) == 0:
        return
    result = await service.notify_all_async(title, body, url=url, tag=tag)
    log.info("push sent: %s", result)


def push_webui_notification(
    channel: str | None,
    title: str,
    body: str,
    *,
    url: str = "/",
    tag: str | None = None,
) -> None:
    """Send a Web Push notification if the channel is the WebUI.

    Never raises. Safe to call from sync or async code.
    """
    if channel != _WEBUI_CHANNEL:
        return
    _spawn(_notify(title, body, url=url, tag=tag))


def push_cron_completed(
    job_name: str,
    status: str,
    *,
    channel: str | None = None,
    error: str | None = None,
) -> None:
    """Notify the WebUI that a cron job finished."""
    if status == "ok":
        title = f"Cron: {job_name}"
        body = "Completed successfully."
    else:
        title = f"Cron: {job_name}"
        body = f"Finished with status '{status}'." + (f" {error}" if error else "")
    push_webui_notification(
        channel, title, body, url="/", tag=f"cron-{job_name}"
    )


def push_subagent_completed(
    label: str,
    status: str,
    *,
    channel: str | None = None,
) -> None:
    """Notify the WebUI that a subagent task finished."""
    title = f"Subagent: {label}"
    body = "Completed successfully." if status == "ok" else "Finished with errors."
    push_webui_notification(
        channel, title, body, url="/", tag=f"subagent-{label}"
    )
