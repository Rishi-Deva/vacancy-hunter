"""CFG-1…CFG-5: загрузка и проверка конфигурации и секретов (TEST_PLAN.md, раздел 4.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hunter_core.config import ConfigError, SecretsError
from vacancy_hunter.__main__ import main
from vacancy_hunter.config import load_app_config, read_app_database

GOOD_ENV = {
    "DB_HOST": "db",
    "DB_PORT": "5432",
    "DB_NAME": "vacancy_hunter",
    "DB_APP_USER": "hunter_app",
    "DB_APP_PASSWORD": "very-secret-app-password-123",
}


def edit_yaml(config_dir: Path, filename: str, change) -> None:
    path = config_dir / filename
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def problems_of(config_dir: Path) -> list[str]:
    with pytest.raises(ConfigError) as exc:
        load_app_config(config_dir)
    return exc.value.problems


# ------------------------------------------------------------------ CFG-1
def test_cfg1_repository_config_loads(config_dir):
    config = load_app_config(config_dir)
    assert config.scoring.weights.stack == 30
    assert config.llm.budget.monthly_limit_usd == 3.0
    assert config.schedule.timezone == "Asia/Tbilisi"
    assert config.llm.roles.extraction.model == "claude-haiku-5-5"


def test_cfg1_check_config_command_succeeds(config_dir, capsys):
    assert main(["--config-dir", str(config_dir), "check-config"], env=GOOD_ENV) == 0
    assert "Конфигурация в порядке" in capsys.readouterr().out


# ------------------------------------------------------------------ CFG-2
def test_cfg2_unknown_field_names_file_and_field(config_dir):
    edit_yaml(config_dir, "retention.yaml", lambda d: d.update(raw_items_dayz=30))
    assert any(p.startswith("retention.yaml: raw_items_dayz:") and "неизвестное поле" in p
               for p in problems_of(config_dir))


def test_cfg2_wrong_type_names_file_and_field(config_dir):
    edit_yaml(config_dir, "scoring.yaml", lambda d: d["weights"].update(stack="тридцать"))
    assert any(p.startswith("scoring.yaml: weights.stack:") for p in problems_of(config_dir))


def test_cfg2_numeric_string_is_not_accepted_as_number(config_dir):
    edit_yaml(config_dir, "schedule.yaml", lambda d: d.update(collect_every_hours="3"))
    assert any(p.startswith("schedule.yaml: collect_every_hours:") for p in problems_of(config_dir))


def test_cfg2_value_out_of_range_names_file_and_field(config_dir):
    edit_yaml(config_dir, "scoring.yaml", lambda d: d["report"].update(max_cards=16))
    assert any(p.startswith("scoring.yaml: report.max_cards:") for p in problems_of(config_dir))


def test_cfg2_nested_list_field_is_named(config_dir):
    edit_yaml(config_dir, "scoring.yaml",
              lambda d: d["level"]["experience_penalties"][0].update(penalty=-1))
    assert any(p.startswith("scoring.yaml: level.experience_penalties.0.penalty:")
               for p in problems_of(config_dir))


def test_cfg2_missing_file_is_reported(config_dir):
    (config_dir / "llm.yaml").unlink()
    assert "llm.yaml: файл не найден" in problems_of(config_dir)


def test_cfg2_broken_yaml_is_reported_with_line(config_dir):
    (config_dir / "schedule.yaml").write_text("timezone: [Asia/Tbilisi\n", encoding="utf-8")
    assert any(p.startswith("schedule.yaml: строка") for p in problems_of(config_dir))


def test_cfg2_errors_from_several_files_reported_together(config_dir):
    edit_yaml(config_dir, "retention.yaml", lambda d: d.update(raw_items_days=0))
    edit_yaml(config_dir, "schedule.yaml", lambda d: d.update(report_time="07:03"))
    problems = problems_of(config_dir)
    assert any(p.startswith("retention.yaml: raw_items_days:") for p in problems)
    assert any(p.startswith("schedule.yaml: report_time:") for p in problems)


def test_cfg2_check_config_command_fails_and_names_field(config_dir, capsys):
    edit_yaml(config_dir, "scoring.yaml", lambda d: d["report"].update(threshold=91))
    assert main(["--config-dir", str(config_dir), "check-config"], env=GOOD_ENV) == 1
    assert "scoring.yaml: report.threshold:" in capsys.readouterr().out


# ------------------------------------------------------------------ CFG-3
@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["adjustments"]["freshness"].update(fresh=4),          # +4 +2 = +6
        lambda d: d["adjustments"]["geo_priority"].update(first=4),       # +2 +4 = +6
        lambda d: d["adjustments"]["freshness"].update(old=-6),           # -6 +0 = -6
        lambda d: d["adjustments"]["geo_priority"].update(other=-4),      # -2 -4 = -6
    ],
)
def test_cfg3_adjustments_beyond_five_rejected(config_dir, change):
    edit_yaml(config_dir, "scoring.yaml", change)
    assert any(p.startswith("scoring.yaml: adjustments:") and "от -5 до +5" in p
               for p in problems_of(config_dir))


def test_cfg3_adjustments_exactly_five_accepted(config_dir):
    edit_yaml(config_dir, "scoring.yaml", lambda d: d["adjustments"]["freshness"].update(fresh=3))  # +3 +2
    load_app_config(config_dir)


# ------------------------------------------------------------------ CFG-4
def test_cfg4_missing_secret_is_named_without_values():
    env = dict(GOOD_ENV)
    del env["DB_APP_PASSWORD"]
    with pytest.raises(SecretsError) as exc:
        read_app_database(env)
    assert exc.value.missing == ["DB_APP_PASSWORD"]
    for value in env.values():
        if len(value) > 4:
            assert value not in str(exc.value)


def test_cfg4_empty_secret_counts_as_missing():
    with pytest.raises(SecretsError) as exc:
        read_app_database({**GOOD_ENV, "DB_APP_PASSWORD": "   "})
    assert exc.value.missing == ["DB_APP_PASSWORD"]


def test_cfg4_app_does_not_start_without_secret(config_dir, capsys):
    env = {k: v for k, v in GOOD_ENV.items() if k != "DB_APP_USER"}
    assert main(["--config-dir", str(config_dir), "run"], env=env) == 1
    out = capsys.readouterr().out
    assert "DB_APP_USER" in out
    assert "startup_aborted" in out
    assert GOOD_ENV["DB_APP_PASSWORD"] not in out


def test_cfg4_check_config_reports_missing_secret_without_value(config_dir, capsys):
    env = {**GOOD_ENV, "DB_NAME": ""}
    assert main(["--config-dir", str(config_dir), "check-config"], env=env) == 1
    out = capsys.readouterr().out
    assert "DB_NAME" in out
    assert GOOD_ENV["DB_APP_PASSWORD"] not in out


def test_cfg4_later_step_secrets_not_required_on_a1(config_dir, capsys):
    # Токен Telegram и ключ Claude на шаге A1 не обязательны.
    assert main(["--config-dir", str(config_dir), "check-config"], env=GOOD_ENV) == 0
    out = capsys.readouterr().out
    assert "TELEGRAM_BOT_TOKEN: не задан" in out
    assert "ANTHROPIC_API_KEY: не задан" in out


# ------------------------------------------------------------------ CFG-5
def test_cfg5_reserves_not_below_monthly_limit_rejected(config_dir):
    edit_yaml(config_dir, "llm.yaml", lambda d: d["budget"].update(letters_reserve_usd=2.5, quiet_limit_usd=0.5))
    assert any(p.startswith("llm.yaml: budget.letters_reserve_usd:") and "quiet_limit_usd" in p
               for p in problems_of(config_dir))


def test_cfg5_warning_above_limit_rejected(config_dir):
    edit_yaml(config_dir, "llm.yaml", lambda d: d["budget"].update(warn2_usd=3.5))
    assert any(p.startswith("llm.yaml: budget.warn2_usd:") for p in problems_of(config_dir))


def test_cfg5_warning_equal_to_limit_rejected(config_dir):
    edit_yaml(config_dir, "llm.yaml", lambda d: d["budget"].update(warn2_usd=3.0))
    assert any("warn2_usd" in p for p in problems_of(config_dir))


def test_cfg5_role_model_without_prices_rejected(config_dir):
    edit_yaml(config_dir, "llm.yaml", lambda d: d["roles"]["letter"].update(model="claude-sonnet-5-5"))
    assert any("roles.letter.model" in p for p in problems_of(config_dir))


# ------------------------------------------------------------------ сквозные проверки: путь к полю
@pytest.mark.parametrize("filename,change,expected_prefix", [
    ("scoring.yaml", lambda d: d["direction"].update(related_points=50), "scoring.yaml: direction.related_points:"),
    ("scoring.yaml", lambda d: d["stack"].update(no_skills_value=31), "scoring.yaml: stack.no_skills_value:"),
    ("scoring.yaml", lambda d: d["tasks"].update(no_tasks_value=26), "scoring.yaml: tasks.no_tasks_value:"),
    ("scoring.yaml", lambda d: d["level"]["experience_penalties"][0].update(penalty=21),
     "scoring.yaml: level.experience_penalties.0.penalty:"),
    ("scoring.yaml", lambda d: d["preliminary"].update(main_entry=70), "scoring.yaml: preliminary.main_entry:"),
    ("scoring.yaml", lambda d: d["clarity"].update(remote=5), "scoring.yaml: clarity:"),
    ("scoring.yaml", lambda d: d["weights"].update(stack=31), "scoring.yaml: weights:"),
    ("llm.yaml", lambda d: d["prices"]["claude-haiku-5-5"].update(cache_write=0.05),
     "llm.yaml: prices.claude-haiku-5-5.cache_write:"),
    ("llm.yaml", lambda d: d["budget"].update(warn1_usd=2.6), "llm.yaml: budget.warn1_usd:"),
    ("schedule.yaml", lambda d: d.update(report_catchup_until="06:00"), "schedule.yaml: report_catchup_until:"),
])
def test_cross_field_problem_names_exact_field(config_dir, filename, change, expected_prefix):
    edit_yaml(config_dir, filename, change)
    problems = problems_of(config_dir)
    assert any(p.startswith(expected_prefix) for p in problems), problems


def test_each_cross_field_problem_is_separate_line(config_dir):
    edit_yaml(config_dir, "llm.yaml", lambda d: d["budget"].update(warn1_usd=2.6, daily_extraction_limit_usd=4.0))
    problems = problems_of(config_dir)
    assert any(p.startswith("llm.yaml: budget.warn1_usd:") for p in problems)
    assert any(p.startswith("llm.yaml: budget.daily_extraction_limit_usd:") for p in problems)
