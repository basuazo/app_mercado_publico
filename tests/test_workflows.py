"""Amarra los workflows de GitHub Actions con el CLI (F-actions-1), offline.

El cron de `ciclo-activas` en cron-job.org quedó apuntando a `?job=<ciclo-activas>`
—con los `<>` literales— y nadie lo vio en semanas. Este test hace imposible
repetirlo del lado de Actions: todo job nombrado en un workflow tiene que
existir en el CLI.

Convención (ver docs/prompt-F-actions-1-canary.md): la lista de jobs va SIEMPRE
en una línea con comillas dobles, `jobs: "ca match alerts"`. Parseo por regex,
sin PyYAML.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.ingest.__main__ import _JOBS

_WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
_LINEA_JOBS = re.compile(r'^\s*jobs:\s*"([^"]*)"\s*$', re.MULTILINE)


def _archivos() -> list[Path]:
    return sorted(_WORKFLOWS.glob("*.yml"))


def test_hay_workflows() -> None:
    nombres = {p.name for p in _archivos()}
    assert {"_job.yml", "ciclo-ca.yml"} <= nombres


def test_al_menos_un_workflow_declara_jobs() -> None:
    """Sanidad del regex: si la convención se rompe, el test de abajo no vería nada."""
    listas = [m for p in _archivos() for m in _LINEA_JOBS.findall(p.read_text(encoding="utf-8"))]
    assert listas


@pytest.mark.parametrize("workflow", _archivos(), ids=lambda p: p.name)
def test_cada_job_de_cada_workflow_existe_en_el_cli(workflow: Path) -> None:
    for lista in _LINEA_JOBS.findall(workflow.read_text(encoding="utf-8")):
        nombres = lista.split()
        assert nombres, f"{workflow.name}: lista de jobs vacía"
        desconocidos = [n for n in nombres if n not in _JOBS]
        assert not desconocidos, f"{workflow.name}: jobs que el CLI no conoce: {desconocidos}"


def test_ciclo_ca_corre_ca_match_alerts() -> None:
    texto = (_WORKFLOWS / "ciclo-ca.yml").read_text(encoding="utf-8")
    assert _LINEA_JOBS.findall(texto) == ["ca match alerts"]


def test_job_reutilizable_no_trae_secret_key_ni_jobs_token_de_secrets() -> None:
    """El CLI no firma cookies ni atiende HTTP: SECRET_KEY y JOBS_TOKEN se generan
    efímeros en el step. Traer los de producción a un repo público es riesgo
    sin ningún beneficio."""
    texto = (_WORKFLOWS / "_job.yml").read_text(encoding="utf-8")
    assert not re.search(r"secrets\.(SECRET_KEY|JOBS_TOKEN)\b", texto)
    assert re.search(r"export SECRET_KEY=.*openssl rand", texto)
    assert re.search(r"export JOBS_TOKEN=.*openssl rand", texto)


def test_job_reutilizable_no_aplica_migraciones() -> None:
    """Las migraciones las aplica Render en el deploy; Actions solo mueve datos."""
    for p in _archivos():
        assert "alembic" not in p.read_text(encoding="utf-8"), p.name


def test_opcionales_del_job_reutilizable_se_quitan_si_vienen_vacias() -> None:
    """Una opcional que no está en el loop de `unset` llega a pydantic como "" y
    revienta el parseo de int/float. MATCH_MAX_DETALLES_POR_CORRIDA (F-raw-json)
    es opcional, igual que las CA_*."""
    texto = (_WORKFLOWS / "_job.yml").read_text(encoding="utf-8")
    loop = re.search(r"for v in ([A-Z_ ]+); do\s+if \[ -z \"\$\{!v\}\" \]; then unset", texto)
    assert loop, "no encontré el loop de unset de opcionales"
    opcionales = set(loop.group(1).split())
    assert {"CA_TAMANO_PAGINA", "MATCH_MAX_DETALLES_POR_CORRIDA"} <= opcionales
    for v in opcionales:
        assert re.search(rf"^\s*{v}: \$\{{\{{ vars\.{v} \}}\}}\s*$", texto, re.MULTILINE), v
