"""Cliente del servicio de adjuntos de Compra Ágil de la ficha pública (F-plat-anexos-auto, regla 9).

ÚLTIMA ALTERNATIVA de la regla 9: la API oficial no entrega el archivo (R8 [V, 5-nov]). Capa
anti-corrupción: es el ÚNICO módulo que conoce las URLs y los headers de ese servicio.

Flujo [V, paso 0 de Boris, 6–7-nov, ficha `2467-760-COT26` con 3 anexos]: token anónimo
(`GET {URL_TOKEN}`) → `GET {URL_BASE}/listar/{codigo}` (`payload.files[] = {id, nombreArchivo}`) →
`GET {URL_BASE}/descargar/{id}` (respuesta binaria con content-disposition). `listar` y `descargar`
llevan `Authorization: Bearer <JWT>` y `user_key`; acá además solo nuestro `User-Agent` (sin origin,
referer, sec-* ni user-agent de navegador; si la sonda muestra que falta `origin`, se agrega SOLO
ese vía `MP_ADJUNTOS_HEADERS_EXTRA`, documentado en decisiones). No existe `encrypt`. El JWT es ANÓNIMO (Keycloak de heimdall, `preferred_username = anonymouser`, ~8 h):
se entrega a cualquier visitante sin login. El token se pide sin cuerpo ni `user_key`, con solo
`Accept: application/json` y nuestro `User-Agent`; responde un envelope `{success, trace, payload,
errores}` con `payload.access_token` y `payload.expires_in` (28800) [V]. Basta uno por corrida.
[I] El `{id}` de `listar` es el mismo `documentos[].id` de la API v2: si el SELECT de Boris lo
confirma, el job usa los ids guardados y no llama a `listar` (`ANEXOS_USAR_LISTAR=false`).

Reglas que cumple:
- Credenciales SOLO desde el entorno (`MP_ADJUNTOS_USER_KEY`, `MP_ADJUNTOS_HEADERS_EXTRA`) y
  enmascaradas en el logger (regla 1). El token vive solo en memoria (nunca a log, disco ni
  Postgres) y se renueva al vencer; si no se obtiene, la corrida se corta.
- ≤ 1 req/s con jitter (`RateLimiter` propio; NO usa el ticket ni la cuota de la API) y cada
  request (token o descarga) pasa antes por `al_pedir`: ahí el job cuenta el tope diario.
- Cliente identificado (`User-Agent` propio, sin datos personales), sin cookies ni sesión, sin
  rotar headers, IP ni cuentas. Las credenciales nunca salen hacia otro host (redirects).
- Nunca parsea HTML: una respuesta `text/html` se descarta (`no_archivo`). Tope de 10 MB.
- 403 o 429 → :class:`DescargaBloqueada` (quien llama corta y persiste una pausa de 60 min);
  504 o timeout → :class:`DescargaTransitoria` tras enfriar 60 s (regla 3). Un 401 renueva el
  token UNA vez; si se repite, :class:`TokenNoDisponible` y se corta.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from app.clients.base import RateLimiter
from app.core.logging import enmascarar_tambien, get_logger

_log = get_logger(__name__)

# [V] consola de Boris, 6-nov (CA de prueba con un PNG): la descarga es un GET a esta ruta.
URL_BASE = "https://adjunto.mercadopublico.cl/adjunto-compra-agil/v1/adjuntos-compra-agil"
URL_DESCARGAR = URL_BASE + "/descargar/{id}"
URL_LISTAR = URL_BASE + "/listar/{codigo}"
_RE_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# [V] paso 0 de Boris (7-nov): la ficha pide el token anónimo con un GET sin payload ni user_key.
URL_TOKEN: str | None = "https://servicios-prd.mercadopublico.cl/v1/auth/publico"
# Claves en las que puede venir el JWT si la respuesta es JSON (la forma exacta la muestra la sonda).
CLAVES_TOKEN = ("access_token", "token", "accessToken", "jwt")

USER_AGENT = "Rastrillo/1.0 (lector de adjuntos publicos de Compra Agil; uso interno, 1 req/s)"
MAX_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 2
ENFRIAMIENTO_S = 60.0
TIMEOUT_S = 30.0
MARGEN_TOKEN_S = 60.0  # se renueva si le queda menos que esto
VIDA_TOKEN_DEFECTO_S = 300.0  # si la respuesta no informa vencimiento


class DescargaBloqueada(Exception):
    """403 o 429 del servicio: se corta la corrida y se persiste una pausa (regla 9)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class DescargaTransitoria(Exception):
    """504 o timeout: ya se enfrió 60 s; el archivo se intentará en otra corrida."""


class TokenNoDisponible(Exception):
    """No se obtuvo el token anónimo (o el servicio lo rechazó dos veces): se corta la corrida."""


@dataclass(frozen=True)
class Descarga:
    data: bytes | None
    # None si llegó el archivo; si no: no_archivo (HTML) | muy_grande | http_<status> |
    # demasiados_redirects | error_red
    motivo: str | None = None


@dataclass(frozen=True)
class Listado:
    """Adjuntos de una CA según `listar`: (id, nombre). `archivos` None si no se pudo leer."""

    archivos: list[tuple[str, str]] | None
    motivo: str | None = None  # http_<status> | no_json | error_red


def leer_listado(cuerpo: Any) -> list[tuple[str, str]] | None:
    """`payload.files[]` (o `files[]`) → [(id, nombreArchivo)]; None si la forma no calza."""
    if not isinstance(cuerpo, dict):
        return None
    payload = cuerpo.get("payload")
    files = (payload or {}).get("files") if isinstance(payload, dict) else cuerpo.get("files")
    if not isinstance(files, list):
        return None
    salida: list[tuple[str, str]] = []
    for f in files:
        if isinstance(f, dict) and str(f.get("id") or "").strip():
            nombre = f.get("nombreArchivo") or f.get("nombre") or ""
            salida.append((str(f["id"]).strip()[:100], str(nombre)[:200]))
    return salida


def id_para_url(doc_id: str) -> str:
    """La ficha usa los UUID en mayúsculas [V, paso 0]; uno guardado en minúsculas se normaliza."""
    doc_id = doc_id.strip()
    return doc_id.upper() if _RE_UUID.fullmatch(doc_id) else doc_id


def headers_extra(texto: str) -> dict[str, str] | None:
    """`MP_ADJUNTOS_HEADERS_EXTRA` (JSON objeto de strings) → dict; None si no es válido."""
    if not texto.strip():
        return {}
    try:
        datos = json.loads(texto)
    except ValueError:
        return None
    if not isinstance(datos, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in datos.items()
    ):
        return None
    return datos


def _vence_del_jwt(token: str) -> float | None:
    """`exp` del payload del JWT (sin verificar la firma: solo para saber cuándo renovarlo)."""
    partes = token.split(".")
    if len(partes) != 3:
        return None
    try:
        crudo = partes[1] + "=" * (-len(partes[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(crudo)).get("exp")
    except (ValueError, AttributeError):
        return None
    return float(exp) if isinstance(exp, int | float) else None


def _leer_token(r: httpx.Response) -> tuple[str | None, Any]:
    """(JWT, expires_in) de la respuesta del token: JSON con el token en una de `CLAVES_TOKEN`
    (también dentro de `payload`/`data`) o el JWT como texto plano. Nunca HTML."""
    tipo = r.headers.get("Content-Type", "").lower()
    if "text/html" in tipo:
        return None, None
    try:
        cuerpo = r.json()
    except ValueError:
        texto = r.text.strip().strip('"')
        return (texto if texto.count(".") == 2 and " " not in texto else None), None
    if isinstance(cuerpo, str):
        return (cuerpo if cuerpo.count(".") == 2 else None), None
    if not isinstance(cuerpo, dict):
        return None, None
    for nivel in (cuerpo, cuerpo.get("payload"), cuerpo.get("data")):
        if isinstance(nivel, dict):
            for clave in CLAVES_TOKEN:
                valor = nivel.get(clave)
                if isinstance(valor, str) and valor:
                    return valor, nivel.get("expires_in", cuerpo.get("expires_in"))
    return None, None


class DocumentosClient:
    def __init__(
        self,
        *,
        user_key: str = "",
        extra: dict[str, str] | None = None,
        http: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        dormir: Callable[[float], None] = time.sleep,
        reloj: Callable[[], float] = time.time,
        url_token: str | None = None,
        url_descargar: str = URL_DESCARGAR,
        url_listar: str = URL_LISTAR,
    ) -> None:
        self._user_key = user_key.strip()
        self._extra = dict(extra or {})
        self._url_token = url_token if url_token is not None else URL_TOKEN
        self._url_descargar = url_descargar
        self._url_listar = url_listar
        self._http = http or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        self._limiter = limiter or RateLimiter(1.0)
        self._dormir = dormir
        self._reloj = reloj
        self._token: str | None = None
        self._vence = 0.0
        # Lo fija quien llama: se ejecuta ANTES de cada request (cuenta el tope; puede lanzar).
        self.al_pedir: Callable[[], None] = lambda: None
        for v in (self._user_key, *self._extra.values()):
            enmascarar_tambien(v)

    @classmethod
    def desde_settings(cls, settings: Any, **kw: Any) -> DocumentosClient:
        extra = headers_extra(settings.mp_adjuntos_headers_extra or "")
        if extra is None:
            _log.warning("MP_ADJUNTOS_HEADERS_EXTRA no es un JSON de strings: se ignora")
            return cls(user_key="", **kw)  # no configurado: no se pide nada
        return cls(user_key=settings.mp_adjuntos_user_key or "", extra=extra, **kw)

    @property
    def configurado(self) -> bool:
        return bool(self._user_key and self._url_token)

    # --- token anónimo --------------------------------------------------------------------------

    def _token_vigente(self) -> str:
        if self._token is None or self._reloj() >= self._vence - MARGEN_TOKEN_S:
            self._pedir_token()
        assert self._token is not None
        return self._token

    def _pedir_token(self) -> None:
        self._token = None
        if not self._url_token:
            raise TokenNoDisponible("sin URL de token")
        self.al_pedir()
        self._limiter.acquire()
        self._http.cookies.clear()
        try:
            r = self._http.get(
                self._url_token, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
        except httpx.TimeoutException:
            self._enfriar("timeout del token")
            raise TokenNoDisponible("timeout") from None
        except httpx.HTTPError as exc:
            raise TokenNoDisponible(type(exc).__name__) from None
        finally:
            self._http.cookies.clear()
            self._limiter.tras_respuesta()
        if r.status_code in (403, 429):
            raise DescargaBloqueada(r.status_code)
        if r.status_code == 504:
            self._enfriar("504 del token")
            raise TokenNoDisponible("http_504")
        if r.status_code != 200:
            raise TokenNoDisponible(f"http_{r.status_code}")
        token, expira = _leer_token(r)
        if not token:
            raise TokenNoDisponible("respuesta sin token")
        enmascarar_tambien(token)
        ahora = self._reloj()
        if isinstance(expira, int | float) and expira > 0:
            self._vence = ahora + float(expira)
        else:
            self._vence = _vence_del_jwt(token) or ahora + VIDA_TOKEN_DEFECTO_S
        self._token = token

    # --- descarga -------------------------------------------------------------------------------

    def _headers(self, url: str, token: str) -> dict[str, str]:
        base = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if urlsplit(url).netloc != urlsplit(self._url_descargar).netloc:
            return base  # las credenciales nunca salen hacia otro host
        return {**base, **self._extra, "user_key": self._user_key, "Authorization": f"Bearer {token}"}

    def descargar_adjunto(self, codigo: str, doc_id: str) -> Descarga:
        """El archivo del adjunto `doc_id` (de la CA `codigo`). Ver el docstring del módulo."""
        if not self.configurado:
            raise RuntimeError("Servicio de adjuntos sin configurar (MP_ADJUNTOS_USER_KEY o token)")
        for intento in range(2):
            token = self._token_vigente()
            url = self._url_descargar.format(id=quote(id_para_url(doc_id), safe=""))
            d = self._descargar(url, token)
            if d.motivo != "http_401":
                return d
            self._token = None  # vencido o revocado: se pide otro UNA vez
            if intento:
                raise TokenNoDisponible("http_401 con token nuevo")
        raise AssertionError("inalcanzable")

    def listar(self, codigo: str) -> Listado:
        """Los adjuntos de la CA según el servicio (1 request, cuenta en el tope). 403/429 →
        :class:`DescargaBloqueada`; 504/timeout → :class:`DescargaTransitoria`; un 401 renueva el
        token una vez. No sigue redirects: un listado no los necesita."""
        if not self.configurado:
            raise RuntimeError("Servicio de adjuntos sin configurar (MP_ADJUNTOS_USER_KEY o token)")
        url = self._url_listar.format(codigo=quote(codigo, safe=""))
        for intento in range(2):
            token = self._token_vigente()
            self.al_pedir()
            self._limiter.acquire()
            self._http.cookies.clear()
            try:
                r = self._http.get(url, headers={**self._headers(url, token), "Accept": "application/json"})
            except httpx.TimeoutException:
                self._enfriar("timeout de listar")
                raise DescargaTransitoria("timeout") from None
            except httpx.HTTPError as exc:
                _log.warning("Adjuntos: error de red al listar (%s)", type(exc).__name__)
                return Listado(None, "error_red")
            finally:
                self._http.cookies.clear()
                self._limiter.tras_respuesta()
            if r.status_code in (403, 429):
                raise DescargaBloqueada(r.status_code)
            if r.status_code == 504:
                self._enfriar("504 de listar")
                raise DescargaTransitoria("504")
            if r.status_code == 401:
                self._token = None
                if intento:
                    raise TokenNoDisponible("http_401 con token nuevo")
                continue
            if r.status_code != 200:
                return Listado(None, f"http_{r.status_code}")
            if "text/html" in r.headers.get("Content-Type", "").lower():
                return Listado(None, "no_json")  # sin parsear HTML (regla 9)
            try:
                archivos = leer_listado(r.json())
            except ValueError:
                archivos = None
            return Listado(archivos, None if archivos is not None else "no_json")
        raise AssertionError("inalcanzable")

    def _descargar(self, url: str, token: str) -> Descarga:
        for _salto in range(MAX_REDIRECTS + 1):
            self.al_pedir()
            self._limiter.acquire()
            self._http.cookies.clear()  # sin cookies ni sesión (regla 9)
            try:
                with self._http.stream("GET", url, headers=self._headers(url, token)) as r:
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
                _log.warning("Adjuntos: error de red (%s)", type(exc).__name__)
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
        _log.warning("Adjuntos: %s, enfrío %.0f s", causa, ENFRIAMIENTO_S)
        self._dormir(ENFRIAMIENTO_S)
