from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Secretos obligatorios ---
    mp_ticket: str = Field(..., description="Ticket de acceso a la API de Mercado Público")
    database_url: str = Field(..., description="URL de conexión a Postgres (Neon) branch dev; sslmode=require")
    secret_key: str = Field(..., description="Clave para firmar cookies de sesión")
    jobs_token: str = Field(..., description="Token para proteger POST /api/jobs/run")

    # --- Branch production de Neon (solo referencia; la usa Render) ---
    database_url_prod: str = Field(default="", description="URL Neon branch production — solo para referencia, no se usa en runtime")

    # --- Rate limit y presupuestos ---
    rate_limit_rps: float = Field(default=1.0, description="Solicitudes por segundo hacia la API")
    api_daily_budget: int = Field(default=9000, description="Presupuesto máximo de requests/día")
    email_daily_limit: int = Field(default=250, description="Tope de correos por día")

    # --- Ingesta por lotes (regla 12: nunca un día completo en memoria) ---
    ingest_batch_size: int = Field(
        default=200, description="Tamaño de lote para commits incrementales en la ingesta"
    )

    # --- Brevo REST API (preferido en producción; Render bloquea TCP/SMTP) ---
    brevo_api_key: str = Field(default="", description="API key de Brevo para envío de correos vía HTTPS")

    # --- SMTP (deprecated: solo para desarrollo local sin Brevo configurado) ---
    smtp_host: str = Field(default="", description="Host SMTP")
    smtp_port: int = Field(default=587, description="Puerto SMTP")
    smtp_user: str = Field(default="", description="Usuario SMTP")
    smtp_password: str = Field(default="", description="Contraseña SMTP")
    smtp_from: str = Field(default="", description="Dirección remitente")

    # --- Alertas email ---
    digest_hour: int = Field(default=8, description="Hora Chile para envío del digest diario (0–23)")

    # --- Admin inicial (solo para seed; no usar en runtime) ---
    admin_email: str = Field(default="", description="Email del administrador inicial")
    admin_password: str = Field(
        default="", description="Contraseña del administrador inicial (solo seed)"
    )

    # --- Tasas de cambio configurables ---
    tasa_uf: float = Field(default=37000.0, description="Valor de la UF en CLP")
    tasa_utm: float = Field(default=65000.0, description="Valor de la UTM en CLP")
    tasa_usd: float = Field(default=950.0, description="Tipo de cambio USD a CLP")
    tasa_eur: float = Field(default=1030.0, description="Tipo de cambio EUR a CLP")

    # --- Pre-filtro de ingesta (opcional) ---
    prefilter_keywords: list[str] = Field(
        default_factory=list,
        description='Keywords amplias para pre-filtrar licitaciones antes de pedir detalle. JSON: \'["word1","word2"]\'',
    )

    # --- Datos abiertos (F-rubros): ítems UNSPSC sin gastar cuota de la API ---
    datos_abiertos_habilitado: bool = Field(
        default=True, description="Habilita la ingesta de licitacion_items desde datos abiertos"
    )
    datos_abiertos_base_url: str = Field(
        default="https://transparenciachc.blob.core.windows.net",
        description="Base URL del Azure Blob público de datos abiertos de ChileCompra",
    )


def get_settings() -> "Settings":
    return Settings()  # type: ignore[call-arg]
