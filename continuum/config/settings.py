"""Application settings.

CLAUDE.md §10: "No magic thresholds. Every threshold is a named setting with a comment
explaining how it was chosen." This module is the sole home for those thresholds. Nothing
outside this file should hardcode a numeric business rule.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Loaded from environment variables / .env. See .env.example for the full key list."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["local", "ci", "staging", "production"] = "local"
    log_level: str = "INFO"

    # --- Postgres ---
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_db: str = "continuum"
    postgres_user: str = "continuum"
    postgres_password: str = "continuum_local_dev"

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379

    # --- LiteLLM gateway (CLAUDE.md I7: the only door to any model provider) ---
    litellm_base_url: str = "http://localhost:4000"
    litellm_api_key: str = ""

    # --- Data build (Phase 1) ---
    data_seed: int = 20260101

    # --- Telemetry ---
    otel_exporter_otlp_endpoint: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    # -------------------------------------------------------------------
    # Named thresholds. Each carries a comment on how it was chosen, per §10.
    # Values here are Phase-0 placeholders pending real tuning in the phase
    # that owns them; the comment says so explicitly rather than pretending
    # to a precision the number doesn't have yet.
    # -------------------------------------------------------------------

    # Screening band floor/ceiling (Phase 3). Candidates scoring below the floor are
    # auto-rejected, above the ceiling auto-confirmed; the middle is adjudicated.
    # Placeholder pending calibration against the hard-name-pair set in Phase 3 —
    # I5 requires the floor be set to guarantee 100% recall on that set, not by feel.
    screening_auto_reject_floor: float = 0.15
    screening_auto_confirm_ceiling: float = 0.92

    # Candidate-generation floor (Phase 3) — deliberately looser than
    # screening_auto_reject_floor (I5: candidate generation must be over-inclusive
    # relative to band logic, never the other way round, or anything scoring between
    # this floor and the auto-reject floor would never even become a candidate).
    # Placeholder pending a real run's query-cost profile against the trigram index.
    screening_candidate_min_similarity: float = 0.10

    # Delinquency state machine (Phase 1). Standard US consumer-lending DPD buckets.
    delinquency_buckets_days: tuple[int, ...] = (30, 60, 90)

    # Historical contact-policy simulation (Phase 1). Matches CLAUDE.md §8: "contact at
    # 30 DPD weighted by exposure." Placeholder cap/window pending the frequency-cap
    # values the guard will enforce at runtime (Phase 6) — kept identical on purpose so
    # the simulated history and the live guard never diverge.
    contact_policy_trigger_dpd: int = 30
    contact_policy_max_contacts_per_week: int = 2
    contact_policy_quiet_hours_start: int = 21  # local hour, 24h clock
    contact_policy_quiet_hours_end: int = 8

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
