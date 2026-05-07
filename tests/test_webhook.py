"""Tests for webhook notification system."""

from pathlib import Path

from orch.config import Config, WebhookConfig
from orch.db import Database, Event
from orch.tickets import create_ticket, get_ticket
from orch.webhook import fire_webhook


async def test_fires_post_with_correct_payload(tmp_db_path: Path) -> None:
    """Webhook POSTs correct payload when new_state matches a trigger."""
    posts: list[tuple[str, dict]] = []

    async def fake_post(url: str, payload: dict, *, timeout: float = 10.0) -> int:
        posts.append((url, payload))
        return 200

    config = Config(
        webhook=WebhookConfig(url="https://hooks.example.com", triggers=["Needs Human Review"])
    )

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {
                "title": "Bug fix",
                "description": "Fix the bug",
                "acceptance_criteria": "No crash",
                "risk_score": 3,
            },
        )

        event = Event(
            ticket_id=ticket.id,
            timestamp="2026-01-01T00:00:00",
            actor="router",
            old_state="In Progress",
            new_state="Needs Human Review",
        )

        t = await get_ticket(db, ticket.id)
        await fire_webhook(config, event, t, post_fn=fake_post)

    assert len(posts) == 1
    url, payload = posts[0]
    assert url == "https://hooks.example.com"
    assert payload["ticket"]["id"] == ticket.id
    assert payload["ticket"]["title"] == "Bug fix"
    assert payload["ticket"]["risk_score"] == 3
    assert payload["old_state"] == "In Progress"
    assert payload["new_state"] == "Needs Human Review"
    assert payload["actor"] == "router"
    assert payload["timestamp"] == "2026-01-01T00:00:00"


async def test_skips_when_no_url_configured(tmp_db_path: Path) -> None:
    """No POST is made when webhook URL is empty."""
    posts: list = []

    async def fake_post(url: str, payload: dict, *, timeout: float = 10.0) -> int:
        posts.append(url)
        return 200

    config = Config()  # default: url=""

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db, {"title": "T", "description": "D", "acceptance_criteria": "AC"}
        )
        event = Event(
            ticket_id=ticket.id,
            timestamp="now",
            actor="router",
            old_state="In Progress",
            new_state="Needs Human Review",
        )
        t = await get_ticket(db, ticket.id)
        result = await fire_webhook(config, event, t, post_fn=fake_post)

    assert result is True
    assert posts == []


async def test_skips_when_state_not_in_triggers(tmp_db_path: Path) -> None:
    """No POST is made when new_state is not in trigger list."""
    posts: list = []

    async def fake_post(url: str, payload: dict, *, timeout: float = 10.0) -> int:
        posts.append(url)
        return 200

    config = Config(
        webhook=WebhookConfig(url="https://hooks.example.com", triggers=["Needs Human Review"])
    )

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db, {"title": "T", "description": "D", "acceptance_criteria": "AC"}
        )
        event = Event(
            ticket_id=ticket.id,
            timestamp="now",
            actor="coder",
            old_state="In Progress",
            new_state="Code Review",  # not a trigger
        )
        t = await get_ticket(db, ticket.id)
        result = await fire_webhook(config, event, t, post_fn=fake_post)

    assert result is True
    assert len(posts) == 0


async def test_retries_on_non_2xx_stops_on_success(tmp_db_path: Path) -> None:
    """Webhook retries on non-2xx; stops as soon as a 2xx is received."""
    call_count = 0

    async def flaky_post(url: str, payload: dict, *, timeout: float = 10.0) -> int:
        nonlocal call_count
        call_count += 1
        return 500 if call_count < 3 else 200

    config = Config(
        webhook=WebhookConfig(url="https://hooks.example.com", triggers=["Needs Human Review"])
    )

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db, {"title": "T", "description": "D", "acceptance_criteria": "AC"}
        )
        event = Event(
            ticket_id=ticket.id,
            timestamp="now",
            actor="router",
            old_state="In Progress",
            new_state="Needs Human Review",
        )
        t = await get_ticket(db, ticket.id)
        result = await fire_webhook(config, event, t, post_fn=flaky_post, backoff=(0, 0, 0))

    assert result is True
    assert call_count == 3  # 2 failures + 1 success


async def test_returns_false_after_max_retries(tmp_db_path: Path) -> None:
    """After 3 failed attempts, fire_webhook returns False (does not crash)."""
    call_count = 0

    async def always_fail(url: str, payload: dict, *, timeout: float = 10.0) -> int:
        nonlocal call_count
        call_count += 1
        return 503

    config = Config(
        webhook=WebhookConfig(url="https://hooks.example.com", triggers=["Needs Human Review"])
    )

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db, {"title": "T", "description": "D", "acceptance_criteria": "AC"}
        )
        event = Event(
            ticket_id=ticket.id,
            timestamp="now",
            actor="router",
            old_state="In Progress",
            new_state="Needs Human Review",
        )
        t = await get_ticket(db, ticket.id)
        result = await fire_webhook(config, event, t, post_fn=always_fail, backoff=(0, 0, 0))

    assert result is False
    assert call_count == 3
