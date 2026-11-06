"""Descarga directa del adjunto PÚBLICO de una Compra Ágil (F-plat-anexos-2 §2.7, regla 9).

ÚLTIMA ALTERNATIVA, después de la API (la sonda `--documentos` no encontró ruta, R8 [V, 5-nov]) y
de la subida manual / carga masiva. Capa anti-corrupción: es el ÚNICO módulo que conoce la URL
del archivo. Reglas que cumple:

- Uno a uno, ≤ 1 req/s con jitter (el `RateLimiter` de `clients/base.py`, instancia propia: no
  comparte la tasa del ticket porque no usa el ticket ni la cuota de la API).
- Cliente identificado (`User-Agent` propio, sin datos personales), sin cookies ni sesión: el
  jar se vacía antes de cada request y los redirects (máx. 2) se siguen a mano.
- Nunca parsea HTML: una respuesta `text/html` se descarta (`no_archivo`).
- Tope de 10 MB por archivo, leído en streaming con corte.
- 403 o 429 → :class:`DescargaBloqueada` (quien llama corta la corrida y persiste una pausa);
  504 o timeout → :class:`DescargaTransitoria` tras enfriar 60 s (el servidor puede seguir
  procesando, regla 3). No elude ninguna protección.

`URL_ADJUNTO` queda en None hasta que Boris capture la URL real en el navegador (DevTools →
Network al bajar un adjunto de la ficha pública) y la anote en `01-analisis` R8 como [V]: sin
ella, :attr:`DocumentosClient.configurado` es False y nada se descarga aunque la bandera
`ANEXOS_DESCARGA_DIRECTA` esté encendida.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urljoin

import httpx

from app.clients.base import RateLimiter
from app.core.logging import get_logger

_log = get_logger(__name__)

# Plantilla con `{codigo}` y `{id}` (el `id` de `documentos[]` del detalle v2). [PENDIENTE: captura
# de Boris, R8]. No se infiere: una URL adivinada no se escribe como hecha (regla 23).
URL_ADJUNTO: str | None = None

USER_AGENT = "Rastrillo/1.0 (lector de adjuntos publicos de Compra Agil; uso interno, 1 req/s)"
MAX_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 2
ENFRIAMIENTO_S = 60.0
TIMEOUT_S = 30.0


class DescargaBloqueada(Exception):
    """403 o 429 del sitio: se corta la corrida y se persiste una pausa (regla 9)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class DescargaTransitoria(Exception):
    """504 o timeout: ya se enfrió 60 s; el archivo se intentará en otra corrida."""


@dataclass(frozen=True)
class Descarga:
    data: bytes | None
    # None si llegó el archivo; si no: no_archivo (HTML) | muy_grande | http_<status> |
    # demasiados_redirects | error_red
    motivo: str | None = None


class DocumentosClient:
    def __init__(
        self,
        *,
        http: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        dormir: Callable[[float], None] = time.sleep,
        url: str | None = None,
    ) -> None:
        self._url = url if url is not None else URL_ADJUNTO
        self._http = http or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        self._limiter = limiter or RateLimiter(1.0)
        self._dormir = dormir

    @property
    def configurado(self) -> bool:
        return bool(self._url)

    def _url_de(self, codigo: str, doc_id: str) -> str:
        assert self._url
        return self._url.format(codigo=quote(codigo, safe=""), id=quote(doc_id, safe=""))

    def descargar_adjunto(self, codigo: str, doc_id: str) -> Descarga:
        """El archivo del adjunto `doc_id` de la CA `codigo`. Ver el docstring del módulo."""
        if not self.configurado:
            raise RuntimeError("Descarga directa sin URL configurada (R8 pendiente)")
        url = self._url_de(codigo, doc_id)
        for _salto in range(MAX_REDIRECTS + 1):
            self._limiter.acquire()
            self._http.cookies.clear()  # sin cookies ni sesión (regla 9)
            try:
                with self._http.stream(
                    "GET", url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
                ) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        destino = r.headers.get("Location")
                        if not destino:
                            return Descarga(None, f"http_{r.status_code}")
                        url = urljoin(url, destino)
                        continue
                    return self._leer(r)
            except httpx.TimeoutException:
                self._enfriar("timeout")
                raise DescargaTransitoria("timeout") from None
            except httpx.HTTPError as exc:
                _log.warning("Descarga directa: error de red (%s)", type(exc).__name__)
                return Descarga(None, "error_red")
            finally:
                self._http.cookies.clear()
                self._limiter.tras_respuesta()
        return Descarga(None, "demasiados_redirects")

    def _leer(self, r: httpx.Response) -> Descarga:
        if r.status_code in (403, 429):
            raise DescargaBloqueada(r.status_code)
        if r.status_code == 504:
            self._enfriar("504")
            raise DescargaTransitoria("504")
        if r.status_code != 200:
            return Descarga(None, f"http_{r.status_code}")
        tipo = r.headers.get("Content-Type", "").lower()
        if "text/html" in tipo:
            return Descarga(None, "no_archivo")  # sin leer ni parsear el cuerpo (regla 9)
        largo = r.headers.get("Content-Length")
        if largo and largo.isdigit() and int(largo) > MAX_BYTES:
            return Descarga(None, "muy_grande")
        partes: list[bytes] = []
        total = 0
        for trozo in r.iter_bytes():
            total += len(trozo)
            if total > MAX_BYTES:
                return Descarga(None, "muy_grande")
            partes.append(trozo)
        return Descarga(b"".join(partes))

    def _enfriar(self, causa: str) -> None:
        _log.warning("Descarga directa: %s, enfrío %.0f s", causa, ENFRIAMIENTO_S)
        self._dormir(ENFRIAMIENTO_S)
