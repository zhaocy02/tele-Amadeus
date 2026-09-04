from __future__ import annotations

import argparse
import asyncio
import signal
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from amadeus_bot.app import (
    build_provider,
    build_telegram_gateway,
    build_v2_runtime,
    build_v2_telegram_application,
)
from amadeus_bot.character import (
    AutonomyAction,
    AutonomyDecision,
    AutonomyOpportunity,
    AutonomySignal,
    AutonomySignalKind,
    load_legacy_persona,
    load_persona_core,
)
from amadeus_bot.config import AppSettings, ConfigurationError, load_settings
from amadeus_bot.logging_config import configure_logging
from amadeus_bot.memory import (
    MemoryKind,
    MemoryRecord,
    MemorySourceType,
    rehearse_legacy_v1_memory_migration,
)
from amadeus_bot.runtime import AutonomyRuntimeEvaluation, prepare_v2_cutover_data
from amadeus_bot.telegram import (
    IncomingMessage,
    TelegramInboxStore,
    V2AutonomyPilotRunner,
    V2TelegramPollingRunner,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Amadeus v2 runtime and migration utilities")
    parser.add_argument(
        "command",
        choices=(
            "check-config",
            "smoke",
            "smoke-v2",
            "smoke-v2-retrospective",
            "smoke-v2-autonomy",
            "smoke-v2-telegram",
            "smoke-v2-autonomy-telegram",
            "smoke-v2-polling",
            "rehearse-v1-memory-migration",
            "prepare-v2-cutover-data",
            "run-v2",
        ),
        nargs="?",
        default="check-config",
        help="validate, smoke-test, prepare cutover data, or run the guarded v2 poller",
    )
    parser.add_argument(
        "--text",
        default="你觉得长期角色记忆最容易出什么问题？",
        help="user text for v2 smoke commands; ignored by other commands",
    )
    parser.add_argument(
        "--chat-id",
        type=int,
        default=None,
        help="authorized private chat target or explicitly selected legacy chat",
    )
    parser.add_argument(
        "--confirm-send",
        action="store_true",
        help="required acknowledgement for commands that actually send a Telegram message",
    )
    parser.add_argument(
        "--confirm-polling",
        action="store_true",
        help="required acknowledgement for commands that call Telegram getUpdates",
    )
    parser.add_argument(
        "--poll-smoke-timeout-seconds",
        type=float,
        default=45.0,
        help="maximum wait for one authorized dev update during smoke-v2-polling",
    )
    parser.add_argument(
        "--source-copy",
        type=Path,
        default=None,
        help="disposable snapshot of the production-compatible v1 memory SQLite database",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=None,
        help="dedicated target directory for migration rehearsal or cutover data preparation",
    )
    parser.add_argument(
        "--confirm-disposable-copy",
        action="store_true",
        help="required acknowledgement that --source-copy is not the only production database",
    )
    parser.add_argument(
        "--confirm-cutover-target",
        action="store_true",
        help="required acknowledgement that --target-dir is a new empty v2 cutover data directory",
    )
    return parser


def _check_config() -> None:
    settings = load_settings()
    legacy = load_legacy_persona(settings.persona_dir)
    v2 = load_persona_core(settings.persona_v2_path)
    print(
        "Configuration valid; "
        f"legacy persona hash={legacy.version_hash[:12]}; "
        f"v2 persona={v2.core.persona_version} hash={v2.version_hash[:12]}; "
        f"long_polling_enabled={str(settings.enable_long_polling).lower()}; "
        f"autonomy_pilot_enabled={str(settings.enable_autonomy_pilot).lower()}; "
        f"autonomy_timezone={settings.autonomy_timezone}."
    )


async def _smoke() -> None:
    settings = load_settings()
    load_legacy_persona(settings.persona_dir)
    provider = build_provider(settings)
    telegram = build_telegram_gateway(settings)
    try:
        await provider.healthcheck()
        identity = await telegram.get_me()
        username = identity.get("username")
        printable_username = username if isinstance(username, str) else "<unknown>"
        print(f"Non-polling smoke passed; Telegram identity @{printable_username}.")
    finally:
        await provider.aclose()
        await telegram.aclose()


async def _smoke_v2(text: str) -> None:
    settings = load_settings()
    with TemporaryDirectory(prefix="amadeus-v2-smoke-") as temporary_dir:
        app = build_v2_runtime(settings, data_dir_override=Path(temporary_dir))
        try:
            prepared = await app.conversation.prepare_user_turn(chat_id=1, user_text=text)
            finalized = await app.conversation.finalize_delivered_turn(prepared)
            _require_clean_finalize(finalized.exchange is not None, finalized.warnings)
            print(
                "V2 non-polling smoke passed; "
                f"policy={prepared.turn_result.policy.act.value}; "
                f"state_version={finalized.state_version}; warnings=none."
            )
            print("V2 reply:")
            print(prepared.reply_text)
        finally:
            await app.aclose()


async def _smoke_v2_retrospective(text: str) -> None:
    settings = load_settings()
    with TemporaryDirectory(prefix="amadeus-v2-retrospective-smoke-") as temporary_dir:
        app = build_v2_runtime(
            settings,
            data_dir_override=Path(temporary_dir),
            retrospective_interval_turns=2,
        )
        try:
            finalized = None
            for user_text in (text, "基于刚才的交流继续说，但不要把推测当成事实。"):
                prepared = await app.conversation.prepare_user_turn(chat_id=1, user_text=user_text)
                finalized = await app.conversation.finalize_delivered_turn(prepared)
                _require_clean_finalize(finalized.exchange is not None, finalized.warnings)
            if finalized is None or finalized.retrospective is None:
                raise RuntimeError("Retrospective smoke reached two turns but did not run")
            print(
                "V2 Retrospective smoke passed; "
                f"changed={str(finalized.retrospective.changed).lower()}; "
                f"reason={finalized.retrospective.reason_label}; "
                f"state_version={finalized.state_version}; warnings=none."
            )
        finally:
            await app.aclose()


async def _smoke_v2_autonomy(text: str) -> None:
    settings = load_settings()
    with TemporaryDirectory(prefix="amadeus-v2-autonomy-smoke-") as temporary_dir:
        app = build_v2_runtime(settings, data_dir_override=Path(temporary_dir))
        try:
            app.sessions.append_exchange(1, text, "先记着，之后有机会再接着聊。")
            last_user = app.sessions.last_user_message_at(1)
            if last_user is None:
                raise RuntimeError("autonomy smoke failed to persist synthetic user history")
            await app.memory.upsert(
                MemoryRecord(
                    memory_id="smoke-open-thread",
                    kind=MemoryKind.OPEN_THREAD,
                    content="用户留下了一个可以稍后继续讨论的角色记忆话题。",
                    confidence=1.0,
                    salience=0.9,
                    created_at=datetime.now(UTC),
                    source_message_ids=(),
                    source_type=MemorySourceType.MANUAL,
                )
            )
            evaluation = await app.autonomy.evaluate(1, at=last_user + timedelta(hours=3))
            if not evaluation.due or evaluation.opportunity is None:
                raise RuntimeError("autonomy smoke did not evaluate a due opportunity")
            if evaluation.decision.reason_label == "autonomy_failure":
                raise RuntimeError("autonomy provider/schema validation failed")
            stats = app.autonomy_store.delivery_stats(
                1,
                evaluation.generation,
                now=evaluation.evaluated_at,
                last_user_message_at=last_user,
            )
            if stats.autonomy_messages_last_24h != 0:
                raise RuntimeError("autonomy smoke recorded a delivery without sending anything")
            print(
                "V2 Autonomy smoke passed; "
                f"action={evaluation.decision.action.value}; "
                f"reason={evaluation.decision.reason_label or '<none>'}; "
                f"would_prepare_delivery={str(evaluation.should_prepare_delivery).lower()}; "
                "confirmed_deliveries=0."
            )
        finally:
            await app.aclose()


async def _smoke_v2_telegram(text: str, chat_id: int | None, confirm_send: bool) -> None:
    settings = load_settings()
    target = _require_explicit_dev_send(settings, chat_id, confirm_send)
    with TemporaryDirectory(prefix="amadeus-v2-telegram-smoke-") as temporary_dir:
        app = build_v2_telegram_application(settings, data_dir_override=Path(temporary_dir))
        try:
            result = await app.delivery.deliver_user_message(
                IncomingMessage(
                    chat_id=target,
                    user_id=target,
                    message_id=1,
                    text=text,
                    received_at=datetime.now(UTC),
                )
            )
            if result.finalize is None or result.finalize.exchange is None:
                raise RuntimeError("Telegram v2 smoke sent text but did not persist delivered turn")
            if result.warnings:
                raise RuntimeError(
                    "Telegram v2 smoke post-delivery warnings: " + ",".join(result.warnings)
                )
            print(
                "V2 Telegram delivery smoke passed; "
                f"telegram_message_id={result.telegram_message_id}; warnings=none."
            )
        finally:
            await app.aclose()


async def _smoke_v2_autonomy_telegram(chat_id: int | None, confirm_send: bool) -> None:
    settings = load_settings()
    target = _require_explicit_dev_send(settings, chat_id, confirm_send)
    with TemporaryDirectory(prefix="amadeus-v2-autonomy-telegram-smoke-") as temporary_dir:
        app = build_v2_telegram_application(settings, data_dir_override=Path(temporary_dir))
        try:
            now = datetime.now(UTC)
            app.runtime.sessions.append_exchange(
                target,
                "这个话题之后有机会可以再继续。",
                "好。",
            )
            last_user = app.runtime.sessions.last_user_message_at(target)
            if last_user is None:
                raise RuntimeError("autonomy Telegram smoke has no synthetic user history")
            signal_record = AutonomySignal(
                signal_id="open-thread:telegram-delivery-smoke",
                kind=AutonomySignalKind.OPEN_THREAD,
                summary="用户之前留下了一个可以自然继续的角色记忆边界话题。",
                salience=0.95,
                source_thread_id="telegram-delivery-smoke",
            )
            evaluation = AutonomyRuntimeEvaluation(
                chat_id=target,
                generation=app.runtime.sessions.current_generation(target),
                evaluated_at=now,
                due=True,
                opportunity=AutonomyOpportunity(
                    now=now,
                    last_user_message_at=last_user,
                    signals=(signal_record,),
                    current_state_summary="relationship_tone: baseline",
                    relationship_summary="baseline",
                ),
                decision=AutonomyDecision(
                    action=AutonomyAction.FOLLOW_UP,
                    selected_signal_id=signal_record.signal_id,
                    motivation=0.9,
                    focus="自然地接回之前留下的话题，不要假装用户刚刚发了消息",
                    reason_label="server_dev_delivery_smoke",
                ),
            )
            result = await app.delivery.deliver_autonomy_evaluation(evaluation)
            if result.stored_delivery is None or result.transcript is None:
                raise RuntimeError(
                    "autonomy Telegram send succeeded but persistence was incomplete"
                )
            if result.warnings:
                raise RuntimeError(
                    "autonomy Telegram delivery warnings: " + ",".join(result.warnings)
                )
            stats = app.runtime.autonomy_store.delivery_stats(
                target,
                evaluation.generation,
                now=datetime.now(UTC),
                last_user_message_at=None,
            )
            if stats.autonomy_messages_last_24h != 1:
                raise RuntimeError("autonomy Telegram smoke did not record confirmed delivery")
            print(
                "V2 Autonomy Telegram delivery smoke passed; "
                f"telegram_message_id={result.telegram_message_id}; "
                "confirmed_deliveries=1; transcript=1; warnings=none."
            )
        finally:
            await app.aclose()


async def _smoke_v2_polling(confirm_polling: bool, timeout_seconds: float) -> None:
    settings = load_settings()
    if settings.environment == "production":
        raise RuntimeError("polling smoke is disabled in production environment")
    if not confirm_polling:
        raise RuntimeError("polling smoke requires --confirm-polling")
    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise RuntimeError("polling smoke timeout must be between 0 and 300 seconds")
    configure_logging(settings.log_level)

    with TemporaryDirectory(prefix="amadeus-v2-polling-smoke-") as temporary_dir:
        data_dir = Path(temporary_dir)
        app = build_v2_telegram_application(settings, data_dir_override=data_dir)
        inbox = TelegramInboxStore(data_dir / "telegram-inbox.sqlite")
        stop_event = asyncio.Event()
        runner = V2TelegramPollingRunner(
            gateway=app.telegram,
            handler=app.router,
            inbox=inbox,
            allowed_user_ids=settings.allowed_user_ids,
            poll_timeout_seconds=2,
            retry_delay_seconds=1,
        )
        try:
            await app.runtime.provider.healthcheck()
            await app.telegram.get_me()
            try:
                completed = await asyncio.wait_for(
                    runner.run(
                        stop_event=stop_event,
                        max_completed_messages=1,
                        initial_offset=-1,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                stop_event.set()
                raise RuntimeError(
                    "polling smoke timed out waiting for one authorized dev message"
                ) from None
            if completed < 1:
                raise RuntimeError("polling smoke exited without handling an authorized message")
            print("V2 polling smoke passed; authorized_messages=1; durable_inbox=completed.")
        finally:
            inbox.close()
            await app.aclose()


async def _run_v2(confirm_polling: bool) -> None:
    settings = load_settings()
    if not confirm_polling:
        raise RuntimeError("run-v2 requires --confirm-polling")
    if not settings.enable_long_polling:
        raise RuntimeError(
            "run-v2 refused: AMADEUS_ENABLE_LONG_POLLING must be explicitly true"
        )
    if settings.environment == "test":
        raise RuntimeError("run-v2 is disabled in test environment")
    configure_logging(settings.log_level)

    app = build_v2_telegram_application(settings)
    inbox = TelegramInboxStore(app.runtime.data_dir / "telegram-inbox.sqlite")
    stop_event = asyncio.Event()
    polling_runner = V2TelegramPollingRunner(
        gateway=app.telegram,
        handler=app.router,
        inbox=inbox,
        allowed_user_ids=settings.allowed_user_ids,
    )
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_number, stop_event.set)

    autonomy_task: asyncio.Task[None] | None = None
    try:
        await app.runtime.provider.healthcheck()
        identity = await app.telegram.get_me()
        username = identity.get("username")
        printable_username = username if isinstance(username, str) else "<unknown>"
        print(f"Amadeus v2 polling started; Telegram identity @{printable_username}.")
        if settings.enable_autonomy_pilot:
            autonomy_runner = V2AutonomyPilotRunner(
                autonomy=app.runtime.autonomy,
                delivery=app.delivery,
                router=app.router,
                conversation=app.runtime.conversation,
                preferences=app.runtime.preferences,
                allowed_chat_ids=settings.allowed_user_ids,
                timezone=settings.autonomy_timezone,
            )
            autonomy_task = asyncio.create_task(autonomy_runner.run(stop_event=stop_event))
            print(
                "Autonomy pilot scheduler enabled; per-chat /autonomy opt-in is still required."
            )
        else:
            print("Autonomy pilot scheduler disabled by process gate.")
        await polling_runner.run(stop_event=stop_event)
    finally:
        stop_event.set()
        if autonomy_task is not None:
            autonomy_task.cancel()
            with suppress(asyncio.CancelledError):
                await autonomy_task
        inbox.close()
        await app.aclose()


async def _rehearse_v1_memory_migration(
    source_copy: Path | None,
    target_dir: Path | None,
    chat_id: int | None,
    confirm_disposable_copy: bool,
) -> None:
    if not confirm_disposable_copy:
        raise RuntimeError("migration rehearsal requires --confirm-disposable-copy")
    if source_copy is None:
        raise RuntimeError("migration rehearsal requires --source-copy")
    if target_dir is None:
        raise RuntimeError("migration rehearsal requires --target-dir")
    if chat_id is None:
        raise RuntimeError("migration rehearsal requires --chat-id")
    target = target_dir.expanduser().resolve()
    report = await rehearse_legacy_v1_memory_migration(
        source_copy=source_copy,
        target_db=target / "structured-memory.sqlite",
        chat_id=chat_id,
    )
    target.mkdir(parents=True, exist_ok=True)
    report_path = target / "migration-report.json"
    report_path.write_text(report.to_json(), encoding="utf-8")
    print(
        "V1 memory migration rehearsal passed; "
        f"source_rows={report.source_rows}; active={report.active_rows}; "
        f"forgotten={report.forgotten_rows}; expired={report.expired_rows}; "
        f"first_created={report.first_pass.created}; "
        f"first_updated={report.first_pass.updated}; "
        f"second_created={report.second_pass.created}; "
        f"second_updated={report.second_pass.updated}; "
        f"source_unchanged={str(report.source_unchanged).lower()}; "
        f"memory_enabled={str(report.memory_enabled).lower()}; "
        f"sensitive_flagged_rows={report.sensitive_flagged_rows}; "
        f"report={report_path}."
    )


async def _prepare_v2_cutover_data(
    source_copy: Path | None,
    target_dir: Path | None,
    chat_id: int | None,
    confirm_disposable_copy: bool,
    confirm_cutover_target: bool,
) -> None:
    if not confirm_disposable_copy:
        raise RuntimeError("cutover preparation requires --confirm-disposable-copy")
    if not confirm_cutover_target:
        raise RuntimeError("cutover preparation requires --confirm-cutover-target")
    if source_copy is None or target_dir is None or chat_id is None:
        raise RuntimeError(
            "cutover preparation requires --source-copy, --target-dir, and --chat-id"
        )
    target = target_dir.expanduser().resolve()
    report = await prepare_v2_cutover_data(
        source_snapshot=source_copy,
        target_data_dir=target,
        chat_id=chat_id,
    )
    report_path = target / "cutover-preparation-report.json"
    report_path.write_text(report.to_json(), encoding="utf-8")
    report_path.chmod(0o600)
    migration = report.migration
    print(
        "V2 cutover data prepared; "
        f"source_rows={migration.source_rows}; target_records={migration.target_records}; "
        f"source_unchanged={str(migration.source_unchanged).lower()}; "
        f"memory_enabled={str(migration.memory_enabled).lower()}; "
        f"memory_preference_applied={str(report.memory_preference_applied).lower()}; "
        f"report={report_path}."
    )


def _require_explicit_dev_send(
    settings: AppSettings,
    chat_id: int | None,
    confirm_send: bool,
) -> int:
    if settings.environment == "production":
        raise RuntimeError("real Telegram delivery smoke is disabled in production environment")
    if not confirm_send:
        raise RuntimeError("real Telegram delivery smoke requires --confirm-send")
    if chat_id is None:
        raise RuntimeError("real Telegram delivery smoke requires --chat-id")
    if chat_id not in settings.allowed_user_ids:
        raise RuntimeError("Telegram delivery smoke chat ID is not allowlisted")
    return chat_id


def _require_clean_finalize(exchange_persisted: bool, warnings: tuple[str, ...]) -> None:
    if not exchange_persisted:
        raise RuntimeError("v2 smoke generated a reply but failed to persist the delivered turn")
    if warnings:
        raise RuntimeError(f"v2 smoke post-delivery warnings: {','.join(warnings)}")


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "check-config":
            _check_config()
        elif args.command == "smoke":
            asyncio.run(_smoke())
        elif args.command == "smoke-v2":
            asyncio.run(_smoke_v2(args.text))
        elif args.command == "smoke-v2-retrospective":
            asyncio.run(_smoke_v2_retrospective(args.text))
        elif args.command == "smoke-v2-autonomy":
            asyncio.run(_smoke_v2_autonomy(args.text))
        elif args.command == "smoke-v2-telegram":
            asyncio.run(_smoke_v2_telegram(args.text, args.chat_id, args.confirm_send))
        elif args.command == "smoke-v2-autonomy-telegram":
            asyncio.run(_smoke_v2_autonomy_telegram(args.chat_id, args.confirm_send))
        elif args.command == "smoke-v2-polling":
            asyncio.run(
                _smoke_v2_polling(
                    args.confirm_polling,
                    args.poll_smoke_timeout_seconds,
                )
            )
        elif args.command == "rehearse-v1-memory-migration":
            asyncio.run(
                _rehearse_v1_memory_migration(
                    args.source_copy,
                    args.target_dir,
                    args.chat_id,
                    args.confirm_disposable_copy,
                )
            )
        elif args.command == "prepare-v2-cutover-data":
            asyncio.run(
                _prepare_v2_cutover_data(
                    args.source_copy,
                    args.target_dir,
                    args.chat_id,
                    args.confirm_disposable_copy,
                    args.confirm_cutover_target,
                )
            )
        else:
            asyncio.run(_run_v2(args.confirm_polling))
    except (ConfigurationError, OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Amadeus check failed: {exc}") from None


if __name__ == "__main__":
    main()
