# Образ приложения. Используется контейнером app и одноразовым сервисом migrate.
# Базовый образ python:3.12-slim собирается для amd64 и arm64.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# pg_dump и pg_restore той же версии, что и сервер (postgres:16), — для резервных копий (шаг A8).
# В Debian только версия 15, поэтому клиент ставится из официального репозитория PostgreSQL (PGDG).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-common ca-certificates \
    && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y --auto-remove postgresql-common \
    && rm -rf /var/lib/apt/lists/*

# Сначала только зависимости: этот слой пересобирается лишь при изменении pyproject.toml.
COPY pyproject.toml ./
RUN python -c "import tomllib; d = tomllib.load(open('pyproject.toml', 'rb')); \
print('\n'.join(d['project']['dependencies'] + d['project']['optional-dependencies']['dev']))" \
        > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
COPY src ./src
COPY tests ./tests

# Приложение работает не от root. Файлы кода принадлежат root: процесс не может их менять.
RUN useradd --create-home --uid 10001 hunter \
    && mkdir -p /app/backups && chown hunter:hunter /app/backups
USER hunter

CMD ["python", "-m", "vacancy_hunter", "run"]
