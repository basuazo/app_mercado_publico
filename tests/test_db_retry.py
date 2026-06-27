"""Tests F-fix — reintento acotado de commits (app.core.db_retry)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from app.core.db_retry import MAX_INTENTOS, commit_con_retry


def _operational_error() -> OperationalError:
    return OperationalError("commit", {}, Exception("server closed the connection"))


class TestCommitConRetry:
    def test_exito_al_primer_intento(self):
        session = MagicMock()
        aplicado = {"n": 0}

        def aplicar() -> None:
            aplicado["n"] += 1

        ok = commit_con_retry(session, aplicar, contexto="test")

        assert ok is True
        assert aplicado["n"] == 1
        session.commit.assert_called_once()
        session.rollback.assert_not_called()

    def test_recupera_tras_fallo_transitorio(self):
        session = MagicMock()
        session.commit.side_effect = [_operational_error(), None]
        aplicado = {"n": 0}
        sleeps: list[float] = []

        def aplicar() -> None:
            aplicado["n"] += 1

        ok = commit_con_retry(session, aplicar, contexto="test", sleep_fn=sleeps.append)

        assert ok is True
        assert aplicado["n"] == 2  # se reaplicó el trabajo completo en el reintento
        assert session.commit.call_count == 2
        session.rollback.assert_called_once()
        assert len(sleeps) == 1

    def test_agota_reintentos_y_retorna_false(self):
        session = MagicMock()
        session.commit.side_effect = _operational_error()
        sleeps: list[float] = []

        ok = commit_con_retry(session, lambda: None, contexto="test", sleep_fn=sleeps.append)

        assert ok is False
        assert session.commit.call_count == MAX_INTENTOS
        assert session.rollback.call_count == MAX_INTENTOS
        assert len(sleeps) == MAX_INTENTOS - 1

    def test_otros_errores_no_se_capturan(self):
        session = MagicMock()
        session.commit.side_effect = RuntimeError("no relacionado a la conexión")

        with pytest.raises(RuntimeError):
            commit_con_retry(session, lambda: None, contexto="test")
