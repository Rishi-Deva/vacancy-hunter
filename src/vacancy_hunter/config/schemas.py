"""Схемы семи файлов конфигурации (TECHNICAL_SPEC.md, раздел 4).

Значения по умолчанию лежат в самих файлах config/*.yaml. Схема только
проверяет: состав полей, типы, диапазоны и согласованность значений.
Наполнение профиля и словаря навыков — шаг A6, перечня источников — шаг A3.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from hunter_core.config import StrictModel, check_fields

# Архитектурный предел суммы поправок (ARCHITECTURE.md, раздел 9.2): ±5 баллов.
ADJUSTMENTS_LIMIT = 5
# Архитектурный максимум карточек основного блока (ARCHITECTURE.md, раздел 10).
MAX_CARDS_LIMIT = 15

PosInt = Annotated[int, Field(ge=1)]
NonNegInt = Annotated[int, Field(ge=0)]
Points = Annotated[int, Field(ge=0, le=100)]
Share = Annotated[float, Field(ge=0, le=1)]
Usd = Annotated[float, Field(ge=0)]
Text = Annotated[str, Field(min_length=1)]
CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]

DIRECTION_CODES = [
    "python_backend", "api_fastapi", "telegram_bots", "ai_llm", "integrations", "automation_dev", "qa",
    "scraping", "support_tech", "implementation", "other_tech", "other",
]
TASK_TYPES = [
    "telegram_bots", "backend_api", "api_integration", "gov_api_integration", "llm_integration", "rag",
    "scraping", "internal_api_research", "documents_processing", "database_sql", "auth", "payments",
    "crm_features", "schedulers", "manual_testing", "api_testing", "python_autotests", "deploy_linux",
    "monitoring", "computer_vision", "tech_docs", "frontend", "mobile", "ml_training", "ui_autotests",
    "load_testing", "cloud_orchestration", "bi_analytics", "people_management", "sales_clients",
    "user_support", "network_admin", "other",
]


def _unique(values: list[str], what: str) -> list[str]:
    seen, dupes = set(), []
    for v in values:
        key = v.casefold()
        if key in seen:
            dupes.append(v)
        seen.add(key)
    if dupes:
        raise ValueError(f"повторяются {what}: {', '.join(dupes)}")
    return values


# ======================================================================= profile.yaml
class Language(StrictModel):
    language: Text
    level: Literal["native", "fluent", "spoken", "written", "reading"]


class Project(StrictModel):
    name: Text
    description: str = ""
    task_types: list[Literal[tuple(TASK_TYPES)]]  # type: ignore[valid-type]


class Contacts(StrictModel):
    telegram: str
    email: str
    phone: str
    github: str


class UnknownParams(StrictModel):
    documents: Literal["unknown"] | list[str]
    foreign_phones: Literal["unknown"] | list[str]
    employment_forms: Literal["unknown"] | list[str]


class MissingItem(StrictModel):
    """Подтверждённо отсутствующее (раздел 5.6): к чему относится и как узнать в требовании вакансии."""

    text: Text
    applies_to: Literal["language", "legal", "requirement"]
    kinds: list[str] = []          # виды legal.kind или other_requirements.kind; пусто — любой вид
    keywords: list[Text] = []      # слова в тексте требования; пусто — любое требование этого вида


class Professional(StrictModel):
    positioning: Text
    goal: Text
    languages: list[Language] = Field(min_length=1)
    education: str
    experience: str
    skills_confirmed: dict[str, list[Text]]
    skills_unconfirmed: list[Text]
    projects: list[Project]
    covered_task_types: list[Literal[tuple(TASK_TYPES)]]  # type: ignore[valid-type]
    confirmed_missing: list[MissingItem]
    unknown_params: UnknownParams
    contacts: Contacts

    @model_validator(mode="after")
    def _skills_not_both(self) -> Professional:
        confirmed = {s.casefold() for group in self.skills_confirmed.values() for s in group}
        both = [s for s in self.skills_unconfirmed if s.casefold() in confirmed]
        if both:
            check_fields([("skills_unconfirmed", "навык не может быть одновременно подтверждённым и требующим "
                           f"подтверждения: {', '.join(both)}")])
        return self


class SearchParams(StrictModel):
    current_location: CountryCode
    preferred_format: Literal["remote"]
    employer_priority_first: list[CountryCode]
    employer_priority_second: list[CountryCode]
    working_language: Text


class ProfileConfig(StrictModel):
    source_note: Text
    professional: Professional
    search: SearchParams


# ======================================================================= skills.yaml
class Skill(StrictModel):
    canonical: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9 .+#/_-]*$")]
    synonyms: list[Text] = []
    group: Text
    related: list[str] = []  # «требуется этот навык → засчитывается при наличии в профиле related»


class SkillsConfig(StrictModel):
    skills: list[Skill]

    @model_validator(mode="after")
    def _consistent(self) -> SkillsConfig:
        problems = []
        names = [s.canonical for s in self.skills]
        try:
            _unique(names, "канонические названия навыков")
        except ValueError as exc:
            problems.append(("skills", str(exc)))
        known = set(names)
        for i, skill in enumerate(self.skills):
            unknown = [r for r in skill.related if r not in known]
            if unknown:
                problems.append((f"skills.{i}.related", f"навыков нет в словаре: {', '.join(unknown)}"))
        check_fields(problems)
        return self


# ======================================================================= scoring.yaml
class Weights(StrictModel):
    stack: Points
    tasks: Points
    level: Points
    direction: Points
    clarity: Points

    @model_validator(mode="after")
    def _sum_100(self) -> Weights:
        total = self.stack + self.tasks + self.level + self.direction + self.clarity
        if total != 100:
            check_fields([("", f"сумма максимумов критериев должна быть 100, сейчас {total}")])
        return self


class StackRules(StrictModel):
    required_share: Share
    optional_share: Share
    related_weight: Share
    no_skills_value: NonNegInt
    only_optional_base: NonNegInt

    @model_validator(mode="after")
    def _shares(self) -> StackRules:
        if abs(self.required_share + self.optional_share - 1) > 1e-9:
            check_fields([("optional_share", "сумма required_share и optional_share должна быть 1")])
        return self


class TaskRules(StrictModel):
    core_weight: PosInt
    secondary_weight: PosInt
    no_tasks_value: NonNegInt


class ExperiencePenalty(StrictModel):
    min_years: NonNegInt
    max_years: PosInt
    penalty: NonNegInt

    @model_validator(mode="after")
    def _range(self) -> ExperiencePenalty:
        if self.min_years >= self.max_years:
            check_fields([("min_years", "должно быть меньше max_years")])
        return self


class LevelRules(StrictModel):
    experience_penalties: list[ExperiencePenalty]
    senior_confirmed_penalty: NonNegInt
    unmet_requirement_penalty: NonNegInt
    unmet_requirement_cap: NonNegInt


class DirectionRules(StrictModel):
    main_points: NonNegInt
    related_points: NonNegInt
    other_points: NonNegInt
    main_codes: list[Literal[tuple(DIRECTION_CODES)]]  # type: ignore[valid-type]
    related_codes: list[Literal[tuple(DIRECTION_CODES)]]  # type: ignore[valid-type]

    @model_validator(mode="after")
    def _disjoint(self) -> DirectionRules:
        both = set(self.main_codes) & set(self.related_codes)
        if both:
            check_fields([("related_codes", f"направление не может быть и основным, и смежным: "
                                            f"{', '.join(sorted(both))}")])
        return self


class ClarityRules(StrictModel):
    remote: NonNegInt
    geo: NonNegInt
    legal: NonNegInt
    language: NonNegInt


class Freshness(StrictModel):
    fresh_max_days: NonNegInt
    normal_max_days: NonNegInt
    fresh: int
    normal: int
    old: int


class GeoPriority(StrictModel):
    first: int
    second: int
    other: int


class Adjustments(StrictModel):
    freshness: Freshness
    geo_priority: GeoPriority


class Categories(StrictModel):
    excellent: Points
    good: Points
    consider: Points

    @model_validator(mode="after")
    def _order(self) -> Categories:
        if not self.excellent > self.good > self.consider:
            check_fields([("", "границы категорий должны убывать: excellent > good > consider")])
        return self


class Gates(StrictModel):
    exclude_required_years: PosInt
    geo_allow_words: list[Text]
    actual_seen_days: PosInt


class Prefilter(StrictModel):
    max_age_days: PosInt
    min_text_chars: NonNegInt
    incompatible_roles: list[Text]
    profile_title_words: list[Text]       # слово профиля в названии снимает incompatible_role
    not_vacancy_markers: list[Text]       # хештег или начало заголовка: «резюме», «ищу работу»


class Preliminary(StrictModel):
    main_entry: Points
    main: Points
    related: Points
    other: Points
    cap: Annotated[int, Field(ge=0, le=69)]
    entry_markers: list[Text]
    main_keywords: list[Text]
    related_keywords: list[Text]

    @model_validator(mode="after")
    def _under_cap(self) -> Preliminary:
        check_fields([
            (name, f"значение выше потолка cap ({self.cap})")
            for name in ("main_entry", "main", "related", "other")
            if getattr(self, name) > self.cap
        ])
        return self


class ReportDefaults(StrictModel):
    threshold: Annotated[int, Field(ge=50, le=90)]
    max_cards: Annotated[int, Field(ge=1, le=MAX_CARDS_LIMIT)]
    max_per_company: PosInt
    first_show_days: PosInt
    title_only_max: PosInt


class ScoringConfig(StrictModel):
    weights: Weights
    stack: StackRules
    tasks: TaskRules
    level: LevelRules
    direction: DirectionRules
    clarity: ClarityRules
    adjustments: Adjustments
    categories: Categories
    gates: Gates
    prefilter: Prefilter
    seniority_words: list[Text]
    seniority_exclusions: list[Text]      # сочетания, где слово уровня не относится к роли
    preliminary: Preliminary
    report: ReportDefaults

    @model_validator(mode="after")
    def _cross_checks(self) -> ScoringConfig:
        p: list[tuple[str, str]] = []
        w = self.weights
        clarity_sum = self.clarity.remote + self.clarity.geo + self.clarity.legal + self.clarity.language
        if clarity_sum != w.clarity:
            p.append(("clarity", f"сумма баллов ({clarity_sum}) должна равняться weights.clarity ({w.clarity})"))
        for name in ("main_points", "related_points", "other_points"):
            if getattr(self.direction, name) > w.direction:
                p.append((f"direction.{name}", f"больше максимума weights.direction ({w.direction})"))
        for name in ("no_skills_value", "only_optional_base"):
            if getattr(self.stack, name) > w.stack:
                p.append((f"stack.{name}", f"больше максимума weights.stack ({w.stack})"))
        if self.tasks.no_tasks_value > w.tasks:
            p.append(("tasks.no_tasks_value", f"больше максимума weights.tasks ({w.tasks})"))
        for i, rule in enumerate(self.level.experience_penalties):
            if rule.penalty > w.level:
                p.append((f"level.experience_penalties.{i}.penalty", f"больше максимума weights.level ({w.level})"))
            if rule.max_years > self.gates.exclude_required_years:
                p.append((f"level.experience_penalties.{i}.max_years",
                          "выше порога исключения gates.exclude_required_years"))
        for name in ("senior_confirmed_penalty", "unmet_requirement_cap"):
            if getattr(self.level, name) > w.level:
                p.append((f"level.{name}", f"больше максимума weights.level ({w.level})"))
        if self.level.unmet_requirement_penalty > self.level.unmet_requirement_cap:
            p.append(("level.unmet_requirement_penalty", "больше предела unmet_requirement_cap"))
        # Сумма поправок не выходит за ±5 ни при каком сочетании (TECHNICAL_SPEC.md, 11.7).
        fr = self.adjustments.freshness
        geo = self.adjustments.geo_priority
        fresh_values = (fr.fresh, fr.normal, fr.old)
        geo_values = (geo.first, geo.second, geo.other)
        hi = max(fresh_values) + max(geo_values)
        lo = min(fresh_values) + min(geo_values)
        if hi > ADJUSTMENTS_LIMIT or lo < -ADJUSTMENTS_LIMIT:
            p.append(("adjustments", f"сумма поправок может составить от {lo} до {hi}, "
                                     f"допустимо от -{ADJUSTMENTS_LIMIT} до +{ADJUSTMENTS_LIMIT}"))
        if fr.fresh_max_days > fr.normal_max_days:
            p.append(("adjustments.freshness.fresh_max_days", "больше normal_max_days"))
        if self.report.threshold < self.categories.consider:
            p.append(("report.threshold", "порог показа ниже границы категории consider"))
        if self.preliminary.cap >= self.categories.good:
            p.append(("preliminary.cap", "потолок предварительной оценки не ниже категории good"))
        check_fields(p)
        return self


# ======================================================================= sources.yaml
class SourceEntry(StrictModel):
    code: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    kind: Literal["telegram", "rss", "api"]
    adapter: Literal["telegram_preview", "hh_rss", "habr_career_rss"]
    mode: Literal["active", "quiet", "waiting", "disabled"]
    note: str = ""
    channel: str = ""
    url: str = ""
    queries: list[Text] = []

    @model_validator(mode="after")
    def _adapter_fields(self) -> SourceEntry:
        problems = []
        if self.adapter == "telegram_preview":
            if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", self.channel):
                problems.append(("channel", "нужно имя канала Telegram (латиница, цифры, подчёркивание)"))
            if self.kind != "telegram":
                problems.append(("kind", "для telegram_preview тип должен быть telegram"))
        else:
            if not self.url.startswith("https://"):
                problems.append(("url", "нужен адрес https://"))
            if "{query}" in self.url and not self.queries:
                problems.append(("queries", "в адресе есть {query}, но запросы не заданы"))
        check_fields(problems)
        return self


class SourceDefaults(StrictModel):
    interval_hours: Annotated[int, Field(ge=3)]
    degraded_interval_hours: Annotated[int, Field(ge=3)]
    domain_pause_seconds: Annotated[float, Field(ge=0)]
    max_age_days: PosInt
    failures_to_degrade: PosInt
    empty_runs_to_alert: PosInt


class SourcesConfig(StrictModel):
    user_agent: Text
    defaults: SourceDefaults
    sources: list[SourceEntry]

    @model_validator(mode="after")
    def _unique_codes(self) -> SourcesConfig:
        try:
            _unique([s.code for s in self.sources], "коды источников")
        except ValueError as exc:
            check_fields([("sources", str(exc))])
        return self


# ======================================================================= retention.yaml
class RetentionConfig(StrictModel):
    raw_items_days: PosInt
    rejected_text_days: PosInt
    reported_text_days: PosInt
    dedup_keys_days: PosInt
    runs_days: PosInt
    outbox_days: PosInt
    reports_days: PosInt
    ledger_months: PosInt
    backups_daily: PosInt
    backups_weekly: NonNegInt
    cleanup_batch_rows: PosInt
    log_file_mb: PosInt
    log_files: PosInt


# ======================================================================= schedule.yaml
class ScheduleConfig(StrictModel):
    timezone: Text
    collect_every_hours: Annotated[int, Field(ge=3)]
    process_every_minutes: PosInt
    report_time: HHMM
    report_catchup_until: HHMM
    outbox_every_seconds: PosInt
    cleanup_time: HHMM
    backup_time: HHMM
    heartbeat_every_minutes: PosInt
    lease_minutes: PosInt
    recalc_every_minutes: PosInt
    recalc_pause_before_report_minutes: NonNegInt

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"неизвестный часовой пояс «{value}»") from None
        return value

    @field_validator("report_time")
    @classmethod
    def _step_5(cls, value: str) -> str:
        if int(value[3:]) % 5:
            raise ValueError("время отчёта задаётся с шагом 5 минут")  # путь поля pydantic добавит сам
        return value

    @model_validator(mode="after")
    def _catchup(self) -> ScheduleConfig:
        if self.report_catchup_until <= self.report_time:
            check_fields([("report_catchup_until", "должно быть позже report_time")])
        return self


# ======================================================================= llm.yaml
class Role(StrictModel):
    model: Annotated[str, Field(pattern=r"^[a-z0-9.-]+$")]
    prompt_version: Annotated[str, Field(pattern=r"^p\d+-\d+\.\d+$")]
    max_tokens: Annotated[int, Field(ge=1, le=64000)]
    effort: Literal["low", "medium", "high"] | None
    thinking: Literal["adaptive", "disabled"] = "adaptive"   # adaptive — параметр не передаётся (V4)
    max_input_chars: PosInt


class Roles(StrictModel):
    extraction: Role
    letter: Role
    answer: Role


class Prices(StrictModel):
    """Цены за миллион токенов, долларов США."""

    input: Usd
    cache_write: Usd
    cache_read: Usd
    output: Usd
    checked_at: date

    @model_validator(mode="after")
    def _cache_write_is_max_input(self) -> Prices:
        # Верхняя граница резерва считает вход по цене записи в кэш (TECHNICAL_SPEC.md, 21.3),
        # поэтому она должна быть не ниже обычной цены входа и цены чтения из кэша.
        check_fields([
            ("cache_write", f"цена записи в кэш ниже цены «{name}»: резерв бюджета окажется заниженным")
            for name in ("input", "cache_read")
            if self.cache_write < getattr(self, name)
        ])
        return self


class Budget(StrictModel):
    monthly_limit_usd: Usd
    letters_reserve_usd: Usd
    daily_extraction_limit_usd: Usd
    quiet_limit_usd: Usd
    warn1_usd: Usd
    warn2_usd: Usd


class Limits(StrictModel):
    max_attempts: Annotated[int, Field(ge=1, le=3)]
    max_parts: Annotated[int, Field(ge=1, le=4)]
    part_overlap_chars: NonNegInt
    max_concurrency: Annotated[int, Field(ge=1, le=4)]
    quiet_posts_per_channel: PosInt
    search_call_limit: PosInt


class Observations(StrictModel):
    """Замеры на реальной модели (AGENT_PROMPTS.md, раздел 12, пункты V3–V6)."""

    measured_at: date | None = None
    avg_cost_per_record_usd: Usd | None = None
    avg_output_tokens_p1: NonNegInt | None = None
    system_cached: bool | None = None
    note: str = ""


class LlmConfig(StrictModel):
    roles: Roles
    prices: dict[str, Prices]
    budget: Budget
    limits: Limits
    observations: Observations = Observations()

    @model_validator(mode="after")
    def _cross_checks(self) -> LlmConfig:
        p: list[tuple[str, str]] = []
        b = self.budget
        for name in ("extraction", "letter", "answer"):
            model = getattr(self.roles, name).model
            if model not in self.prices:
                p.append((f"roles.{name}.model", f"нет цен для модели «{model}» в разделе prices"))
        if not b.letters_reserve_usd + b.quiet_limit_usd < b.monthly_limit_usd:
            p.append(("budget.letters_reserve_usd",
                      "сумма letters_reserve_usd и quiet_limit_usd должна быть меньше monthly_limit_usd"))
        if not b.warn1_usd < b.warn2_usd:
            p.append(("budget.warn1_usd", "первый порог предупреждения должен быть меньше второго"))
        if not b.warn2_usd < b.monthly_limit_usd:
            p.append(("budget.warn2_usd", "порог предупреждения должен быть меньше monthly_limit_usd"))
        if b.daily_extraction_limit_usd > b.monthly_limit_usd:
            p.append(("budget.daily_extraction_limit_usd", "больше месячного лимита"))
        check_fields(p)
        return self


CONFIG_SCHEMAS = {
    "profile.yaml": ProfileConfig,
    "skills.yaml": SkillsConfig,
    "scoring.yaml": ScoringConfig,
    "sources.yaml": SourcesConfig,
    "retention.yaml": RetentionConfig,
    "schedule.yaml": ScheduleConfig,
    "llm.yaml": LlmConfig,
}

