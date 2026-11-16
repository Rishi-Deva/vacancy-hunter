"""Проверки тестовой инфраструктуры и логов шага A1."""

from __future__ import annotations

import io
import json
import logging
import socket

import pytest

from hunter_core.observability import setup_logging


def test_network_is_forbidden_in_tests():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="запрещён"):
            sock.connect(("93.184.216.34", 80))
    finally:
        sock.close()


def test_logs_are_json_lines_with_required_fields():
    stream = io.StringIO()
    setup_logging(stream=stream)
    logging.getLogger("vacancy_hunter.test").info("job_finished", extra={"job": "collect", "rows": 3})
    entry = json.loads(stream.getvalue().strip())
    assert entry["level"] == "info"
    assert entry["component"] == "vacancy_hunter.test"
    assert entry["event"] == "job_finished"
    assert entry["job"] == "collect" and entry["rows"] == 3
    assert entry["ts"].endswith("+00:00")


def test_secrets_are_masked_in_logs():
    secret = "super-secret-token-value"
    stream = io.StringIO()
    setup_logging(secrets=[secret], stream=stream)
    log = logging.getLogger("vacancy_hunter.test")
    log.info("connecting with %s", secret, extra={"detail": f"token={secret}"})
    try:
        raise ValueError(f"bad {secret}")
    except ValueError:
        log.exception("failed")
    output = stream.getvalue()
    assert secret not in output
    assert output.count("***") >= 3


def test_secrets_are_masked_in_nested_fields_and_stack():
    secret = "nested-secret-value-42"

    class Holder:
        def __str__(self):
            return f"Holder({secret})"

    stream = io.StringIO()
    setup_logging(secrets=[secret], stream=stream)
    logging.getLogger("vacancy_hunter.test").info(
        "event",
        extra={"d": {"k": secret, "list": [secret, {"deep": (secret,)}]}, "obj": Holder()},
        stack_info=True,
    )
    output = stream.getvalue()
    assert secret not in output
    entry = json.loads(output.strip())
    assert entry["d"] == {"k": "***", "list": ["***", {"deep": ["***"]}]}
    assert entry["obj"] == "Holder(***)"
    assert "stack" in entry


def test_tests_refuse_to_use_production_database():
    from types import SimpleNamespace

    from tests.integration.conftest import _guard_against_production

    env = {"DB_NAME": "vacancy_hunter", "DB_APP_USER": "hunter_app", "DB_MIGRATOR_USER": "hunter_migrator"}
    _guard_against_production(SimpleNamespace(name="vacancy_hunter_test", user="hunter_test"), env)
    for bad in (SimpleNamespace(name="vacancy_hunter", user="hunter_test"),
                SimpleNamespace(name="vacancy_hunter_test", user="hunter_app"),
                SimpleNamespace(name="vacancy_hunter_test", user="hunter_migrator")):
        with pytest.raises(pytest.exit.Exception):
            _guard_against_production(bad, env)
