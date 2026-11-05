"""La suite nunca ve la key real de OpenRouter del .env (regla 1)."""

from __future__ import annotations

from app.core.settings import Settings


def test_settings_de_fixtures_sin_key_de_openrouter() -> None:
    s = Settings()  # type: ignore[call-arg]  # lee el .env real, como los fixtures
    assert s.openrouter_api_key == ""
    assert s.llm_modelo_extraccion == ""
