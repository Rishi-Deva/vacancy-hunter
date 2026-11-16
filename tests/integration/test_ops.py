"""CLN-1…CLN-4, BKP-1, REC-1, LOG-1, ALR-1 — эксплуатация (TEST_PLAN.md, 4.8)."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hunter_core.notify import Alerts
from hunter_core.storage.backup import BackupFile, make_backup, restore_backup, rotate, to_keep
from hunter_core.storage.tables import fetch_runs, job_runs, llm_budget, llm_ledger, outbox, raw_items, source_state
from tests.fakes import FakeClock
from tests.fakes.report import CONFIG, NOW, FakeChecker, add_scored
from vacancy_hunter.pipeline.cleanup import cleanup
from vacancy_hunter.pipeline.normalize import text_hash
from vacancy_hunter.storage.tables import (
    extractions,
    feedback_events,
    report_items,
    reports,
    scores,
    vacancies,
    vacancy_sources,
)

pytestmark = pytest.mark.integration

OWNER = 111
OLD = NOW - timedelta(days=400)


async def add_vacancy(engine, *, status="scored", seen=OLD, text_="Текст вакансии достаточно длинный", user_state="none",
                      purged=False, **over) -> int:
    async with engine.begin() as conn:
        await conn.execute(pg_insert(source_state).values(code="tg_a", kind="telegram", mode="active")
                           .on_conflict_do_nothing())
        values = dict(title="Python-разработчик", status=status, has_description=True,
                      description_text=None if purged else text_, description_hash=text_hash(text_),
                      text_purged_at=seen if purged else None, primary_source_code="tg_a", first_seen_at=seen,
                      last_seen_at=seen, published_at=seen, user_state=user_state, company_norm="ромашка",
                      title_norm="python разработчик", canonical_url=f"https://example.com/{text_hash(text_)[:8]}")
        values.update(over)
        return (await conn.execute(insert(vacancies).values(**values).returning(vacancies.c.id))).scalar_one()


async def count(engine, table, *where) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(table).where(*where))).scalar_one()


async def vac(engine, vid):
    async with engine.connect() as conn:
        return (await conn.execute(select(vacancies).where(vacancies.c.id == vid))).mappings().first()


# ------------------------------------------------------------------ CLN-1…CLN-4
async def test_cln1_cleanup_removes_only_what_is_due(aengine):
    clock = FakeClock(NOW)
    fresh_rejected = await add_vacancy(aengine, status="rejected_rules", seen=NOW - timedelta(days=30), text_="a" * 90)
    old_rejected = await add_vacancy(aengine, status="rejected_rules", seen=NOW - timedelta(days=61), text_="b" * 90)
    old_unshown = await add_vacancy(aengine, status="scored", seen=NOW - timedelta(days=61), text_="c" * 90)
    shown_100 = await add_vacancy(aengine, status="reported", seen=NOW - timedelta(days=100), text_="d" * 90,
                                  reported_at=NOW - timedelta(days=100))
    shown_200 = await add_vacancy(aengine, status="reported", seen=NOW - timedelta(days=200), text_="e" * 90,
                                  reported_at=NOW - timedelta(days=200))
    pending = await add_vacancy(aengine, status="pending_llm", seen=NOW - timedelta(days=90), text_="f" * 90)
    gone = await add_vacancy(aengine, status="rejected_gate", seen=NOW - timedelta(days=400), purged=True, text_="g" * 90)
    async with aengine.begin() as conn:
        for t in ("b" * 90, "f" * 90):
            await conn.execute(insert(extractions).values(
                text_hash=text_hash(t), model="m", prompt_version="p1-1.0", status="ok", parts_total=1,
                parts_done=1, chars_total=90, chars_analyzed=90, facts={}))
        await conn.execute(insert(raw_items).values(source_code="tg_a", external_id="old", payload={},
                                                    fetched_at=NOW - timedelta(days=31)))
        await conn.execute(insert(raw_items).values(source_code="tg_a", external_id="new", payload={},
                                                    fetched_at=NOW - timedelta(days=29)))
        await conn.execute(insert(outbox).values(dedup_key="old", chat_id=1, kind="system", payload={}, state="sent",
                                                 created_at=NOW - timedelta(days=40), sent_at=NOW - timedelta(days=40)))
        await conn.execute(insert(outbox).values(dedup_key="unconfirmed", chat_id=1, kind="system", payload={},
                                                 state="unconfirmed", created_at=NOW - timedelta(days=40)))
        await conn.execute(insert(job_runs).values(job_name="collect", status="success",
                                                   started_at=NOW - timedelta(days=91)))
        await conn.execute(insert(fetch_runs).values(source_code="tg_a", status="success",
                                                     started_at=NOW - timedelta(days=91)))
        await conn.execute(insert(reports).values(kind="daily", report_date=(NOW - timedelta(days=366)).date(),
                                                  status="sent"))
        old_month = datetime(2025, 9, 1).date()
        await conn.execute(insert(llm_budget).values(
            month=old_month, limit_usd=3, letters_reserve_usd=0.3, daily_extraction_limit_usd=0.08,
            quiet_limit_usd=0.5, warn1_usd=1.5, warn2_usd=2.5))
        await conn.execute(insert(llm_ledger).values(month=old_month, purpose="extraction", model="m",
                                                     attempt_no=1, reserved_usd=0, state="settled",
                                                     started_at=datetime(2025, 9, 2, tzinfo=UTC)))
    stats = await cleanup(aengine, CONFIG, clock=clock)
    assert stats == {"raw_items": 1, "texts_rejected": 2, "texts_reported": 1, "extractions": 1, "vacancies": 1,
                     "outbox": 1, "fetch_runs": 1, "job_runs": 1, "reports": 1, "llm_ledger": 1}
    assert (await vac(aengine, fresh_rejected))["description_text"] is not None
    for vid in (old_rejected, old_unshown, shown_200):
        v = await vac(aengine, vid)
        assert v["description_text"] is None and v["text_purged_at"] == NOW and v["description_hash"]
    assert (await vac(aengine, shown_100))["description_text"] is not None
    assert (await vac(aengine, pending))["description_text"] is not None
    assert await vac(aengine, gone) is None
    assert await count(aengine, extractions) == 1                      # извлечение текста в очереди осталось
    assert await count(aengine, outbox) == 1 and await count(aengine, llm_budget) == 1
    assert await cleanup(aengine, CONFIG, clock=clock) == {}            # повтор ничего не удаляет


async def test_cln2_marked_vacancy_older_than_year_kept_with_history(aengine):
    vid = await add_scored(aengine, total=80, first_seen_at=OLD, published_at=OLD)
    async with aengine.begin() as conn:
        await conn.execute(update(vacancies).where(vacancies.c.id == vid).values(
            user_state="applied", description_text=None, text_purged_at=OLD, status="reported"))
        await conn.execute(insert(feedback_events).values(vacancy_id=vid, event="applied", created_at=OLD))
    await cleanup(aengine, CONFIG, clock=FakeClock(NOW))
    assert await vac(aengine, vid) is not None
    assert await count(aengine, scores, scores.c.vacancy_id == vid) == 1
    assert await count(aengine, feedback_events, feedback_events.c.vacancy_id == vid) == 1
    assert await count(aengine, vacancy_sources, vacancy_sources.c.vacancy_id == vid) == 1


async def test_cln3_awaiting_description_not_deleted(aengine):
    vid = await add_vacancy(aengine, status="awaiting_description", has_description=False, text_=".",
                            description_text=None, text_purged_at=OLD)
    await cleanup(aengine, CONFIG, clock=FakeClock(NOW))
    assert await vac(aengine, vid) is not None


async def test_cln4_republication_after_text_purge_is_duplicate(aengine):
    from vacancy_hunter.pipeline.dedup import find_original

    text_ = "Ищем Python-разработчика в команду, удалённо, полный день, опыт от года."
    original = await add_vacancy(aengine, status="rejected_gate", seen=NOW - timedelta(days=61), text_=text_)
    await cleanup(aengine, CONFIG, clock=FakeClock(NOW))
    assert (await vac(aengine, original))["description_text"] is None
    again = {"id": 10**9, "canonical_url": None, "description_hash": text_hash(text_), "company_norm": "ромашка",
             "title_norm": "python разработчик", "first_seen_at": NOW}
    async with aengine.connect() as conn:
        match = await find_original(conn, again, NOW)
    assert match is not None and match.original_id == original


# ------------------------------------------------------------------ BKP-1
async def test_bkp1_backup_and_restore_into_empty_database(aengine, test_settings, test_engine, tmp_path):
    vid = await add_scored(aengine, total=77, title="Вакансия для копии «Ё» & <b>")

    async def snapshot():
        async with aengine.connect() as conn:
            rows = {}
            for table in (vacancies, scores, vacancy_sources, source_state):
                rows[table.name] = [dict(r) for r in (await conn.execute(select(table).order_by(*table.primary_key)))
                                    .mappings()]
            rows["alembic"] = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            return rows

    before = await snapshot()
    backup = await make_backup(test_settings, tmp_path, NOW)
    assert backup.path.exists() and backup.path.stat().st_size > 0
    await aengine.dispose()
    with test_engine.begin() as conn:                    # пустая база: схема удалена целиком
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    await restore_backup(test_settings, backup.path)
    after = await snapshot()
    assert after == before and any(r["id"] == vid for r in after["vacancies"])
    async with aengine.begin() as conn:                  # последовательности продолжают с правильного места
        new_id = (await conn.execute(insert(vacancies).values(
            title="после восстановления", status="new", has_description=False, primary_source_code="habr_career",
            first_seen_at=NOW, last_seen_at=NOW).returning(vacancies.c.id))).scalar_one()
    assert new_id > vid


async def test_backup_failure_leaves_no_file(test_settings, tmp_path):
    from hunter_core.storage.backup import BackupError

    with pytest.raises(BackupError) as err:
        await make_backup(replace(test_settings, password="wrong-password-xyz"), tmp_path, NOW)
    assert "wrong-password-xyz" not in str(err.value)
    assert list(tmp_path.iterdir()) == []


def test_backup_rotation_7_daily_4_weekly(tmp_path):
    start = datetime(2026, 9, 1, 4, 0)
    for day in range(70):
        moment = start + timedelta(days=day)
        (tmp_path / f"backup-{moment:%Y%m%d-%H%M%S}.dump").write_bytes(b"x")
    import os
    import time

    old_partial = tmp_path / "backup-20261101-040000.partial"
    old_partial.write_bytes(b"x")
    os.utime(old_partial, (time.time() - 7200, time.time() - 7200))
    fresh_partial = tmp_path / "backup-20261110-040000.partial"               # копия, которая пишется сейчас
    fresh_partial.write_bytes(b"x")
    rotate(tmp_path, 7, 4)
    assert not old_partial.exists() and fresh_partial.exists()
    fresh_partial.unlink()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert len(names) == 11
    newest = start + timedelta(days=69)
    assert f"backup-{newest:%Y%m%d-%H%M%S}.dump" in names
    weeks = {datetime.strptime(n[7:22], "%Y%m%d-%H%M%S").isocalendar()[:2] for n in names}
    assert len(weeks) >= 5


def test_rotation_keeps_everything_when_few():
    files = [BackupFile(Path(f"/x/{i}"), datetime(2026, 11, 1 + i)) for i in range(3)]
    assert to_keep(files, 7, 4) == {f.path for f in files}


async def test_backup_file_sent_to_owner_via_outbox(aengine, test_settings, tmp_path):
    from hunter_core.notify import OutboxSender
    from tests.fakes import FakeSender
    from vacancy_hunter.ops import run_backup

    result = await run_backup(aengine, CONFIG, test_settings, owner_chat_id=OWNER, directory=tmp_path,
                              clock=FakeClock(NOW))
    async with aengine.connect() as conn:
        row = (await conn.execute(select(outbox).where(outbox.c.kind == "backup"))).mappings().one()
    assert row["payload"]["document_path"].endswith(result["file"]) and "Резервная копия" in row["payload"]["caption"]
    sender = FakeSender()
    await OutboxSender(aengine, sender, clock=FakeClock(NOW)).send_once()
    assert sender.sent and sender.sent[0][1]["document_path"] == row["payload"]["document_path"]


async def test_telegram_send_document_multipart(tmp_path):
    from hunter_core.notify import TelegramBotApiSender

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    path = tmp_path / "backup-20261114-040000.dump"
    path.write_bytes(b"PGDMP-data")
    sender = TelegramBotApiSender("123:abc", transport=httpx.MockTransport(handler), document_dir=tmp_path)
    assert await sender.send(OWNER, {"document_path": str(path), "caption": "копия"}) == 5
    from hunter_core.notify import SendRefused

    with pytest.raises(SendRefused, match="вне каталога"):
        await sender.send(OWNER, {"document_path": "/etc/passwd"})
    body = seen[0].content
    assert seen[0].url.path.endswith("/sendDocument") and b"PGDMP-data" in body and b'name="chat_id"' in body
    await sender.aclose()


# ------------------------------------------------------------------ REC-1
async def test_rec1_crash_during_report_leaves_nothing_then_single_report(aengine, monkeypatch):
    from vacancy_hunter.reporting import daily

    for i in range(3):
        await add_scored(aengine, total=80 + i)
    real = daily.enqueue
    calls = {"n": 0}

    async def crashing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("процесс остановлен")
        return await real(*args, **kwargs)

    monkeypatch.setattr(daily, "enqueue", crashing)
    with pytest.raises(RuntimeError):
        await daily.build_daily_report(aengine, CONFIG, checker=FakeChecker(), owner_chat_id=OWNER,
                                       clock=FakeClock(NOW))
    assert await count(aengine, reports) == 0 and await count(aengine, report_items) == 0
    assert await count(aengine, outbox) == 0
    assert await count(aengine, vacancies, vacancies.c.reported_at.isnot(None)) == 0
    monkeypatch.setattr(daily, "enqueue", real)
    result = await daily.build_daily_report(aengine, CONFIG, checker=FakeChecker(), owner_chat_id=OWNER,
                                            clock=FakeClock(NOW))
    assert result["main"] == 3 and await count(aengine, outbox) == 4


async def test_rec1_restart_recovers_jobs_leases_sending_and_reserves(aengine):
    from vacancy_hunter.recovery import recover_state

    clock = FakeClock(NOW)
    vid = await add_vacancy(aengine, status="pending_llm", seen=NOW, lease_until=NOW - timedelta(minutes=1))
    async with aengine.begin() as conn:
        await conn.execute(insert(job_runs).values(job_name="collect", status="running",
                                                   started_at=NOW - timedelta(hours=1),
                                                   lease_until=NOW - timedelta(minutes=5)))
        await conn.execute(insert(outbox).values(dedup_key="card", chat_id=OWNER, kind="system", payload={},
                                                 state="sending", sending_started_at=NOW - timedelta(minutes=1)))
        await conn.execute(insert(outbox).values(dedup_key="queued", chat_id=OWNER, kind="system", payload={},
                                                 state="pending"))
    await recover_state(aengine, clock=clock)
    async with aengine.connect() as conn:
        states = dict((await conn.execute(select(outbox.c.dedup_key, outbox.c.state))).all())
        job = (await conn.execute(select(job_runs.c.status))).scalar()
    assert states == {"card": "unconfirmed", "queued": "pending"}       # без повторной отправки и без потери
    assert job == "abandoned"
    v = await vac(aengine, vid)
    assert v["lease_until"] is None and v["status"] == "pending_llm"


# ------------------------------------------------------------------ LOG-1
async def test_log1_no_secrets_and_no_vacancy_texts_in_logs(aengine, monkeypatch):
    from hunter_core.llm import LlmGateway
    from hunter_core.observability.logging import setup_logging
    from tests.fakes.llm import TEXT, FakeLlm
    from vacancy_hunter.config import load_app_config
    from vacancy_hunter.pipeline.extract import build_budget
    from vacancy_hunter.pipeline.process import process

    secret = "sk-ant-test-SECRET-value-123456"
    stream = io.StringIO()
    setup_logging(level="DEBUG", secrets=[secret], stream=stream)
    marker_text = TEXT + " Уникальный маркер текста вакансии 7f3a9c."
    await add_vacancy(aengine, status="new", seen=NOW, text_=marker_text, title="Python-разработчик",
                      description_text=marker_text)
    config = load_app_config()
    clock = FakeClock(NOW)
    gateway = LlmGateway(build_budget(aengine, config, clock=clock), FakeLlm())
    import logging

    logging.getLogger("test").warning("probe", extra={"api_key": secret, "nested": {"token": secret}})
    try:
        raise RuntimeError(f"ошибка провайдера с ключом {secret}")
    except RuntimeError:
        logging.getLogger("test").exception("provider_failed")
    await process(aengine, config, clock=clock, gateway=gateway)
    output = stream.getvalue()
    assert output and "probe" in output and "provider_failed" in output
    assert secret not in output
    assert "7f3a9c" not in output and "Уникальный маркер" not in output
    for line in output.splitlines():
        json.loads(line)                                                 # каждая строка — JSON


# ------------------------------------------------------------------ ALR-1
async def test_alr1_ten_same_errors_in_hour_one_alert(aengine):
    alerts = Alerts(aengine, OWNER, timezone="Asia/Tbilisi", clock=FakeClock(NOW))
    sent = [await alerts.send("source_failed:tg_a", f"ошибка {i}") for i in range(10)]
    assert sent.count(True) == 1
    assert await count(aengine, outbox, outbox.c.kind == "alert") == 1
    later = Alerts(aengine, OWNER, timezone="Asia/Tbilisi", clock=FakeClock(NOW + timedelta(hours=1)))
    assert await later.send("source_failed:tg_a", "снова") is True                  # следующий час — новое


def test_rotation_by_dates_not_by_files(tmp_path):
    """Семь ручных копий за один день не вытесняют копии прошлых дней."""
    for day in range(10):
        (tmp_path / f"backup-202611{day + 1:02d}-040000.dump").write_bytes(b"x")
    for hour in range(10, 17):
        (tmp_path / f"backup-20261110-{hour}0000.dump").write_bytes(b"x")
    rotate(tmp_path, 7, 4)
    names = sorted(p.name for p in tmp_path.iterdir())
    days = sorted({n[7:15] for n in names})
    assert days[-7:] == [f"202611{d:02d}" for d in range(4, 11)]
    assert [n for n in names if n.startswith("backup-20261110")] == ["backup-20261110-160000.dump"]


async def test_recalc_does_not_touch_scores_of_purged_texts(aengine):
    from vacancy_hunter.matching.versions import ensure_versions
    from vacancy_hunter.pipeline.recalc import recalc
    from tests.fakes.scoring import scoring_config

    vid = await add_scored(aengine, total=80)
    async with aengine.begin() as conn:
        await conn.execute(update(scores).where(scores.c.vacancy_id == vid).values(state="pending_recalc"))
        await conn.execute(update(vacancies).where(vacancies.c.id == vid).values(description_text=None,
                                                                                 text_purged_at=NOW))
    rules = scoring_config(categories__excellent=86)
    await ensure_versions(aengine, rules)
    result = await recalc(aengine, rules, clock=FakeClock(NOW), sleep=lambda s: __import__("asyncio").sleep(0))
    assert result.get("skipped_no_text") == 1 and not result.get("recalculated")
    async with aengine.connect() as conn:
        rows = (await conn.execute(select(scores.c.state, scores.c.total).where(scores.c.vacancy_id == vid))).all()
    assert rows == [("current", 80)]
    status = (await vac(aengine, vid))["status"]
    assert status == "scored"


async def test_cleanup_deletes_old_titles_without_text_and_keeps_title_only_shown_text(aengine):
    clock = FakeClock(NOW)
    headline = await add_vacancy(aengine, status="scored", seen=OLD, has_description=False, description_text=None,
                                 text_="заголовок hh")
    shown_by_title = await add_vacancy(aengine, status="scored", seen=NOW - timedelta(days=90), text_="h" * 90,
                                       title_only_reported_at=NOW - timedelta(days=90))
    await cleanup(aengine, CONFIG, clock=clock)
    assert await vac(aengine, headline) is None
    assert (await vac(aengine, shown_by_title))["description_text"] is not None
