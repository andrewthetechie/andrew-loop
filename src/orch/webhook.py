"""Webhook notification system for human escalation.

Fires a POST to a configured URL when a ticket transitions to a trigger state.
Retries up to 3 times on non-2xx responses. First 2xx = success.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Awaitable, Callable

from orch.config import Config
from orch.db import Event, Ticket

logger = logging.getLogger(__name__)

PostFn = Callable[[str, dict, float], Awaitable[int]]

MAX_RETRIES = 3
BACKOFF_SECONDS = (1.0, 2.0, 4.0)


async def _default_post(url: str, payload: dict, *, timeout: float = 10.0) -> int:  # noqa: ASYNC109
    """POST JSON payload to URL, return HTTP status code."""
    import asyncio

    req_timeout = timeout

    def _sync_post() -> int:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            logger.exception("Webhook request failed")
            return 0

    return await asyncio.to_thread(_sync_post)


def _build_payload(event: Event, ticket: Ticket) -> dict:
    """Build the webhook POST payload."""
    return {
        "ticket": {
            "id": ticket.id,
            "title": ticket.title,
            "state": ticket.state,
            "risk_score": ticket.risk_score,
            "linked_pr": ticket.linked_pr,
            "assignee": ticket.assignee,
        },
        "old_state": event.old_state,
        "new_state": event.new_state,
        "actor": event.actor,
        "timestamp": event.timestamp,
    }


async def fire_webhook(
    config: Config,
    event: Event,
    ticket: Ticket,
    *,
    post_fn: PostFn | None = None,
    backoff: tuple[float, ...] | None = None,
) -> bool:
    """Fire webhook if configured and event matches a trigger state.

    Returns True if webhook fired successfully (or was skipped), False if all retries failed.
    """
    import asyncio

    url = config.webhook.url
    if not url:
        return True

    if event.new_state not in config.webhook.triggers:
        return True

    post = post_fn or _default_post
    delays = backoff if backoff is not None else BACKOFF_SECONDS
    payload = _build_payload(event, ticket)

    status = 0
    for attempt in range(MAX_RETRIES):
        status = await post(url, payload, timeout=10.0)
        if 200 <= status < 300:
            return True
        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(delays[attempt])

    logger.error(
        "Webhook failed after %d retries for %s (last status: %d)",
        MAX_RETRIES,
        event.ticket_id,
        status,
    )
    return False
