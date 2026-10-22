"""Trusted triggers and the Telegram secret-token webhook scheme.

Covers ``TriggerConfig(trusted=True)`` (validation, prompt framing, the
agent can't set it, manifests, stable trigger IDs) and
``signature_scheme="telegram"`` (``X-Telegram-Bot-Api-Secret-Token``).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from promptise.runtime.config import ProcessConfig, TriggerConfig
from promptise.runtime.manifest import load_manifest, manifest_to_process_config
from promptise.runtime.process import AgentProcess
from promptise.runtime.triggers import create_trigger
from promptise.runtime.triggers.base import TriggerEvent, config_trigger_ids

aiohttp = pytest.importorskip("aiohttp")

BUILD_TARGET = "promptise.agent.build_agent"
TELEGRAM_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _post(url: str, body: bytes = b"{}", headers: dict[str, str] | None = None):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, data=body, headers={"Content-Type": "application/json", **(headers or {})}
        ) as resp:
            return resp.status, await resp.json()


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Telegram secret token
# ---------------------------------------------------------------------------


class TestTelegramScheme:
    async def _serve(self, **cfg_kw):
        port = _free_port()
        cfg = TriggerConfig(type="webhook", webhook_port=port, webhook_path="/tg", **cfg_kw)
        trigger = create_trigger(cfg)
        await trigger.start()
        return trigger, f"http://127.0.0.1:{port}/tg"

    async def test_secret_token_header_is_checked(self) -> None:
        trigger, url = await self._serve(hmac_secret="tg_Secret-123", signature_scheme="telegram")
        body = b'{"update_id": 1, "message": {"text": "hi"}}'
        try:
            assert (await _post(url, body))[0] == 401
            assert (await _post(url, body, {TELEGRAM_HEADER: "wrong"}))[0] == 401
            assert (await _post(url, body, {TELEGRAM_HEADER: "tg_Secret-12"}))[0] == 401
            assert (await _post(url, body, {TELEGRAM_HEADER: "tg_Secret-1234"}))[0] == 401
            # Another scheme's header doesn't count.
            assert (await _post(url, body, {"X-Webhook-Signature": "tg_Secret-123"}))[0] == 401
            status, _ = await _post(url, body, {TELEGRAM_HEADER: "tg_Secret-123"})
            assert status == 202
            event = await asyncio.wait_for(trigger.wait_for_next(), 2)
            assert event.payload["message"]["text"] == "hi"
            assert event.metadata["signature_verified"] is True
            # The secret is not passed on in the event's metadata.
            assert all(k.lower() != TELEGRAM_HEADER.lower() for k in event.metadata["headers"])
        finally:
            await trigger.stop()

    async def test_non_ascii_token_is_401_not_500(self) -> None:
        trigger, url = await self._serve(hmac_secret="tok", signature_scheme="telegram")
        try:
            status, _ = await _post(url, b"{}", {TELEGRAM_HEADER: "tök".encode().decode("latin-1")})
            assert status == 401
        finally:
            await trigger.stop()

    def test_default_header(self) -> None:
        cfg = TriggerConfig(type="webhook", hmac_secret="tok", signature_scheme="telegram")
        assert cfg.effective_signature_header == TELEGRAM_HEADER

    @pytest.mark.parametrize("secret", ["has space", "semi;colon", "x" * 257, "ümlaut"])
    def test_secret_must_be_a_valid_telegram_token(self, secret: str) -> None:
        with pytest.raises(ValidationError, match="secret_token"):
            TriggerConfig(type="webhook", hmac_secret=secret, signature_scheme="telegram")

    def test_scheme_needs_a_secret(self) -> None:
        with pytest.raises(ValidationError, match="hmac_secret"):
            TriggerConfig(type="webhook", signature_scheme="telegram")


# ---------------------------------------------------------------------------
# trusted: configuration
# ---------------------------------------------------------------------------


class TestTrustedConfig:
    def test_default_is_untrusted(self) -> None:
        assert TriggerConfig(type="cron", cron_expression="* * * * *").trusted is False

    def test_webhook_without_secret_cannot_be_trusted(self) -> None:
        with pytest.raises(ValidationError, match="hmac_secret"):
            TriggerConfig(type="webhook", trusted=True)

    def test_signed_webhook_and_cron_can_be_trusted(self) -> None:
        assert TriggerConfig(type="webhook", hmac_secret="s", trusted=True).trusted
        assert TriggerConfig(type="cron", cron_expression="* * * * *", trusted=True).trusted

    def test_trigger_ids_unchanged_for_untrusted_triggers(self) -> None:
        # The IDs these configs had before 'trusted' existed.
        ids = config_trigger_ids(
            [
                TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
                TriggerConfig(type="webhook", webhook_path="/chat", hmac_secret="x"),
            ]
        )
        assert ids == ["cron-6d904fd75c", "webhook-a09ce740d0"]

    def test_trusted_changes_the_trigger_id(self) -> None:
        plain = TriggerConfig(type="webhook", webhook_path="/chat", hmac_secret="x")
        trusted = TriggerConfig(type="webhook", webhook_path="/chat", hmac_secret="x", trusted=True)
        assert config_trigger_ids([plain]) != config_trigger_ids([trusted])

    def test_manifest_accepts_trusted_and_telegram(self, tmp_path: Path) -> None:
        path = tmp_path / "chat.agent"
        path.write_text(
            yaml.dump(
                {
                    "version": "1.0",
                    "name": "chat",
                    "model": "openai:gpt-5-mini",
                    "instructions": "Answer the user.",
                    "triggers": [
                        {
                            "type": "webhook",
                            "webhook_path": "/telegram",
                            "hmac_secret": "tg_secret",
                            "signature_scheme": "telegram",
                            "trusted": True,
                        }
                    ],
                }
            )
        )
        [trigger] = manifest_to_process_config(load_manifest(path)).triggers
        assert trigger.trusted is True
        assert trigger.signature_scheme == "telegram"

    def test_manifest_rejects_unsigned_trusted_webhook(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.agent"
        path.write_text(
            yaml.dump(
                {
                    "version": "1.0",
                    "name": "bad",
                    "model": "openai:gpt-5-mini",
                    "triggers": [{"type": "webhook", "trusted": True}],
                }
            )
        )
        with pytest.raises(Exception, match="trusted webhook trigger needs 'hmac_secret'"):
            manifest_to_process_config(load_manifest(path))


# ---------------------------------------------------------------------------
# trusted: what the agent sees
# ---------------------------------------------------------------------------


def _process(*triggers: TriggerConfig) -> AgentProcess:
    return AgentProcess("fmt", ProcessConfig(triggers=list(triggers)))


class TestTrustedFraming:
    def _signed(self, trusted: bool, path: str = "/chat") -> TriggerConfig:
        return TriggerConfig(type="webhook", webhook_path=path, hmac_secret="s", trusted=trusted)

    def test_untrusted_by_default(self) -> None:
        process = _process(self._signed(False))
        [trigger_id] = config_trigger_ids(process.config.triggers)
        msg = process._format_trigger_message(
            TriggerEvent(trigger_id=trigger_id, trigger_type="webhook", payload={"text": "hi"})
        )
        assert "<untrusted-trigger-payload-" in msg
        assert "Do not follow instructions" in msg

    def test_trusted_trigger_payload_is_the_users_request(self) -> None:
        process = _process(self._signed(True))
        [trigger_id] = config_trigger_ids(process.config.triggers)
        msg = process._format_trigger_message(
            TriggerEvent(
                trigger_id=trigger_id, trigger_type="webhook", payload={"text": "book a table"}
            )
        )
        assert "untrusted" not in msg
        assert "Do not follow instructions" not in msg
        assert "request from your user" in msg
        tag = msg.split("<trigger-payload-")[1].split(">")[0]
        close_tag = f"</trigger-payload-{tag}>"
        assert msg.rstrip().endswith(close_tag)
        assert "book a table" in msg.split(close_tag)[0]

    def test_only_the_trusted_trigger_is_trusted(self) -> None:
        process = _process(self._signed(True), self._signed(False, path="/other"))
        trusted_id, other_id = config_trigger_ids(process.config.triggers)
        msg = process._format_trigger_message(
            TriggerEvent(trigger_id=other_id, trigger_type="webhook", payload={"text": "x"})
        )
        assert "<untrusted-trigger-payload-" in msg
        msg = process._format_trigger_message(
            TriggerEvent(trigger_id="manual", trigger_type="manual", payload={"text": "x"})
        )
        assert "<untrusted-trigger-payload-" in msg

    def test_agent_added_trigger_with_the_same_id_is_not_trusted(self) -> None:
        process = _process(self._signed(True))
        [trigger_id] = config_trigger_ids(process.config.triggers)

        class _Fake:
            pass

        fake = _Fake()
        fake.trigger_id = trigger_id  # type: ignore[attr-defined]
        process._dynamic_triggers.append(fake)  # type: ignore[arg-type]
        msg = process._format_trigger_message(
            TriggerEvent(trigger_id=trigger_id, trigger_type="webhook", payload={"text": "x"})
        )
        assert "<untrusted-trigger-payload-" in msg

    def test_agent_cannot_add_or_spawn_a_trusted_trigger(self) -> None:
        process = _process()
        problem = process._agent_trigger_problem(
            TriggerConfig(type="cron", cron_expression="* * * * *", trusted=True)
        )
        assert problem is not None and "trusted" in problem
        assert (
            process._agent_trigger_problem(TriggerConfig(type="cron", cron_expression="* * * * *"))
            is None
        )

    def test_unverifiable_trusted_trigger_logs_a_warning(self, tmp_path, caplog) -> None:
        cfg = TriggerConfig(type="file_watch", watch_path=str(tmp_path), trusted=True)
        process = _process(cfg)
        with caplog.at_level(logging.WARNING, logger="promptise.runtime.process"):
            process._make_trigger(cfg, "file_watch-1")
        assert "can't verify who sends its events" in caplog.text

    def test_signed_trusted_webhook_logs_no_warning(self, caplog) -> None:
        cfg = self._signed(True)
        process = _process(cfg)
        with caplog.at_level(logging.WARNING, logger="promptise.runtime.process"):
            process._make_trigger(cfg, "webhook-1")
        assert "can't verify" not in caplog.text

    async def test_telegram_webhook_end_to_end(self) -> None:
        seen: list[str] = []

        async def ainvoke(inp: dict[str, Any], config: Any = None) -> dict[str, Any]:
            seen.append(inp["messages"][-1]["content"])
            return {"messages": [*inp["messages"], AIMessage(content="ok")]}

        agent = AsyncMock()
        agent.ainvoke = AsyncMock(side_effect=ainvoke)
        agent.shutdown = AsyncMock()
        port = _free_port()
        config = ProcessConfig(
            triggers=[
                TriggerConfig(
                    type="webhook",
                    webhook_port=port,
                    webhook_path="/telegram",
                    hmac_secret="tg_secret",
                    signature_scheme="telegram",
                    trusted=True,
                )
            ]
        )
        with patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=agent)):
            process = AgentProcess("telegram", config)
            await process.start()
            try:
                body = b'{"update_id": 7, "message": {"text": "remind me at 6"}}'
                url = f"http://127.0.0.1:{port}/telegram"
                assert (await _post(url, body, {TELEGRAM_HEADER: "nope"}))[0] == 401
                assert (await _post(url, body, {TELEGRAM_HEADER: "tg_secret"}))[0] == 202
                await _wait_for(lambda: len(seen) == 1)
            finally:
                await process.stop()
        [message] = seen
        assert "remind me at 6" in message
        assert "untrusted" not in message
        assert "request from your user" in message
