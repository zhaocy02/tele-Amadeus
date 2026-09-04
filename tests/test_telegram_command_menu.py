import asyncio
import json
from typing import cast

import httpx

from amadeus_bot.telegram.command_menu import V2_BOT_COMMANDS
from amadeus_bot.telegram.http_gateway import TelegramHTTPGateway
from amadeus_bot.telegram.provider_router import V2_HELP_TEXT


def test_command_menu_matches_current_top_level_controls() -> None:
    names = tuple(command for command, _ in V2_BOT_COMMANDS)
    assert names == (
        "start",
        "help",
        "status",
        "provider",
        "memory",
        "remember",
        "forget",
        "profile",
        "history",
        "autonomy",
        "new",
        "cancel",
    )
    assert len(names) == len(set(names))
    assert all(description.strip() for _, description in V2_BOT_COMMANDS)
    assert all(f"/{name}" in V2_HELP_TEXT for name in names if name != "start")


def test_polling_syncs_command_menu_once() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = cast(dict[str, object], json.loads(request.content.decode()))
        calls.append((request.url.path, payload))
        if request.url.path.endswith("/setMyCommands"):
            return httpx.Response(200, json={"ok": True, "result": True})
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": []})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = TelegramHTTPGateway(
            bot_token="TEST",
            api_base_url="https://api.telegram.test",
            client=client,
        )
        try:
            assert await gateway.get_updates(timeout_seconds=0) == ()
            assert await gateway.get_updates(timeout_seconds=0) == ()
        finally:
            await client.aclose()

    asyncio.run(scenario())

    set_command_calls = [
        payload for path, payload in calls if path.endswith("/setMyCommands")
    ]
    assert len(set_command_calls) == 1
    assert set_command_calls[0] == {
        "commands": [
            {"command": command, "description": description}
            for command, description in V2_BOT_COMMANDS
        ]
    }
    assert sum(path.endswith("/getUpdates") for path, _ in calls) == 2


def test_command_menu_sync_failure_is_fail_soft_and_retries() -> None:
    set_commands_attempts = 0
    get_updates_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal set_commands_attempts, get_updates_calls
        if request.url.path.endswith("/setMyCommands"):
            set_commands_attempts += 1
            if set_commands_attempts == 1:
                return httpx.Response(500, json={"ok": False})
            return httpx.Response(200, json={"ok": True, "result": True})
        if request.url.path.endswith("/getUpdates"):
            get_updates_calls += 1
            return httpx.Response(200, json={"ok": True, "result": []})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = TelegramHTTPGateway(
            bot_token="TEST",
            api_base_url="https://api.telegram.test",
            client=client,
        )
        try:
            assert await gateway.get_updates(timeout_seconds=0) == ()
            assert await gateway.get_updates(timeout_seconds=0) == ()
            assert await gateway.get_updates(timeout_seconds=0) == ()
        finally:
            await client.aclose()

    asyncio.run(scenario())

    assert set_commands_attempts == 2
    assert get_updates_calls == 3
