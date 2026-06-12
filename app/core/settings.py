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
    database_url: str = Field(..., description="URL de conexión a Postgres (Neon); sslmode=require")
    secret_key: str = Field(..., description="Clave para firmar cookies de sesión")
    jobs_token: str = Field(..., description="Token para proteger POST /api/jobs/run")

    # --- Rate limit y presupuestos ---
    rate_limit_rps: float = Field(default=1.0, description="Solicitudes por segundo hacia la API")
    api_daily_budget: int = Field(default=9000, description="Presupuesto máximo de requests/día")
    email_daily_limit: int = Field(default=250, description="Tope de correos por día")

    # --- SMTP (opcionales) ---
    smtp_host: str = Field(default="", description="Host SMTP")
    smtp_port: int = Field(default=587, description="Puerto SMTP")
    smtp_user: str = Field(default="", description="Usuario SMTP")
    smtp_password: str = Field(default="", description="Contraseña SMTP")
    smtp_from: str = Field(default="", description="Dirección remitente")

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


def get_settings() -> "Settings":
    return Settings()  # type: ignore[call-arg]
