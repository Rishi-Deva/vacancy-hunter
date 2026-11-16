"""JOB-1…JOB-4: задачи планировщика, блокировки, аренды, догоняющий запуск (TEST_PLAN.md, раздел 4.2)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import insert, select

from hunter_core.notify import Alerts
from hunter_core.observability.heartbeat import Heartbeat
from hunter_core.scheduler import JobRunner, recover_core_state
from hunter_core.storage.tables import job_runs, outbox
from tests.fakes import FakeClock
from vacancy_hunter.app import Application, DailyJob, report_done_today
from vacancy_hunter.config import load_app_config

pytestmark = pytest.mark.integration


async def job_rows(engine):
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(job_runs).order_by(job_runs.c.id))).mappings()]


# ------------------------------------------------------------------ JOB-1
async def test_job1_second_copy_does_not_start(aengine):
    release = asyncio.Event()
    calls = 0

    async def long_job():
        nonlocal calls
        calls += 1
        await release.wait()
        return {"rows": 1}

    runner = JobRunner(aengine)
    first = asyncio.create_task(runner.run("collect", long_job))
    await asyncio.sleep(0.2)
    assert await runner.run("collect", long_job) == "skipped"
    release.set()
    assert await first == "success"
    assert calls == 1
    rows = await job_rows(aengine)
    assert [r["status"] for r in rows] == ["success"] and rows[0]["stats"] == {"rows": 1}
    # после завершения задача снова запускается
    assert await runner.run("collect", long_job) == "success"


async def test_job_failure_recorded_and_alert_sent_once_per_hour(aengine):
    clock = FakeClock()
    alerts = Alerts(aengine, 42, timezone="Asia/Tbilisi", clock=clock)
    runner = JobRunner(aengine, clock=clock, on_failure=alerts.job_failed)

    async def broken():
        raise RuntimeError("источник недоступен")

    for _ in range(3):
        assert await runner.run("process", broken) == "failed"
    rows = await job_rows(aengine)
    assert [r["status"] for r in rows] == ["failed"] * 3
    assert "источник недоступен" in rows[0]["error_text"]
    async with aengine.connect() as conn:
        alerts_rows = (await conn.execute(select(outbox.c.dedup_key))).scalars().all()
    assert len(alerts_rows) == 1                         # одно оповещение на тип ошибки в час
    clock.advance(hours=1)
    await runner.run("process", broken)
    async with aengine.connect() as conn:
        assert len((await conn.execute(select(outbox.c.id))).all()) == 2


# ------------------------------------------------------------------ JOB-2
async def test_job2_expired_lease_abandoned_at_start_then_runs_again(aengine):
    clock = FakeClock()
    async with aengine.begin() as conn:
        await conn.execute(insert(job_runs).values(job_name="report", status="running",
                                                   started_at=clock() - timedelta(hours=1),
                                                   lease_until=clock() - timedelta(minutes=30)))
    runner = JobRunner(aengine, clock=clock)

    async def job():
        return None

    assert await runner.run("report", job) == "skipped"   # пока блокировка висит
    result = await recover_core_state(aengine, clock=clock)
    assert result["job_runs_abandoned"] == 1
    assert await runner.run("report", job) == "success"
    assert [r["status"] for r in await job_rows(aengine)] == ["abandoned", "success"]


async def test_job2_live_lease_not_touched(aengine):
    clock = FakeClock()
    async with aengine.begin() as conn:
        await conn.execute(insert(job_runs).values(job_name="report", status="running", started_at=clock(),
                                                   lease_until=clock() + timedelta(minutes=10)))
    assert (await recover_core_state(aengine, clock=clock))["job_runs_abandoned"] == 0


# ------------------------------------------------------------------ JOB-3, JOB-4
def make_app(engine, clock, daily_jobs):
    return Application(config=load_app_config(), engine=engine, sender=None, owner_id=None,
                       heartbeat=Heartbeat(None, user_agent="t"), clock=clock, daily_jobs=daily_jobs)


@pytest.mark.parametrize("utc_hour,expected", [(5, True), (11, False)])  # 09:00 и 15:00 в Тбилиси
async def test_job3_job4_report_catchup_after_start(aengine, utc_hour, expected):
    clock = FakeClock(datetime(2026, 11, 13, utc_hour, 0, tzinfo=UTC))
    ran = asyncio.Event()

    async def report_job():
        ran.set()
        return None

    job = DailyJob("report", report_job, time(7, 0),
                   lambda: report_done_today(aengine, clock, "Asia/Tbilisi"))
    app = make_app(aengine, clock, [job])
    started = await app.run_catchups()
    assert ("report" in started) is expected
    if expected:
        await asyncio.wait_for(ran.wait(), 2)
    await app.stop()


async def test_catchup_skipped_when_report_already_exists(aengine):
    clock = FakeClock(datetime(2026, 11, 13, 5, 0, tzinfo=UTC))
    from vacancy_hunter.storage.tables import reports

    async with aengine.begin() as conn:
        await conn.execute(insert(reports).values(kind="daily", report_date=datetime(2026, 11, 13).date()))

    async def report_job():
        return None

    app = make_app(aengine, clock, [DailyJob("report", report_job, time(7, 0),
                                             lambda: report_done_today(aengine, clock, "Asia/Tbilisi"))])
    assert await app.run_catchups() == []
    await app.stop()

