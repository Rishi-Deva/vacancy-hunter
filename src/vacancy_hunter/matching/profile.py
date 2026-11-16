"""Профиль и словарь навыков в виде, удобном для расчёта (TECHNICAL_SPEC.md, разделы 5–6).

Навык вакансии приводится к каноническому названию по словарю (регистр, «ё», пробелы и
дефисы, номер версии не важны) и получает одно из состояний:

- have — есть в подтверждённых навыках профиля, покрытие 1;
- related — закрыт смежным навыком профиля, покрытие 0,5 (смежность только явная, в одну сторону);
- unconfirmed — «требует подтверждения» (раздел 5.3), покрытие 0 и отдельная пометка;
- missing — есть в словаре, в профиле нет, покрытие 0;
- unknown — нет в словаре: считается отсутствующим и пишется в журнал.

Навыки группы «вне оценки» (например, английский язык) в стеке не учитываются:
язык проверяет фильтр «Язык», а каждый недостаток учитывается один раз (раздел 11.1).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from vacancy_hunter.config import AppConfig
from vacancy_hunter.config.schemas import MissingItem

log = logging.getLogger("vacancy_hunter.matching")

NOT_SCORED_GROUP = "вне оценки"


def norm_skill(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").casefold().replace("ё", "е")).strip()


def _squash(name: str) -> str:
    return re.sub(r"[\s\-_.]", "", name)


def _without_version(name: str) -> str:
    return re.sub(r"[\s-]*v?\d+(\.\d+)*(\.x)?$", "", name).strip()


@dataclass(frozen=True)
class SkillMatch:
    name: str                 # как в вакансии
    canonical: str | None
    status: str               # have, related, unconfirmed, missing, unknown
    value: float              # покрытие 1 / 0,5 / 0
    via: str | None = None    # навык профиля, которым закрыт смежный

    def as_dict(self) -> dict:
        return {"name": self.name, "canonical": self.canonical, "status": self.status, "via": self.via}


@dataclass
class MatchProfile:
    confirmed: set[str]
    unconfirmed: set[str]
    groups: dict[str, str]                # canonical → группа словаря
    related: dict[str, list[str]]         # требуется canonical → засчитывается при наличии в профиле
    lookup: dict[str, str]                # нормализованное написание → canonical
    covered_task_types: set[str]
    projects: list[tuple[str, set[str]]]
    languages: set[str]
    confirmed_missing: list[MissingItem]
    unknown_params: dict
    location: str
    priority_first: set[str]
    priority_second: set[str]
    education: str
    unknown_seen: set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, config: AppConfig) -> MatchProfile:
        prof = config.profile.professional
        lookup: dict[str, str] = {}
        for skill in config.skills.skills:
            for spelling in (skill.canonical, *skill.synonyms):
                key = norm_skill(spelling)
                lookup.setdefault(key, skill.canonical)
                lookup.setdefault(_squash(key), skill.canonical)
        search = config.profile.search
        return cls(
            confirmed={s for group in prof.skills_confirmed.values() for s in group},
            unconfirmed=set(prof.skills_unconfirmed),
            groups={s.canonical: s.group for s in config.skills.skills},
            related={s.canonical: list(s.related) for s in config.skills.skills},
            lookup=lookup,
            covered_task_types=set(prof.covered_task_types),
            projects=[(p.name, set(p.task_types)) for p in prof.projects],
            languages={lang_code(lang.language) for lang in prof.languages},
            confirmed_missing=list(prof.confirmed_missing),
            unknown_params=prof.unknown_params.model_dump(),
            location=search.current_location,
            priority_first=set(search.employer_priority_first),
            priority_second=set(search.employer_priority_second),
            education=prof.education,
        )

    def canonical(self, name: str) -> str | None:
        key = norm_skill(name)
        for candidate in (key, _squash(key), _without_version(key), _squash(_without_version(key))):
            if candidate and candidate in self.lookup:
                return self.lookup[candidate]
        return None

    def match_skill(self, name: str, related_weight: float) -> SkillMatch:
        canonical = self.canonical(name)
        if canonical is None:
            if name not in self.unknown_seen:
                self.unknown_seen.add(name)
                log.info("unknown_skill", extra={"skill": name})
            return SkillMatch(name, None, "unknown", 0.0)
        if canonical in self.confirmed:
            return SkillMatch(name, canonical, "have", 1.0)
        if canonical in self.unconfirmed:
            return SkillMatch(name, canonical, "unconfirmed", 0.0)
        for rel in self.related.get(canonical, []):
            if rel in self.confirmed:
                return SkillMatch(name, canonical, "related", related_weight, via=rel)
        return SkillMatch(name, canonical, "missing", 0.0)

    def scored_in_stack(self, name: str) -> bool:
        canonical = self.canonical(name)
        return canonical is None or self.groups.get(canonical) != NOT_SCORED_GROUP

    def projects_for(self, task_type: str) -> list[str]:
        return [name for name, types in self.projects if task_type in types]


LANGUAGE_ALIASES = {
    "ru": ("ru", "rus", "русский", "русский язык", "russian"),
    "en": ("en", "eng", "english", "английский", "английский язык", "англ"),
    "de": ("de", "german", "немецкий", "deutsch"),
    "fr": ("fr", "french", "французский"),
    "es": ("es", "spanish", "испанский"),
    "zh": ("zh", "chinese", "китайский"),
    "kk": ("kk", "kazakh", "казахский"),
    "ka": ("ka", "georgian", "грузинский"),
    "uz": ("uz", "uzbek", "узбекский"),
    "tr": ("tr", "turkish", "турецкий"),
}
_ALIAS = {alias: code for code, aliases in LANGUAGE_ALIASES.items() for alias in aliases}


def lang_code(name: str) -> str:
    """Код языка; «russian (native)», «русский язык — родной» тоже распознаются."""
    key = norm_skill(name)
    if key in _ALIAS:
        return _ALIAS[key]
    words = set(re.findall(r"[a-zа-я]+", key))
    for alias, code in _ALIAS.items():
        if len(alias) >= 4 and (alias in words or any(w.startswith(alias) for w in words)):
            return code
    return key
