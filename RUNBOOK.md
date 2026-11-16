# RUNBOOK — эксплуатация AI Vacancy Hunter

Инструкция для владельца: как запустить, обновить, проверить, восстановить. Все команды
выполняются в каталоге проекта. Значения в `.env` записываются без кавычек (команды ниже читают их через `cut`). `docker compose exec app python -m vacancy_hunter <команда>`
ниже сокращается до `vh <команда>`.

## 1. Что где лежит

| Что | Где |
|-----|-----|
| Код и настройки | каталог проекта, `config/*.yaml` |
| Секреты | `.env` (только на сервере, в Git не попадает) |
| База данных | том Docker `vacancy-hunter_db_data` |
| Резервные копии | том Docker `vacancy-hunter_backups` (7 ежедневных и 4 еженедельных) и файлом в Telegram каждый день в 04:00 |
| Журналы | `docker compose logs app` (5 файлов по 10 МБ, старые перезаписываются) |

## 2. Расписание (время Тбилиси)

| Время | Задача |
|-------|--------|
| каждые 3 часа и после старта | сбор, сразу за ним обработка |
| каждые 30 минут | обработка: дубли, правила, извлечение, оценка |
| раз в час и после старта | пересчёт оценок, если менялись профиль или правила |
| 03:30 | очистка по срокам хранения |
| 04:00 | резервная копия, файл приходит в Telegram |
| 07:00 (меняется в /settings) | ежедневный отчёт; если приложение было выключено — догоняющий отчёт до 14:00 |

Если в момент задачи приложение не работало, очистка, копия и отчёт выполняются после запуска (до 14:00).

## 3. Первый запуск на новом сервере (шаг 0.3 и развёртывание)

### 3.1. Проверка сервера (ARCHITECTURE.md, раздел 15.1)

```
uname -m; cat /etc/os-release | head -3          # архитектура и ОС
free -h                                          # свободно не меньше 1 ГБ сверх занятого
df -h /                                          # свободно не меньше 10 ГБ
systemctl list-units --type=service --state=running
ss -tlnp                                         # занятые порты; проекту порты не нужны
docker --version && docker compose version
```

Если чего-то не хватает — отдельный недорогой VPS с Ubuntu, без изменений в проекте.

### 3.2. Docker на Oracle Linux 9 (проверено на ARM-сервере, шаг 0.3)

```
rpm -q podman podman-docker runc buildah        # что уже стоит; если podman используется другими службами — не удалять, а спросить
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"                  # затем выйти из SSH и зайти снова
docker run --rm hello-world                      # проверка
```

Если `dnf` сообщает о конфликте с `podman`/`runc`, остановитесь и пришлите вывод: удаление пакетов
может затронуть другие службы сервера. SELinux оставить включённым — папки проекта подключаются
с меткой `:z` (docker-compose.yml).

### 3.3. Код на сервер без удалённого репозитория

На компьютере (папка проекта): `git bundle create ../vacancy-hunter.bundle master`
и скопировать файл: `scp ../vacancy-hunter.bundle <пользователь>@<сервер>:~/`.
На сервере: `git clone ~/vacancy-hunter.bundle ~/vacancy-hunter && cd ~/vacancy-hunter`.
Обновление позже: новый bundle на сервер, затем `git pull ~/vacancy-hunter.bundle master`.

### 3.4. Установка

```
# код — по разделу 3.3 (или git clone, если подключён удалённый репозиторий)
cp .env.example .env && nano .env        # заполнить все значения (пароли — новые, длинные)
chmod 600 .env
docker compose up -d --build             # db → migrate → app
docker compose ps                        # app: Up, db: healthy, migrate: Exited (0)
vh check-config
vh check-env                             # база, Telegram, пульс, ключ LLM
vh probe-sources                         # источники с сервера; недоступные — mode: disabled в sources.yaml
docker compose exec app pytest -q        # все тесты должны пройти
```

Автозапуск после перезагрузки: `sudo systemctl enable docker`; контейнеры имеют `restart: unless-stopped`.
Проверка: `sudo reboot`, через пару минут — `docker compose ps` и /status в боте.

### 3.5. Перенос данных с компьютера на сервер (если нужно сохранить накопленное)

1. На компьютере: `vh backup`, затем `docker compose cp app:/app/backups/<файл> .`
2. Скопировать файл на сервер (`scp`), на сервере после установки — раздел 6.2.
3. Когда приложение на сервере заработало, на компьютере: `docker compose stop` — один токен бота
   не должен работать в двух местах одновременно.

## 4. Обновление

```
git pull
docker compose up -d --build            # пересобрать образ, применить миграции, перезапустить app
docker compose logs --tail 50 app
```

База при обновлении не перезапускается. Правка `config/profile.yaml`, `skills.yaml` или `scoring.yaml`
подхватывается без перезапуска: новая версия появится при ближайшей обработке, оценки пересчитаются
сами (без LLM). Остальные файлы `config/` — после `docker compose restart app`.

## 5. Ежедневная проверка (1 минута)

- В 07:00 пришёл отчёт (сводка первой).
- В 04:00 пришёл файл резервной копии.
- /status: источники без ⛔, задачи без «ошибка», бюджет в пределах.

## 6. Резервные копии

### 6.1. Сделать копию вручную и посмотреть список

```
vh backup               # копия сейчас; файл придёт в Telegram
vh backup --list
docker compose cp app:/app/backups/<файл> .       # забрать файл с сервера
```

### 6.2. Восстановление

**Проверка раз в месяц (в пустую проверочную базу, рабочая не трогается):**

```
ADMIN=$(grep '^POSTGRES_ADMIN_USER=' .env | cut -d= -f2); APP=$(grep '^DB_APP_USER=' .env | cut -d= -f2)
docker compose exec -T db psql -U "$ADMIN" -d postgres -c "CREATE DATABASE restore_check OWNER \"$APP\""
docker compose exec -T -e RESTORE_DB_NAME=restore_check app python -m vacancy_hunter backup --restore /app/backups/<файл>
# сверить число строк: vacancies, scores, extractions — в restore_check и в рабочей базе
docker compose exec -T db psql -U "$ADMIN" -d postgres -c "DROP DATABASE restore_check"
```

Запишите дату проверки в `ACCEPTANCE.md` (ACC-10).

**Настоящее восстановление** (заменяет текущие данные базы данными из копии — только если рабочая база
испорчена или потеряна; процедура проверена 14.11.2026 на отдельной базе):

```
vh backup                      # сначала копия текущего состояния — страховка, если база испорчена частично
docker compose stop app
# если файла нет в томе (например, он скачан из Telegram) — положить его в том
# (chmod — чтобы файл мог прочитать пользователь внутри контейнера):
chmod 644 <файл> && docker compose cp <файл> app:/app/backups/
# если пропал весь том базы: `docker compose up -d db` создаст базу и роли заново,
# затем пустая схема: docker compose run --rm migrate
docker compose run --rm -v vacancy-hunter_backups:/app/backups migrate sh -c '
  F=/app/backups/<файл>; export PGPASSWORD="$DB_MIGRATOR_PASSWORD"
  pg_restore -l "$F" | grep -v " EXTENSION " > /tmp/list &&
  pg_restore -h db -U "$DB_MIGRATOR_USER" -d "$DB_NAME" --clean --if-exists --no-owner --no-privileges \
             --single-transaction --exit-on-error -L /tmp/list "$F" && echo ВОССТАНОВЛЕНО'
docker compose up -d
```

Восстановление идёт одной транзакцией: при ошибке база остаётся как была. Права пользователя
приложения выдаются автоматически (права по умолчанию для таблиц пользователя миграций).

## 7. Типовые ситуации

| Что видно | Что делать |
|-----------|------------|
| Отчёт не пришёл | /status → задача report; `docker compose logs app \| grep report`. Пауза? (/settings). Если приложение было выключено — догоняющий отчёт до 14:00 |
| Источник «⛔ запрет доступа» | Сайт запретил доступ. Не обходить. `vh unblock-source <код>` — только если доступ разрешён снова |
| Источник «⚠️ деградировал» | 5 сбоев подряд, опрос раз в 12 часов, вернётся сам при успехе |
| «Бюджет исчерпан» | Извлечение остановлено до нового дня/месяца; отчёт приходит. Лимиты — `config/llm.yaml` |
| Ошибка пересчёта | /status покажет число; после 3 ошибок по одной вакансии придёт оповещение |
| Сообщения «не подтверждено» | Telegram не ответил: автоматически не повторяются, см. /today |
| Нужно остановить всё | `docker compose stop` (данные сохраняются). Запуск — `docker compose up -d` |
| Копия не пришла файлом, в /status «не подтверждено» | Большой файл (до 49 МБ) мог не успеть подтвердиться за 30 с. Копия цела на сервере: `vh backup --list` |
| Диск заполняется | `docker system df`; копии — `vh backup --list`; журналы ограничены 50 МБ |

## 8. Чего делать нельзя

- `docker compose down -v` — удалит тома с базой и копиями.
- Хранить `.env` в Git или пересылать его.
- Подключать к проекту чужие базы, токены и сессии (в том числе соседнего проекта на том же сервере).
