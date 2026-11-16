"""Конфигурация AI Vacancy Hunter: семь файлов config/*.yaml и секреты из окружения."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hunter_core.config import (
    ConfigError,
    DatabaseSettings,
    SecretsError,
    load_config_files,
    missing_env,
    read_database_settings,
)
from vacancy_hunter.config.schemas import (
    CONFIG_SCHEMAS,
    LlmConfig,
    ProfileConfig,
    RetentionConfig,
    ScheduleConfig,
    ScoringConfig,
    SkillsConfig,
    SourcesConfig,
)

DEFAULT_CONFIG_DIR = Path(os.environ.get("HUNTER_CONFIG_DIR", Path(__file__).resolve().parents[3] / "config"))

# Необязательные секреты: check-config только сообщает, заданы ли они.
LATER_SECRETS = {
    "TELEGRAM_BOT_TOKEN": "A2, без него бот и отправка отключены",
    "TELEGRAM_OWNER_ID": "A2, без него бот никому не отвечает",
    "HEARTBEAT_URL": "A2, необязательно",
    "ANTHROPIC_API_KEY": "A5",
}
SECRET_ENV_NAMES = ("DB_APP_PASSWORD", "DB_TEST_PASSWORD", "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "HEARTBEAT_URL")


@dataclass(frozen=True)
class AppConfig:
    profile: ProfileConfig
    skills: SkillsConfig
    scoring: ScoringConfig
    sources: SourcesConfig
    retention: RetentionConfig
    schedule: ScheduleConfig
    llm: LlmConfig


def load_app_config(config_dir: Path = DEFAULT_CONFIG_DIR) -> AppConfig:
    """Загрузить и проверить все семь файлов. При ошибках — ConfigError со списком «файл: поле: причина»."""
    files = load_config_files(Path(config_dir), CONFIG_SCHEMAS)
    config = AppConfig(**{name.removesuffix(".yaml"): model for name, model in files.items()})
    check_profile_against_skills(config.profile, config.skills)
    return config


def refresh_matching_config(config: AppConfig, config_dir: Path = DEFAULT_CONFIG_DIR) -> AppConfig:
    """Перечитать profile.yaml, skills.yaml и scoring.yaml перед расчётом оценок.

    Версии профиля и правил считаются по содержимому файлов (DATABASE_SCHEMA.md, раздел 5):
    работающее приложение не должно откатывать версию, созданную командой `recalc`
    по уже изменённым файлам. При ошибке в файлах остаётся прежняя конфигурация.
    """
    import logging
    from dataclasses import replace

    from hunter_core.config import load_yaml_model

    try:
        profile = load_yaml_model(Path(config_dir) / "profile.yaml", ProfileConfig)
        skills = load_yaml_model(Path(config_dir) / "skills.yaml", SkillsConfig)
        scoring = load_yaml_model(Path(config_dir) / "scoring.yaml", ScoringConfig)
        check_profile_against_skills(profile, skills)
    except ConfigError as exc:
        logging.getLogger("vacancy_hunter.config").warning(
            "matching_config_invalid", extra={"problems": exc.problems[:20]})
        return config
    if (profile, skills, scoring) == (config.profile, config.skills, config.scoring):
        return config
    return replace(config, profile=profile, skills=skills, scoring=scoring)


def check_profile_against_skills(profile: ProfileConfig, skills: SkillsConfig) -> None:
    """Навыки профиля записаны каноническими названиями словаря (TECHNICAL_SPEC.md, раздел 6)."""
    known = {s.canonical for s in skills.skills}
    problems = []
    for group, names in profile.professional.skills_confirmed.items():
        unknown = [n for n in names if n not in known]
        if unknown:
            problems.append(f"profile.yaml: professional.skills_confirmed.{group}: нет в skills.yaml: "
                            f"{', '.join(unknown)}")
    unknown = [n for n in profile.professional.skills_unconfirmed if n not in known]
    if unknown:
        problems.append(f"profile.yaml: professional.skills_unconfirmed: нет в skills.yaml: {', '.join(unknown)}")
    if problems:
        raise ConfigError(problems)


def read_app_database(env: Mapping[str, str] = os.environ) -> DatabaseSettings:
    """Подключение приложения: только пользователь приложения (DB_APP_*)."""
    return read_database_settings(env, user_var="DB_APP_USER", password_var="DB_APP_PASSWORD")


@dataclass(frozen=True)
class TelegramSettings:
    token: str | None
    owner_id: int | None


def read_telegram(env: Mapping[str, str] = os.environ) -> TelegramSettings:
    """Токен бота и идентификатор владельца. Оба необязательны: без токена бот отключён,
    без идентификатора бот никому не отвечает. Неверный идентификатор — ошибка."""
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip() or None
    raw_owner = env.get("TELEGRAM_OWNER_ID", "").strip()
    owner = None
    if raw_owner:
        if not raw_owner.lstrip("-").isdigit():
            raise SecretsError(["TELEGRAM_OWNER_ID (должно быть число — ваш идентификатор в Telegram)"])
        owner = int(raw_owner)
    return TelegramSettings(token=token, owner_id=owner)


def later_secrets_status(env: Mapping[str, str] = os.environ) -> dict[str, bool]:
    """Задан ли каждый секрет следующих шагов. Значения не возвращаются."""
    missing = set(missing_env(env, LATER_SECRETS))
    return {name: name not in missing for name in LATER_SECRETS}


def secret_values(env: Mapping[str, str] = os.environ) -> list[str]:
    """Значения секретов для маскировки в логах."""
    return [env[name] for name in SECRET_ENV_NAMES if env.get(name)]
