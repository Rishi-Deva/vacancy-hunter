"""Сопоставление вакансии с профилем: фильтры, скоринг, предварительная оценка, объяснение, версии.

Всё считается кодом по фактам извлечения и файлам конфигурации, LLM не участвует
(TECHNICAL_SPEC.md, разделы 5, 6, 10–13, 19).
"""

from vacancy_hunter.matching.profile import MatchProfile
from vacancy_hunter.matching.scoring import ScoreResult, score_full
from vacancy_hunter.matching.preliminary import score_preliminary

__all__ = ["MatchProfile", "ScoreResult", "score_full", "score_preliminary"]
