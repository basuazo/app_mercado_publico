<#
.SYNOPSIS
  Dispara los jobs de ingesta contra /api/jobs/run desde esta maquina.

.DESCRIPCION
  Puente temporal mientras Cloudflare le responde 429 al scheduler de cron-job.org
  (21-sep-2026) y F-actions todavia no esta desplegada. Las requests desde esta
  maquina SI pasan.

  Entre job y job consulta /api/salud/jobs cada 60 s. Eso cumple dos funciones:
  muestra el avance, y mantiene despierto el proceso en Render, que se duerme a los
  15 min de la ultima request ENTRANTE aunque tenga un job corriendo en background.

  Los jobs se disparan de a uno y en serie: el advisory lock es una llave global y
  dos jobs simultaneos hacen que uno se omita en silencio.

.EJEMPLOS
  .\scripts\ingesta_manual.ps1 -Ciclo manana
  .\scripts\ingesta_manual.ps1 -Ciclo tarde
  .\scripts\ingesta_manual.ps1 -Ciclo noche
#>

param(
    [ValidateSet('manana', 'tarde', 'noche')]
    [string]$Ciclo = 'manana',

    [string]$Base = 'https://app-mercado-publico.onrender.com'
)

$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $raiz '.env'

if (-not (Test-Path $envFile)) { throw "No encuentro .env en $raiz" }

$tok = ((Select-String -Path $envFile -Pattern '^JOBS_TOKEN=').Line -replace '^JOBS_TOKEN=', '').Trim().Trim('"')
if ([string]::IsNullOrWhiteSpace($tok)) { throw 'JOBS_TOKEN vacio en .env' }

# job = minutos de espera antes de disparar el siguiente
$secuencias = @{
    manana = @(
        @{ job = 'ciclo-activas';  min = 10 }
        @{ job = 'ciclo-ca';       min = 5  }
        @{ job = 'datos-abiertos'; min = 6  }
        @{ job = 'resumen';        min = 3  }
    )
    tarde  = @(
        @{ job = 'ciclo-ca';       min = 5  }
        @{ job = 'ciclo-activas';  min = 10 }
    )
    noche  = @(
        @{ job = 'nocturno';       min = 20 }
        @{ job = 'retencion';      min = 4  }
    )
}

$pasos = $secuencias[$Ciclo]

# catalogos es semanal (lunes). Se engancha al ciclo nocturno del lunes.
if ($Ciclo -eq 'noche' -and (Get-Date).DayOfWeek -eq 'Monday') {
    $pasos += @{ job = 'catalogos'; min = 5 }
}

# nocturno valida por si mismo la ventana 22:00-07:00 America/Santiago (regla 5).
# Fuera de esa franja aborta y no hace nada: mejor avisar antes de esperar 20 min.
if ($Ciclo -eq 'noche') {
    $h = [int](Get-Date).Hour
    if ($h -lt 22 -and $h -ge 7) {
        Write-Warning "Son las $($h):00. El job 'nocturno' solo corre entre 22:00 y 07:00 hora Chile; fuera de esa ventana aborta solo."
        $r = Read-Host 'Seguir igual? (s/N)'
        if ($r -ne 's') { exit 0 }
    }
}

function Mostrar-Salud {
    try {
        $s = Invoke-RestMethod "$Base/api/salud/jobs" -TimeoutSec 60
        $resumen = ($s.jobs | ForEach-Object { "$($_.job)=$([math]::Round($_.edad_horas,1))h" }) -join '  '
        Write-Host ("    [{0}] {1}  ->  {2}" -f (Get-Date -Format 'HH:mm:ss'), $s.status, $resumen)
    }
    catch {
        Write-Host ("    [{0}] salud no responde ({1})" -f (Get-Date -Format 'HH:mm:ss'), $_.Exception.Message)
    }
}

Write-Host ""
Write-Host "=== Ciclo '$Ciclo' - $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" -ForegroundColor Cyan
Write-Host "Estado inicial:"
Mostrar-Salud

foreach ($paso in $pasos) {
    Write-Host ""
    Write-Host ">> $($paso.job)" -ForegroundColor Yellow
    try {
        $r = Invoke-RestMethod -Method Post -Uri "$Base/api/jobs/run?job=$($paso.job)" `
            -Headers @{ 'X-Jobs-Token' = $tok } -TimeoutSec 180
        Write-Host "    encolado: $($r.queued)"
    }
    catch {
        $code = $_.Exception.Response.StatusCode.value__
        Write-Host "    FALLO al encolar (HTTP $code)" -ForegroundColor Red
        if ($code -eq 401) { Write-Host '    -> el JOBS_TOKEN del .env no coincide con el de Render' -ForegroundColor Red }
        if ($code -eq 429) { Write-Host '    -> Cloudflare tambien te limita a vos. Avisar: es un dato nuevo.' -ForegroundColor Red }
        continue
    }

    # Espera + keep-alive. Cada sondeo cuenta como request entrante para Render.
    for ($i = 1; $i -le $paso.min; $i++) {
        Start-Sleep -Seconds 60
        Mostrar-Salud
    }
}

Write-Host ""
Write-Host "=== Fin. Estado final ===" -ForegroundColor Cyan
Mostrar-Salud
Write-Host ""
Write-Host "Revisar: los tres criticos (activas, ca, datos-abiertos) deberian quedar por debajo de sus umbrales (30h / 30h / 36h)."
