"""Точка входа: `python -m vacancy_hunter <команда>`.

Команды:
  check-config — проверить семь файлов конфигурации и обязательные секреты (шаг A1);
  check-env    — проверить доступ к базе, Telegram и внешнему пульсу (шаг A2; API LLM — шаг A5);
  probe-sources — пробный запрос к каждому источнику: «доступен» или причина (шаг A3);
  collect      — разовый сбор по всем источникам или по одному (--source), --force — без учёта интервала;
  prompt-scenarios — сценарии T1–T20 промпта P1 на реальной модели, расход и замеры (шаг A5, платно);
  process      — разовая обработка накопленных записей: дедупликация и правила (шаг A4);
  unblock-source CODE — снять запрет с источника, отключённого системой (решение владельца, правило R6);
  run          — запуск процесса приложения: планировщик, сбор, отправка сообщений, бот.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from hunter_core.config import ConfigError, SecretsError
from hunter_core.observability import setup_logging
from vacancy_hunter.config import (
    DEFAULT_CONFIG_DIR,
    LATER_SECRETS,
    AppConfig,
    later_secrets_status,
    load_app_config,
    read_app_database,
    read_telegram,
    secret_values,
)

log = logging.getLogger("vacancy_hunter.main")


def _check(config_dir: Path, env: Mapping[str, str]) -> tuple[AppConfig | None, list[str]]:
    """Проверка конфигурации и секретов. Возвращает конфигурацию и список проблем."""
    problems: list[str] = []
    config = None
    try:
        config = load_app_config(config_dir)
    except ConfigError as exc:
        problems.extend(exc.problems)
    for reader in (read_app_database, read_telegram):
        try:
            reader(env)
        except SecretsError as exc:
            problems.append(f".env: {exc}")
    return config, problems


def cmd_check_config(config_dir: Path, env: Mapping[str, str]) -> int:
    _, problems = _check(config_dir, env)
    if problems:
        print("Конфигурация НЕ прошла проверку:")
        for line in problems:
            print(f"  - {line}")
        return 1
    print("Конфигурация в порядке: 7 файлов проверено, параметры подключения к базе заданы.")
    print("Необязательные сейчас секреты:")
    for name, is_set in later_secrets_status(env).items():
        print(f"  - {name}: {'задан' if is_set else 'не задан'} (шаг {LATER_SECRETS[name]})")
    return 0


def _verify_database(env: Mapping[str, str]) -> None:
    """Подключиться под пользователем приложения и убедиться, что миграции применены."""
    from alembic.script import ScriptDirectory
    from alembic.config import Config
    from sqlalchemy import text

    from hunter_core.storage import create_db_engine

    engine = create_db_engine(read_app_database(env), application_name="vacancy_hunter")
    try:
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()
    root = Path(__file__).resolve().parents[2]
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    head = script.get_current_head()
    if current != head:
        raise RuntimeError(f"схема базы не обновлена: версия {current}, ожидается {head}. Запустите сервис migrate")


def _budget_status(engine, config, clock):
    """Строки бюджета для /status."""
    from vacancy_hunter.pipeline.extract import build_budget

    budget = build_budget(engine, config, clock=clock)

    async def lines() -> list[str]:
        snap = await budget.snapshot()
        return ["", "<b>Бюджет LLM</b>",
                f"месяц: потрачено ${snap['spent']:.4f}, в резерве ${snap['reserved']:.4f}, "
                f"остаток ${snap['remaining']:.4f} из ${snap['limit']:.2f}",
                f"сегодня на извлечение: ${snap['today_extraction']:.4f} из ${snap['daily_limit']:.2f}; "
                f"вызовов за месяц {snap['calls']}; доля чтения из кэша {snap['cache_read_share'] * 100:.0f}%"]

    return lines


def register_pipeline_jobs(app) -> None:
    """Задачи конвейера: сбор каждые N часов и сразу после старта (ARCHITECTURE.md, разделы 11–12)."""
    from apscheduler.triggers.interval import IntervalTrigger

    from vacancy_hunter.pipeline.collect import collect

    async def collect_job():
        return await collect(app.engine, app.config, alert=app.alerts.send, clock=app.clock)

    from vacancy_hunter.pipeline.process import process

    from vacancy_hunter.config import refresh_matching_config

    async def process_job():
        from vacancy_hunter.reporting.rescored import notify_rescored

        app.config = refresh_matching_config(app.config)
        app.bot_context.config = app.config
        stats = await process(app.engine, app.config, clock=app.clock, gateway=app.gateway)
        await notify_rescored(app.engine, app.owner_id, timezone=app.config.schedule.timezone, clock=app.clock,
                              wake=app.outbox.wake if app.outbox else None)
        return stats

    async def collect_then_process():
        stats = await collect_job()
        await app.runner.run("process", process_job)   # новые записи обрабатываются сразу после сбора
        return stats

    from vacancy_hunter.pipeline.recalc import recalc

    async def recalc_job():
        # Свои блокировки не пересекаются со сбором, обработкой и отчётом (TECHNICAL_SPEC.md, раздел 19).
        app.config = refresh_matching_config(app.config)
        return await recalc(app.engine, app.config, clock=app.clock, notify=app.alerts.send)

    app.scheduler.add_locked("collect", collect_then_process,
                             IntervalTrigger(hours=app.config.schedule.collect_every_hours))
    app.scheduler.add_locked("process", process_job,
                             IntervalTrigger(minutes=app.config.schedule.process_every_minutes))
    app.scheduler.add_locked("recalc", recalc_job,
                             IntervalTrigger(minutes=app.config.schedule.recalc_every_minutes))
    app.startup_jobs.append(("collect", collect_then_process))
    app.startup_jobs.append(("recalc", recalc_job))      # новая версия профиля или правил — пересчёт сразу


def actuality_checker(config):
    from hunter_core.http import HttpClient, HttpPolicy
    from vacancy_hunter.reporting.actuality import ActualityChecker

    http = HttpClient(["t.me"], user_agent=config.sources.user_agent,
                      policy=HttpPolicy(max_retries=0, read_timeout=15.0, default_domain_pause=1.0))
    return ActualityChecker(http), http


def register_maintenance_jobs(app, db_settings) -> None:
    """Очистка в 03:30 и резервная копия в 04:00 по Тбилиси, с догоняющим запуском (ARCHITECTURE.md, 11)."""
    from datetime import time as dtime

    from apscheduler.triggers.cron import CronTrigger

    from vacancy_hunter.app import DailyJob
    from vacancy_hunter.ops import done_today, run_backup
    from vacancy_hunter.pipeline.cleanup import cleanup

    tz = app.config.schedule.timezone

    async def cleanup_job():
        return await cleanup(app.engine, app.config, clock=app.clock)

    async def backup_job():
        return await run_backup(app.engine, app.config, db_settings, owner_chat_id=app.owner_id, clock=app.clock,
                                wake=app.outbox.wake if app.outbox else None)

    for name, func, at in (("cleanup", cleanup_job, app.config.schedule.cleanup_time),
                           ("backup", backup_job, app.config.schedule.backup_time)):
        hour, minute = (int(x) for x in at.split(":"))
        app.scheduler.add_locked(name, func, CronTrigger(hour=hour, minute=minute, timezone=tz))
        app.daily_jobs.append(DailyJob(name, func, dtime(hour, minute), done_today(app.engine, name, app.clock, tz)))


def register_report_job(app) -> None:
    """Ежедневный отчёт: проверка раз в минуту «пора ли» (TECHNICAL_SPEC.md, 15; ARCHITECTURE.md, 11)."""
    from apscheduler.triggers.cron import CronTrigger

    from vacancy_hunter.reporting.daily import build_daily_report, claim_attempt, report_due

    async def report_job():
        checker, http = actuality_checker(app.config)
        try:
            remaining = None
            if app.gateway is not None:
                remaining = (await app.gateway.budget.snapshot())["remaining"]
            return await build_daily_report(app.engine, app.config, checker=checker, owner_chat_id=app.owner_id,
                                            clock=app.clock, budget_remaining=remaining,
                                            wake=app.outbox.wake if app.outbox else None)
        finally:
            await http.aclose()

    async def tick():
        if await report_due(app.engine, app.config, app.clock) and await claim_attempt(app.engine, app.clock):
            await app.runner.run("report", report_job)

    app.scheduler.add_simple("report_tick", tick, CronTrigger(minute="*", timezone=app.config.schedule.timezone))


def cmd_cleanup(config_dir: Path, env: Mapping[str, str]) -> int:
    from vacancy_hunter.pipeline.cleanup import cleanup

    config = load_app_config(config_dir)

    async def go():
        engine = _async_engine(env)
        try:
            return await cleanup(engine, config)
        finally:
            await engine.dispose()

    stats = asyncio.run(go())
    print("Очистка завершена: " + (", ".join(f"{k} {v}" for k, v in sorted(stats.items())) or "удалять нечего"))
    return 0


def cmd_backup(config_dir: Path, env: Mapping[str, str], restore: Path | None, list_only: bool) -> int:
    from hunter_core.storage.backup import list_backups, restore_backup
    from vacancy_hunter.ops import BACKUP_DIR, run_backup

    config = load_app_config(config_dir)
    if list_only:
        for b in list_backups(BACKUP_DIR):
            print(f"{b.path.name}  {b.path.stat().st_size / 1024 / 1024:.1f} МБ")
        return 0
    if restore is not None:
        # Восстановление в рабочую базу — только по инструкции RUNBOOK.md (раздел «Восстановление»).
        settings = read_app_database(env)
        target = env.get("RESTORE_DB_NAME", "").strip()
        if not target or target == settings.name:
            print("Восстановление поверх рабочей базы этой командой запрещено. Укажите пустую базу "
                  "в RESTORE_DB_NAME (см. RUNBOOK.md).")
            return 2
        from dataclasses import replace

        asyncio.run(restore_backup(replace(settings, name=target), restore))
        print(f"Копия {restore.name} развёрнута в базу {target}.")
        return 0

    async def go():
        engine = _async_engine(env)
        try:
            # Ручная копия — под той же блокировкой, что и плановая: две копии одновременно не пишутся.
            from hunter_core.scheduler import JobRunner

            holder = {}

            async def job():
                holder["result"] = await run_backup(engine, config, read_app_database(env),
                                                    owner_chat_id=read_telegram(env).owner_id)
                return holder["result"]

            outcome = await JobRunner(engine).run("backup", job)
            if outcome != "success":
                raise SystemExit("Копия не создана: " + ("сейчас уже выполняется плановая копия, повторите позже"
                                                         if outcome == "skipped" else "ошибка, см. журнал"))
            return holder["result"]
        finally:
            await engine.dispose()

    result = asyncio.run(go())
    print(f"Копия {result['file']} ({result['bytes'] / 1024 / 1024:.1f} МБ) создана; удалено старых "
          f"{result['removed']}. Файл в Telegram отправит работающее приложение.")
    return 0


def cmd_eval_sample(env: Mapping[str, str], action: str) -> int:
    from vacancy_hunter import evaluation

    async def go(csv_text: str | None):
        engine = _async_engine(env)
        try:
            async with engine.connect() as conn:
                if action == "prepare":
                    return await evaluation.prepare(conn)
                return await evaluation.check(conn, csv_text)
        finally:
            await engine.dispose()

    if action == "prepare":
        csv_text, warnings = asyncio.run(go(None))
        sys.stdout.write(csv_text)
        for w in warnings:
            print("Предупреждение: " + w, file=sys.stderr)
        return 0
    result = asyncio.run(go(sys.stdin.read()))
    if "errors" in result:
        print("Разметка не прочитана:\n  " + "\n  ".join(result["errors"]))
        return 2
    if not result["valid"]:
        print("Сравнение недействительно — выборку нужно обновить или дождаться пересчёта:")
        for line in result["stale"]:
            print("  " + line)
        for r in result["not_scored"]:
            print(f"  запись {r.id}: полной оценки нет")
    print(f"Совпало {result['matched']} из {result['total']} (нужно не меньше {result['need']}) — "
          f"QLT-2 {'выполнен' if result['qlt2_ok'] else 'НЕ выполнен'}")
    print(f"QLT-1 {'выполнен' if result['qlt1_ok'] else 'НЕ выполнен'}: записей «подходит», исключённых "
          f"правилами или фильтрами, {len(result['qlt1_failures'])}")
    diffs = [r for r in result["rows"] if r.system != r.owner]
    if diffs:
        print("\nРасхождения (номер, владелец → система, балл, название):")
        for r in diffs:
            note = " (исключено)" if r.excluded else ""
            print(f"  {r.id}: {r.owner} → {r.system or 'нет оценки'}{note}, {r.total if r.total is not None else '—'}"
                  f", {r.title[:70]}")
    return 0 if result["qlt1_ok"] and result["qlt2_ok"] else 1


def cmd_report(config_dir: Path, env: Mapping[str, str], dry_run: bool) -> int:
    from vacancy_hunter.reporting.daily import build_daily_report

    config = load_app_config(config_dir)
    owner = read_telegram(env).owner_id

    async def go():
        engine = _async_engine(env)
        checker, http = actuality_checker(config)
        try:
            return await build_daily_report(engine, config, checker=checker, owner_chat_id=owner, dry_run=dry_run)
        finally:
            await http.aclose()
            await engine.dispose()

    result = asyncio.run(go())
    if result.get("dry_run"):
        print("Пробный режим: ничего не записано и не отправлено.\n")
        print(result["header"])
        for text in result["cards"]:
            print("\n" + "-" * 40 + "\n" + text)
        for vid, reason in result["closed"]:
            print(f"\nЗакрыта при проверке: вакансия {vid} — {reason}")
        return 0
    if "skipped" in result:
        print({"paused": "Отчёты на паузе.", "exists": "Отчёт за сегодня уже сформирован.",
               "no_owner": "TELEGRAM_OWNER_ID не задан: отчёт не сформирован, отметки о показе не поставлены."}
              [result["skipped"]])
        return 0
    print(f"Отчёт {result['report_id']}: основной блок {result['main']}, по заголовку {result['title_only']}, "
          f"закрыто при проверке {result['closed']}. Сообщения отправит работающее приложение.")
    return 0


def _dump_tasks() -> None:
    """По сигналу SIGUSR1 записать в журнал, где сейчас ждёт каждая задача (диагностика зависаний)."""
    import io

    for task in asyncio.all_tasks():
        buf = io.StringIO()
        task.print_stack(limit=12, file=buf)
        log.warning("task_dump", extra={"task": task.get_name(), "stack": buf.getvalue()[-3000:]})


async def _run_app(config: AppConfig, env: Mapping[str, str]) -> int:
    from hunter_core.notify import TelegramBotApiSender
    from hunter_core.observability.heartbeat import Heartbeat
    from hunter_core.storage.db import create_async_db_engine
    from vacancy_hunter.app import Application

    telegram = read_telegram(env)
    user_agent = config.sources.user_agent
    db_settings = read_app_database(env)
    engine = create_async_db_engine(db_settings, application_name="vacancy_hunter")
    from vacancy_hunter.ops import BACKUP_DIR

    sender = TelegramBotApiSender(telegram.token, user_agent=user_agent, document_dir=BACKUP_DIR) \
        if telegram.token else None
    app = Application(config=config, engine=engine, sender=sender, owner_id=telegram.owner_id,
                      heartbeat=Heartbeat(env.get("HEARTBEAT_URL"), user_agent=user_agent),
                      bot_token=telegram.token)
    app.gateway = None
    if env.get("ANTHROPIC_API_KEY", "").strip():
        from hunter_core.llm import LlmGateway
        from hunter_core.llm.anthropic_provider import AnthropicProvider
        from vacancy_hunter.pipeline.extract import build_budget

        app.gateway = LlmGateway(build_budget(engine, config, clock=app.clock, alert=app.alerts.send),
                                 AnthropicProvider(env["ANTHROPIC_API_KEY"]),
                                 max_concurrency=config.llm.limits.max_concurrency)
        await app.gateway.budget.recover_stale()   # проверка состояния при старте (DATABASE_SCHEMA.md, 13)
    else:
        log.warning("llm_disabled", extra={"reason": "ANTHROPIC_API_KEY не задан: извлечение отключено"})
    app.bot_context.status_extra = _budget_status(engine, config, app.clock)
    register_pipeline_jobs(app)
    register_report_job(app)
    register_maintenance_jobs(app, db_settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    loop.add_signal_handler(signal.SIGUSR1, _dump_tasks)
    try:
        await app.start()
        await stop.wait()
    finally:
        await app.stop()
        if sender is not None:
            await sender.aclose()
        await engine.dispose()
    return 0


def cmd_run(config_dir: Path, env: Mapping[str, str]) -> int:
    config, problems = _check(config_dir, env)
    if problems:
        for line in problems:
            log.error("config_invalid", extra={"problem": line})
        log.error("startup_aborted", extra={"reason": "конфигурация не прошла проверку"})
        return 1
    try:
        _verify_database(env)
    except Exception as exc:  # noqa: BLE001 — любая ошибка базы останавливает запуск
        log.error("startup_aborted", extra={"reason": f"база недоступна или не готова: {exc}"})
        return 1
    return asyncio.run(_run_app(config, env))


async def _check_env_async(env: Mapping[str, str]) -> list[tuple[str, str, bool]]:
    """Проверки внешних зависимостей. Каждая строка: что, итог, пройдено ли (обязательные)."""
    from hunter_core.notify import SendOutcomeUnknown, SendRefused, TelegramBotApiSender
    from hunter_core.observability.heartbeat import Heartbeat

    results: list[tuple[str, str, bool]] = []
    try:
        _verify_database(env)
        results.append(("База данных", "доступна, миграции применены", True))
    except Exception as exc:  # noqa: BLE001
        results.append(("База данных", f"ОШИБКА: {exc}", False))

    telegram = read_telegram(env)
    if telegram.token is None:
        results.append(("Telegram", "токен не задан — бот отключён (нужен TELEGRAM_BOT_TOKEN)", True))
    else:
        sender = TelegramBotApiSender(telegram.token)
        try:
            me = await sender.get_me()
            results.append(("Telegram", f"бот @{me.get('username', '?')} доступен", True))
        except (SendRefused, SendOutcomeUnknown) as exc:
            results.append(("Telegram", f"ОШИБКА: {exc}", False))
        finally:
            await sender.aclose()
    results.append(("Владелец", "идентификатор задан" if telegram.owner_id is not None else
                    "TELEGRAM_OWNER_ID не задан — бот никому не отвечает, идентификатор виден в журнале", True))

    heartbeat = Heartbeat(env.get("HEARTBEAT_URL"), user_agent="AIVacancyHunter")
    if not heartbeat.enabled:
        results.append(("Внешний пульс", "адрес не задан — пульс отключён", True))
    else:
        ok = await heartbeat.beat()
        results.append(("Внешний пульс", "сигнал принят" if ok else "ОШИБКА: сигнал не принят", ok))
    await heartbeat.aclose()
    key = env.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        results.append(("API LLM", "ключ не задан — извлечение отключено (нужен ANTHROPIC_API_KEY)", True))
    else:
        from hunter_core.llm.anthropic_provider import AnthropicProvider

        model = load_app_config().llm.roles.extraction.model
        provider = AnthropicProvider(key)
        try:
            await provider.check_model(model)
            results.append(("API LLM", f"ключ действует, модель {model} доступна", True))
        except Exception as exc:  # noqa: BLE001
            results.append(("API LLM", f"ОШИБКА: {type(exc).__name__}", False))
        finally:
            await provider.aclose()
    return results


def cmd_check_env(env: Mapping[str, str]) -> int:
    try:
        read_telegram(env)
    except SecretsError as exc:
        print(f"Окружение НЕ прошло проверку: {exc}")
        return 1
    results = asyncio.run(_check_env_async(env))
    for name, outcome, _ok in results:
        print(f"{name}: {outcome}")
    return 0 if all(ok for _, _, ok in results) else 1


def _async_engine(env):
    from hunter_core.storage.db import create_async_db_engine

    return create_async_db_engine(read_app_database(env), application_name="vacancy_hunter_cli")


def cmd_probe_sources(config_dir: Path, save: Path | None) -> int:
    from vacancy_hunter.sources.probe import probe_sources

    config = load_app_config(config_dir)
    results = asyncio.run(probe_sources(config.sources, save_dir=save))
    mode_ru = {"active": "активен", "quiet": "тихий", "waiting": "ожидает", "disabled": "отключён"}
    for r in results:
        print(f"{r.code:28} [{mode_ru[r.mode]:8}] {'доступен' if r.ok else 'НЕДОСТУПЕН'}: {r.detail}")
    print(f"Итого: доступно {sum(r.ok for r in results)} из {len(results)}")
    return 0


def cmd_collect(config_dir: Path, env: Mapping[str, str], source: str | None, force: bool) -> int:
    from vacancy_hunter.pipeline.collect import collect

    config = load_app_config(config_dir)

    async def go():
        engine = _async_engine(env)
        try:
            return await collect(engine, config, only=source, force=force)
        finally:
            await engine.dispose()

    stats = asyncio.run(go())
    print(f"Сбор завершён: источников {stats['sources']}, успешно {stats['success']}, сбоев {stats['failed']}, "
          f"запретов {stats['blocked']}, пропущено по интервалу {stats['skipped']}, новых записей {stats['new']}")
    return 0 if stats["failed"] == 0 and stats["blocked"] == 0 else 1


def cmd_process(config_dir: Path, env: Mapping[str, str]) -> int:
    from vacancy_hunter.pipeline.process import process

    config = load_app_config(config_dir)

    async def go():
        engine = _async_engine(env)
        gateway = None
        try:
            if env.get("ANTHROPIC_API_KEY", "").strip():
                from hunter_core.llm import LlmGateway
                from hunter_core.llm.anthropic_provider import AnthropicProvider
                from vacancy_hunter.pipeline.extract import build_budget

                gateway = LlmGateway(build_budget(engine, config), AnthropicProvider(env["ANTHROPIC_API_KEY"]),
                                     max_concurrency=config.llm.limits.max_concurrency)
            return await process(engine, config, gateway=gateway)
        finally:
            if gateway is not None:
                await gateway.provider.aclose()
            await engine.dispose()

    result = asyncio.run(go())
    print("Обработка завершена.")
    for stage in ("dedup_prefilter", "extraction", "scoring"):
        stats = result.get(stage)
        if stats is None:
            print(f"  {stage}: отключено (нет ключа API)" if stage == "extraction" else "")
            continue
        print(f"  {stage}: " + (", ".join(f"{k} {v}" for k, v in sorted(stats.items())) or "нечего обрабатывать"))
    return 0


def cmd_recalc(config_dir: Path, env: Mapping[str, str]) -> int:
    from vacancy_hunter.matching.versions import ensure_versions
    from vacancy_hunter.pipeline.recalc import recalc

    config = load_app_config(config_dir)

    async def go():
        engine = _async_engine(env)
        try:
            versions = await ensure_versions(engine, config)
            return versions, await recalc(engine, config)
        finally:
            await engine.dispose()

    versions, stats = asyncio.run(go())
    print(f"Версия профиля {versions.profile_no}, версия правил {versions.scoring_no}.")
    if stats.get("paused_for_report"):
        print("Пересчёт отложен: до отчёта меньше 15 минут.")
    print(f"Пересчитано {stats.get('recalculated', 0)}, изменили категорию {stats.get('category_changed', 0)}, "
          f"ошибок {stats.get('failed', 0)}, осталось {stats.get('remaining', 0)}.")
    return 0 if not stats.get("failed") else 1


def cmd_show_scores(config_dir: Path, env: Mapping[str, str], limit: int, vacancy_id: int | None) -> int:
    """Карточки оценок для проверки владельцем: лучшие действующие полные оценки или одна вакансия."""
    from sqlalchemy import select

    from vacancy_hunter.matching.explain import card_lines
    from vacancy_hunter.matching.gates import GATE_TITLE
    from vacancy_hunter.storage.tables import scores, vacancies

    load_app_config(config_dir)

    async def go():
        engine = _async_engine(env)
        try:
            async with engine.connect() as conn:
                query = (select(scores, vacancies.c.title, vacancies.c.company_name, vacancies.c.canonical_url,
                                vacancies.c.offer_type, vacancies.c.published_at, vacancies.c.first_seen_at,
                                vacancies.c.salary_from, vacancies.c.salary_to, vacancies.c.salary_currency,
                                vacancies.c.work_format_src, vacancies.c.status.label("vacancy_status"))
                         .join(vacancies, vacancies.c.id == scores.c.vacancy_id)
                         .where(scores.c.state != "outdated"))
                if vacancy_id is not None:
                    query = query.where(scores.c.vacancy_id == vacancy_id)
                else:
                    query = query.where(scores.c.kind == "full").order_by(scores.c.total.desc()).limit(limit)
                return [dict(r) for r in (await conn.execute(query)).mappings().all()]
        finally:
            await engine.dispose()

    rows = asyncio.run(go())
    if not rows:
        print("Оценок нет.")
        return 0
    for row in rows:
        print(f"— вакансия {row['vacancy_id']} ({row['vacancy_status']}, оценка {row['kind']}, {row['state']})")
        for line in card_lines(row, row, row.get("canonical_url")):
            print("  " + line)
        rejected = [f"{GATE_TITLE[n]}: {g['reason']}" for n, g in row["gates"].items() if g["state"] == "no"]
        if rejected:
            print("  Исключено фильтром — " + "; ".join(rejected))
        print()
    return 0


def cmd_prompt_scenarios(config_dir: Path, env: Mapping[str, str], only: list[str] | None, thinking: str | None) -> int:
    from dataclasses import replace

    from hunter_core.llm import LlmGateway
    from hunter_core.llm.anthropic_provider import AnthropicProvider
    from vacancy_hunter.pipeline.extract import build_budget
    from vacancy_hunter.pipeline.scenarios_runner import run_scenarios

    if not env.get("ANTHROPIC_API_KEY", "").strip():
        print("Нужен ANTHROPIC_API_KEY в .env")
        return 1
    config = load_app_config(config_dir)
    if thinking:   # сравнение режимов рассуждений (V4) без правки llm.yaml
        roles = config.llm.roles.model_copy(update={"extraction": config.llm.roles.extraction.model_copy(
            update={"thinking": thinking})})
        config = replace(config, llm=config.llm.model_copy(update={"roles": roles}))

    async def go():
        engine = _async_engine(env)
        provider = AnthropicProvider(env["ANTHROPIC_API_KEY"])
        try:
            gateway = LlmGateway(build_budget(engine, config), provider,
                                 max_concurrency=config.llm.limits.max_concurrency)
            return await run_scenarios(engine, gateway, config, only)
        finally:
            await provider.aclose()
            await engine.dispose()

    r = asyncio.run(go())
    print(f"{'Сценарий':8} | {'Итог':6} | Ожидалось | Получено")
    for code, expect, got, ok in r["rows"]:
        print(f"{code:8} | {'да' if ok else 'НЕТ':6} | {expect} | {got}")
    print(f"Пройдено {r['passed']} из {r['total']}. Вызовов {r['calls']}, расход ${r['cost_usd']:.4f}, "
          f"в среднем ${r['avg_cost_per_call']:.5f} за вызов, {r['avg_output_tokens']} выходных токенов за вызов.")
    print(f"Токены: обычный вход {r['input_tokens']}, запись в кэш {r['cache_write_tokens']}, "
          f"чтение из кэша {r['cache_read_tokens']}.")
    return 0 if r["passed"] == r["total"] else 1


def cmd_unblock_source(env: Mapping[str, str], code: str) -> int:
    from sqlalchemy import update

    from hunter_core.storage.tables import source_state

    async def go():
        engine = _async_engine(env)
        try:
            async with engine.begin() as conn:
                return (await conn.execute(
                    update(source_state).where(source_state.c.code == code)
                    .values(health="ok", consecutive_failures=0, mode_reason="owner", last_error=None)
                )).rowcount
        finally:
            await engine.dispose()

    if asyncio.run(go()) == 0:
        print(f"Источник {code} не найден в базе")
        return 1
    print(f"Запрет снят с {code}. Режим будет взят из sources.yaml при следующем сборе.")
    return 0


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="python -m vacancy_hunter")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR, help="папка с файлами *.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config", help="проверить файлы конфигурации и секреты")
    sub.add_parser("check-env", help="проверить доступ к базе, Telegram и пульсу")
    probe = sub.add_parser("probe-sources", help="пробный запрос к каждому источнику")
    probe.add_argument("--save-samples", type=Path, default=None, help="сохранить ответы в папку")
    col = sub.add_parser("collect", help="разовый сбор")
    col.add_argument("--source", default=None, help="код одного источника")
    col.add_argument("--force", action="store_true", help="не ждать интервала 3 часа")
    sub.add_parser("process", help="разовая обработка накопленных записей")
    ps = sub.add_parser("prompt-scenarios", help="сценарии промпта P1 на реальной модели (платно)")
    ps.add_argument("--only", nargs="*", default=None, help="коды сценариев, например T1 T5")
    ps.add_argument("--thinking", choices=["adaptive", "disabled"], default=None,
                    help="режим рассуждений для сравнения (V4)")
    sub.add_parser("cleanup", help="очистка по срокам хранения")
    ev = sub.add_parser("eval-sample", help="размеченная выборка: prepare — CSV для разметки, check — сравнение")
    ev.add_argument("action", choices=["prepare", "check"])
    bkp = sub.add_parser("backup", help="резервная копия базы")
    bkp.add_argument("--list", action="store_true", help="показать сохранённые копии")
    bkp.add_argument("--restore", type=Path, default=None, help="развернуть копию в базу RESTORE_DB_NAME")
    rep = sub.add_parser("report", help="сформировать сегодняшний отчёт")
    rep.add_argument("--dry-run", action="store_true", help="только показать, ничего не записывать и не отправлять")
    sub.add_parser("recalc", help="пересчёт оценок по текущим версиям профиля и правил (без LLM)")
    shw = sub.add_parser("show-scores", help="показать карточки оценок")
    shw.add_argument("--limit", type=int, default=10)
    shw.add_argument("--vacancy", type=int, default=None, help="номер вакансии")
    unb = sub.add_parser("unblock-source", help="снять запрет с источника")
    unb.add_argument("code")
    sub.add_parser("run", help="запустить приложение")
    args = parser.parse_args(argv)

    # Служебные команды печатают результат для человека: в журнал — только предупреждения и ошибки.
    setup_logging(level="INFO" if args.command == "run" else "WARNING", secrets=secret_values(env))
    if args.command == "check-config":
        return cmd_check_config(args.config_dir, env)
    if args.command == "check-env":
        return cmd_check_env(env)
    if args.command == "probe-sources":
        return cmd_probe_sources(args.config_dir, args.save_samples)
    if args.command == "collect":
        return cmd_collect(args.config_dir, env, args.source, args.force)
    if args.command == "prompt-scenarios":
        return cmd_prompt_scenarios(args.config_dir, env, args.only, args.thinking)
    if args.command == "process":
        return cmd_process(args.config_dir, env)
    if args.command == "eval-sample":
        return cmd_eval_sample(env, args.action)
    if args.command == "cleanup":
        return cmd_cleanup(args.config_dir, env)
    if args.command == "backup":
        return cmd_backup(args.config_dir, env, args.restore, args.list)
    if args.command == "report":
        return cmd_report(args.config_dir, env, args.dry_run)
    if args.command == "recalc":
        return cmd_recalc(args.config_dir, env)
    if args.command == "show-scores":
        return cmd_show_scores(args.config_dir, env, args.limit, args.vacancy)
    if args.command == "unblock-source":
        return cmd_unblock_source(env, args.code)
    return cmd_run(args.config_dir, env)


if __name__ == "__main__":
    sys.exit(main())
