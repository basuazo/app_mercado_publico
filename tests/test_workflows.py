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


def test_ciclo_ca_corre_ca_match_alerts_detalles_match() -> None:
    """detalles-match va al final: `alerts` no espera a los detalles (F-detalles-match)."""
    texto = (_WORKFLOWS / "ciclo-ca.yml").read_text(encoding="utf-8")
    assert _LINEA_JOBS.findall(texto) == ["ca match alerts detalles-match"]


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
    revienta el parseo de int/float. DETALLES_MINUTOS_DIA/NOCHE (F-detalles-match)
    son opcionales, igual que las CA_*."""
    texto = (_WORKFLOWS / "_job.yml").read_text(encoding="utf-8")
    loop = re.search(r"for v in ([A-Z_ ]+); do\s+if \[ -z \"\$\{!v\}\" \]; then unset", texto)
    assert loop, "no encontré el loop de unset de opcionales"
    opcionales = set(loop.group(1).split())
    assert {
        "CA_TAMANO_PAGINA",
        "CA_MAX_PAGINAS_POR_VENTANA",
        "DETALLES_MINUTOS_DIA",
        "DETALLES_MINUTOS_NOCHE",
    } <= opcionales
    # Reemplazada por el tope de tiempo: no debe quedar colgando.
    assert "MATCH_MAX_DETALLES_POR_CORRIDA" not in texto
    for v in opcionales:
        assert re.search(rf"^\s*{v}: \$\{{\{{ vars\.{v} \}}\}}\s*$", texto, re.MULTILINE), v


# ---------------------------------------------------------------------------
# Disparo externo (F-actions-3) y concurrencia (F-actions-2)
# ---------------------------------------------------------------------------

_PROGRAMADOS = {
    "ca.yml",
    "ciclo-match.yml",
    "ciclo-activas.yml",
    "nocturno.yml",
    "retencion.yml",
    "catalogos.yml",
    "resumen.yml",
}


def _llamadores() -> list[Path]:
    """Los workflows que disparan algo: todos menos el reutilizable."""
    return [p for p in _archivos() if p.name != "_job.yml"]


def _texto(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _bloque_on(texto: str) -> str:
    """El bloque `on:` de nivel superior, hasta la siguiente clave sin sangría."""
    m = re.search(r"^on:\s*\n((?:[ \t]+.*\n|\s*\n)*)", texto, re.MULTILINE)
    assert m, "no encontré el bloque `on:`"
    return m.group(1)


def test_estan_los_ocho_workflows() -> None:
    assert {p.name for p in _llamadores()} == _PROGRAMADOS | {"ciclo-ca.yml"}


@pytest.mark.parametrize("workflow", _archivos(), ids=lambda p: p.name)
def test_ningun_workflow_tiene_schedule(workflow: Path) -> None:
    """Los disparos vienen de cron-job.org (docs/operacion-disparos.md). Un
    `schedule` de vuelta dispararía dos veces: cuota doble o dos correos."""
    texto = _texto(workflow)
    assert not re.search(r"^\s*schedule:", texto, re.MULTILINE), workflow.name
    assert not re.search(r"^\s*-?\s*cron:", texto, re.MULTILINE), workflow.name


@pytest.mark.parametrize("workflow", _llamadores(), ids=lambda p: p.name)
def test_todos_se_disparan_por_dispatch_sin_inputs(workflow: Path) -> None:
    """cron-job.org llama a la API solo con {"ref": "main"}: un input obligatorio
    haría fallar el disparo, y uno opcional quedaría siempre en su default."""
    on = _bloque_on(_texto(workflow))
    assert re.fullmatch(r"\s*workflow_dispatch:\s*", on), f"{workflow.name}: {on!r}"


@pytest.mark.parametrize("workflow", _llamadores(), ids=lambda p: p.name)
def test_cada_workflow_tiene_su_propio_grupo_de_concurrencia(workflow: Path) -> None:
    texto = _texto(workflow)
    grupos = re.findall(r"^\s*group:\s*(.+?)\s*$", texto, re.MULTILINE)
    assert grupos == ["mp-${{ github.workflow }}"], workflow.name
    assert re.search(r"^\s*cancel-in-progress:\s*false\s*$", texto, re.MULTILINE)
    assert "mp-jobs" not in texto


@pytest.mark.parametrize("workflow", _llamadores(), ids=lambda p: p.name)
def test_el_timeout_cubre_la_espera_del_lock(workflow: Path) -> None:
    texto = _texto(workflow)
    timeout = re.search(r"^\s*timeout_min:\s*(\d+)\s*$", texto, re.MULTILINE)
    espera = re.search(r"^\s*esperar_lock_min:\s*(\d+)\s*$", texto, re.MULTILINE)
    assert timeout and espera, f"{workflow.name}: timeout_min y esperar_lock_min explícitos"
    assert int(timeout.group(1)) >= int(espera.group(1)) + 10, workflow.name


def test_el_job_reutilizable_pasa_la_espera_al_cli() -> None:
    texto = _texto(_WORKFLOWS / "_job.yml")
    assert re.search(r"^\s*esperar_lock_min:\s*$", texto, re.MULTILINE)
    assert "ESPERAR_LOCK_MIN: ${{ inputs.esperar_lock_min }}" in texto
    assert '--esperar-lock-min="$ESPERAR_LOCK_MIN"' in texto
