"""Amarra los workflows de GitHub Actions con el CLI (F-actions-1), offline.

El cron de `ciclo-activas` en cron-job.org quedó apuntando a `?job=<ciclo-activas>`
—con los `<>` literales— y nadie lo vio en semanas. Este test hace imposible
repetirlo del lado de Actions: todo job nombrado en un workflow tiene que
existir en el CLI.

Convención (ver docs/archivo/prompts/prompt-F-actions-1-canary.md): la lista de jobs va SIEMPRE
en una línea con comillas dobles, `jobs: "ca match alerts"`. Parseo por regex,
sin PyYAML.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
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
    revienta el parseo de int/float. DETALLES_MINUTOS_DIA/NOCHE (F-detalles-match) y
    DETALLES_FALLOS_MAX/ESPERA_HORAS (F-detalles-fallos) son opcionales, igual que las CA_*."""
    texto = (_WORKFLOWS / "_job.yml").read_text(encoding="utf-8")
    loop = re.search(r"for v in ([A-Z_ ]+); do\s+if \[ -z \"\$\{!v\}\" \]; then unset", texto)
    assert loop, "no encontré el loop de unset de opcionales"
    opcionales = set(loop.group(1).split())
    assert {
        "CA_TAMANO_PAGINA",
        "CA_MAX_PAGINAS_POR_VENTANA",
        "DETALLES_MINUTOS_DIA",
        "DETALLES_MINUTOS_NOCHE",
        "DETALLES_FALLOS_MAX",
        "DETALLES_ESPERA_HORAS",
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
    # F-merc-1: se crean en cron-job.org cuando Boris habilite el backfill.
    "mercado.yml",
    "mercado-nocturno.yml",
    # F-merc-2: datos abiertos, 07:20 Chile.
    "mercado-oc.yml",
    # F-plat-4: índice y modelo de adjudicación, 07:45 Chile.
    "mercado-indices.yml",
}


_INPUT_COMPLETO = re.compile(
    r"[ \t]+inputs:[ \t]*\n[ \t]+completo:[ \t]*\n[ \t]+description:.*\n[ \t]+type: boolean[ \t]*\n"
    r"[ \t]+required: false[ \t]*\n[ \t]+default: false[ \t]*\n"
)


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


def test_estan_los_diez_workflows() -> None:
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
    if workflow.name == "mercado-indices.yml":
        # F-plat-6-fix: único input permitido, opcional y en false por defecto (ver abajo).
        on = _INPUT_COMPLETO.sub("", on)
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
    # Un timeout condicional (`${{ inputs.completo && 180 || 60 }}`) se mide por su default.
    timeout = re.search(
        r"^\s*timeout_min:\s*(?:\$\{\{ inputs\.completo && \d+ \|\| )?(\d+)(?: \}\})?\s*$",
        texto,
        re.MULTILINE,
    )
    espera = re.search(r"^\s*esperar_lock_min:\s*(\d+)\s*$", texto, re.MULTILINE)
    assert timeout and espera, f"{workflow.name}: timeout_min y esperar_lock_min explícitos"
    assert int(timeout.group(1)) >= int(espera.group(1)) + 10, workflow.name


def test_el_job_reutilizable_pasa_la_espera_al_cli() -> None:
    texto = _texto(_WORKFLOWS / "_job.yml")
    assert re.search(r"^\s*esperar_lock_min:\s*$", texto, re.MULTILINE)
    assert "ESPERAR_LOCK_MIN: ${{ inputs.esperar_lock_min }}" in texto
    assert '--esperar-lock-min="$ESPERAR_LOCK_MIN"' in texto


# ---------------------------------------------------------------------------
# F-plat-6-fix: carga completa desde Actions
# ---------------------------------------------------------------------------


def test_mercado_indices_tiene_input_completo_opcional_en_false() -> None:
    on = _bloque_on(_texto(_WORKFLOWS / "mercado-indices.yml"))
    assert _INPUT_COMPLETO.search(on), on
    # Fuera de ese input, el disparo sigue siendo un dispatch pelado.
    assert re.fullmatch(r"\s*workflow_dispatch:\s*", _INPUT_COMPLETO.sub("", on))


def test_mercado_indices_pasa_completo_y_sube_el_timeout_solo_si_viene() -> None:
    texto = _texto(_WORKFLOWS / "mercado-indices.yml")
    assert re.search(r"^\s*completo: \$\{\{ inputs\.completo == true \}\}\s*$", texto, re.M)
    t = re.search(r"timeout_min: \$\{\{ inputs\.completo && (\d+) \|\| (\d+) \}\}", texto)
    assert t and int(t.group(1)) == 180 and int(t.group(2)) == 60


def test_job_reutilizable_completo_es_opcional_y_falso_por_defecto() -> None:
    texto = _texto(_WORKFLOWS / "_job.yml")
    m = re.search(r"^      completo:[ \t]*\n((?:        .*\n)+)", texto, re.MULTILINE)
    assert m, "falta el input `completo` en _job.yml"
    bloque = m.group(1)
    assert "type: boolean" in bloque
    assert "required: false" in bloque
    assert "default: false" in bloque
    assert "COMPLETO: ${{ inputs.completo }}" in texto


def _jobs_con_completo() -> set[str]:
    m = re.search(r'^\s*JOBS_CON_COMPLETO="([^"]*)"', _texto(_WORKFLOWS / "_job.yml"), re.M)
    assert m
    return set(m.group(1).split())


def test_completo_solo_para_los_jobs_que_lo_aceptan() -> None:
    """`mercado-catalogo --inicial` no se mezcla; todos existen en el CLI."""
    jobs = _jobs_con_completo()
    assert jobs == {"mercado-indices", "mercado-competencia", "mercado-demanda"}
    assert jobs <= set(_JOBS)


def _bash() -> str:
    """Un bash que ejecute el script tal cual. En Windows `which("bash")` da el bash de WSL (otro
    sistema de archivos y sin variables heredadas): se usa el Git Bash."""
    if sys.platform != "win32":
        bash = shutil.which("bash")
        assert bash, "se necesita bash para simular el step"
        return bash
    candidatos: list[Path] = []
    git = shutil.which("git")
    if git:
        candidatos.append(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    candidatos.append(Path("C:/Program Files/Git/bin/bash.exe"))
    for c in candidatos:
        if c.is_file():
            return str(c)
    pytest.fail("se necesita Git Bash (bin/bash.exe de Git for Windows) para simular el step")


def _simular(completo: str, jobs: str) -> list[str]:
    """Corre con bash el armado de argumentos del step, con `python -m app.ingest` → `echo`."""
    bash = _bash()
    texto = _texto(_WORKFLOWS / "_job.yml")
    lista = re.search(r'^\s*(JOBS_CON_COMPLETO="[^"]*")', texto, re.M)
    armado = re.search(r'^(\s*extra=""\n(?:.*\n){3}\s*python -m app\.ingest run-once.*)$', texto, re.M)
    assert lista and armado
    cuerpo = armado.group(1).replace("python -m app.ingest", "echo")
    # Las variables van DENTRO del script (no hereda `env=` con cualquier bash) y el script entra por
    # stdin: la línea de comandos de Windows no lo altera.
    entrada = f'COMPLETO="{completo}"\nJOBS="{jobs}"\nESPERAR_LOCK_MIN=10\n'
    script = f"{entrada}{lista.group(1)}\nfor j in $JOBS; do\n{cuerpo}\ndone\n"
    out = subprocess.run(
        [bash, "-s"],
        input=script.encode(),  # bytes: en modo texto, Windows cambiaría LF por CRLF
        capture_output=True,
        check=True,
    )
    return out.stdout.decode().splitlines()


def test_disparo_de_cron_job_org_no_cambia() -> None:
    """Sin inputs, `completo` llega como "false": ningún job recibe `--completo`."""
    lineas = _simular("false", "mercado-indices mercado-competencia mercado-demanda")
    assert len(lineas) == 3 and all("--completo" not in x for x in lineas)


def test_con_completo_solo_lo_reciben_los_tres_jobs() -> None:
    lineas = _simular("true", "mercado-indices mercado-competencia mercado-demanda mercado-catalogo")
    con = {x.split("--job=")[1].split()[0] for x in lineas if x.rstrip().endswith("--completo")}
    assert len(lineas) == 4
    assert con == {"mercado-indices", "mercado-competencia", "mercado-demanda"}
