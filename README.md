# AI Vacancy Hunter

**In English.** A single-user service that collects IT job postings from public Telegram channels (t.me/s previews), hh RSS search feeds and Habr Career.
It normalizes and deduplicates them (link, text, company + similar title via pg_trgm) and drops obvious mismatches with rules before any LLM call.
Claude Haiku extracts structured facts (stack, experience, format, geography) as validated JSON under a monthly budget; a rule-based scorer rates each posting 0–100 against a YAML candidate profile.
A daily report goes to the owner's Telegram bot (aiogram 3) through a database outbox; cleanup, pg_dump backups and source health tracking run on a schedule.
Python 3.12, PostgreSQL 16, SQLAlchemy Core, Alembic, APScheduler, Docker Compose, 472 pytest tests (unit + integration on a real Postgres); cover letters are not implemented yet.

Персональный сервис подбора IT-вакансий для одного пользователя. Он собирает вакансии из публичных источников, отбрасывает дубли и явно неподходящие, с помощью LLM извлекает из текста факты (стек, стаж, формат, география), считает оценку соответствия профилю кандидата и раз в день присылает отчёт в Telegram.

> Профиль в `config/profile.yaml` — **вымышленный** кандидат (Алексей Демин, контакты на `example.com`). Для своего поиска профиль нужно заменить своим.

Подробное описание: `docs/ARCHITECTURE.md` (с него стоит начинать), `docs/TECHNICAL_SPEC.md`, `docs/DATABASE_SCHEMA.md`, `docs/AGENT_PROMPTS.md`, `docs/TEST_PLAN.md`. Это проектные спецификации всей системы; из них реализован этап A (см. «Что сделано и что нет»).

Спецификации и код написаны с помощью Claude Code; правила для агента лежат в `CLAUDE.md`.

## Как устроено

```
источники (каждые 3 ч)                       обработка (каждые 30 мин)
┌──────────────────────────┐   ┌───────────────────────────────────────────────────────────┐
│ Telegram: публичные t.me/s│   │ нормализация → дедупликация (ссылка / текст / компания+    │
│ hh: RSS поиска            │──►│ название, pg_trgm) → правила до LLM → извлечение фактов    │
│ Хабр Карьера              │   │ (Claude Haiku, JSON + проверка) → фильтры → оценка 0–100   │
└──────────────────────────┘   └───────────────────────────────────────────────────────────┘
        │ состояние источников                          │
        ▼                                               ▼
   PostgreSQL 16 ◄──────────────────────────────────────┘
        │
        ▼  ежедневно 07:00 (+ догоняющий запуск после перезапуска)
   отчёт: заголовок + карточки ──► outbox (повторы при сбоях) ──► Telegram-бот владельца
```

- **Сбор.** Подключены адаптеры `telegram_preview`, `hh_rss` и `habr_career` (`src/vacancy_hunter/sources/`); есть и универсальный адаптер `rss`, но в `config/sources.yaml` он пока ни к одному источнику не подключён. Только официальные RSS и публичные страницы, без обхода защит. После 5 сбоев подряд источник помечается «деградировавшим», опрашивается раз в 12 часов, владелец получает одно оповещение.
- **Дедупликация** в три уровня: ссылка, текст, компания и похожее название (`pg_trgm`).
- **Правила до LLM** отсекают явно неподходящее, чтобы не тратить токены.
- **Извлечение фактов**: Claude Haiku, строгий JSON с проверкой. Бюджет задаётся в `config/llm.yaml`: $3 в месяц, дневной лимит, предупреждения.
- **Оценка**: пять критериев с весами из `config/scoring.yaml` (стек 30, задачи 25, уровень 20, направление 15, ясность 10) плюс фильтры (стаж, география, документы). При правке профиля или правил оценки пересчитываются без повторных вызовов LLM.
- **Отчёт и бот**: aiogram 3, отвечает только владельцу (`TELEGRAM_OWNER_ID`). Команды `/start`, `/status`, `/today`, `/settings`, `/stats`. Сообщения уходят через таблицу `outbox`, поэтому при сбое Telegram ничего не теряется.
- **Обслуживание**: очистка по срокам хранения, резервная копия `pg_dump` в 04:00, внешний пульс (необязателен).

## Стек

Python 3.12, PostgreSQL 16, SQLAlchemy 2 (Core), Alembic, psycopg 3, Pydantic 2, APScheduler, aiogram 3, httpx, Anthropic SDK, Docker Compose, pytest.

В базе три роли: администратор (только первичная настройка), пользователь миграций (владелец схемы) и пользователь приложения (только чтение и запись строк). Тестовая база отдельная, со своим пользователем.

## Что сделано и что нет

Готово: сбор, дедупликация, правила, извлечение фактов, оценка и пересчёт, ежедневный отчёт, бот с настройками и статистикой, outbox, очистка, резервные копии.

**Не реализовано** (этап B): сопроводительные письма и ответы на вопросы работодателя. Промпты для них описаны в `docs/AGENT_PROMPTS.md`, но кода генерации нет. То же с кнопками «Письмо» и «Пересмотреть» и командами `/search` и `/feedback`: бот отвечает, что они появятся на следующем этапе.

## Запуск

```bash
cp .env.example .env && chmod 600 .env
nano .env                         # четыре разных длинных пароля для ролей базы
docker compose up -d --build      # db → migrate → app
docker compose ps                 # db и app работают, migrate завершён с кодом 0
```

Пароли и имена ролей из `.env` применяются только при первом запуске, пока том базы пустой. Команда `docker compose down -v` удаляет все данные базы.

Подключение бота:
1. Создайте отдельного бота у @BotFather (`/newbot`), впишите токен в `.env` (`TELEGRAM_BOT_TOKEN`) и выполните `docker compose up -d app`.
2. Напишите боту `/start`. Он не ответит, пока не задан владелец. Свой ID найдите в логах: `docker compose logs app | grep bot_access_denied` (поле `user_id`).
3. Впишите его в `TELEGRAM_OWNER_ID` и снова выполните `docker compose up -d app`.
4. Для извлечения фактов нужен `ANTHROPIC_API_KEY`.

Полезные команды:
```bash
docker compose exec app python -m vacancy_hunter check-config    # конфигурация и секреты
docker compose exec app python -m vacancy_hunter check-env       # база, бот, владелец, ключ API
docker compose exec app python -m vacancy_hunter probe-sources   # доступность источников
docker compose exec app python -m vacancy_hunter collect --force # разовый сбор
docker compose exec app python -m vacancy_hunter process         # обработка новых записей
docker compose exec app python -m vacancy_hunter recalc          # пересчёт оценок без LLM
docker compose exec app python -m vacancy_hunter show-scores     # карточки лучших оценок
docker compose exec app python -m vacancy_hunter report --dry-run # отчёт без отправки
```

Пример вывода `check-config` (реальный запуск без ключей):
```
Конфигурация в порядке: 7 файлов проверено, параметры подключения к базе заданы.
Необязательные сейчас секреты:
  - TELEGRAM_BOT_TOKEN: не задан (шаг A2, без него бот и отправка отключены)
  - TELEGRAM_OWNER_ID: не задан (шаг A2, без него бот никому не отвечает)
  - HEARTBEAT_URL: не задан (шаг A2, необязательно)
  - ANTHROPIC_API_KEY: не задан (шаг A5)
```

Эксплуатация на сервере описана в `RUNBOOK.md`.

## Тесты

```bash
cp .env.example .env && chmod 600 .env   # если ещё нет: четыре пароля для ролей базы
docker compose build                      # после любых изменений кода, иначе тесты пойдут на старом образе
docker compose up -d db && docker compose run --rm migrate
docker compose run --rm app pytest
```

472 теста: модульные (`tests/unit`, без базы) и интеграционные (`tests/integration`, на отдельной тестовой базе PostgreSQL). Сеть в тестах запрещена: Telegram, HTTP-источники и LLM подменяются заглушками из `tests/fakes`. Разбор источников проверяется на сохранённых страницах из `tests/samples`. Сценарии промптов на настоящей модели (`prompt-scenarios`) запускаются отдельно и платно, в `pytest` не входят.

## Ограничения

- **Под Грузию.** Часовой пояс расписания (`Asia/Tbilisi`), подписи времени в боте и фильтр «перечень стран включает Грузию» зашиты в код. `search.current_location` в профиле задаётся параметром, но для другой страны придётся править код.
- **Один пользователь.** Бот отвечает только владельцу, профиль один и ведётся вручную в YAML.
- **Источники.** hh читается только через RSS поиска (без API-токена), Telegram только через публичные превью каналов. Закрытые каналы недоступны, вёрстка t.me может измениться.
- **Качество оценки** зависит от того, насколько верно LLM извлекла факты. В карточке показываются цитаты и причины, но ошибки извлечения возможны.
- **Стоимость.** Извлечение платное, лимиты задаются в `config/llm.yaml`. Цены в нём проверены на 13.11.2026 и могут устареть.
- Письма и ответы работодателям пока не реализованы (см. выше).

## Где что лежит

```
config/             семь файлов настроек (*.yaml): профиль, навыки, скоринг, источники, LLM, расписание, хранение
docker/db-init/     первичная настройка базы: роли, базы, расширение pg_trgm
migrations/         миграции Alembic
src/hunter_core/    общая основа: конфигурация, база, логи, HTTP, планировщик, outbox, LLM-шлюз и бюджет, пульс
src/vacancy_hunter/ логика вакансий: источники, конвейер, оценка, отчёт, бот
tests/unit/         тесты без базы
tests/integration/  тесты с настоящей базой
tests/fakes/        подставные часы, отправщик Telegram, HTTP, LLM
```

## Лицензия

MIT, см. [LICENSE](LICENSE).
