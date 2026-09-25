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
    # Empty base url = LiteLLM calls the provider directly (e.g. the DeepSeek API); set it to
    # point at a self-hosted LiteLLM proxy instead. The model id (continuum.config.models)
    # decides the provider either way — swapping is config, not code (I7).
    litellm_base_url: str = ""
    litellm_api_key: str = ""
    deepseek_api_key: str = ""

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

    # Screening band floor/ceiling (Phase 3). At or below the floor a candidate is
    # auto-rejected; at or above the ceiling *with corroborating date of birth* it is
    # auto-confirmed; everything else is adjudicated.
    # Floor: calibrated on eval/golden/screening_hard_pairs.csv (I5) — the lowest name
    # similarity of any true match there is 0.41 (nickname pair hp-015), so 0.35 keeps
    # recall at 1.0 with a 0.06 margin and cuts adjudicator load from ~47k to ~11k
    # candidates on the 5,000-person dataset (floor 0.15 placeholder -> 0.35). Caveat: only
    # 18 labelled pairs, so re-check whenever the golden set grows.
    screening_auto_reject_floor: float = 0.35
    screening_auto_confirm_ceiling: float = 0.92

    # Candidate-generation floor (Phase 3) — deliberately looser than
    # screening_auto_reject_floor (I5: candidate generation must be over-inclusive
    # relative to band logic, so everything in the auto-reject band is still generated and
    # recorded as a candidate before being rejected). 0.30 = 0.05 below the reject floor.
    screening_candidate_min_similarity: float = 0.30

    # A date of birth "corroborates" a name match if it is equal, differs by at most this
    # many days (typo / off-by-a-day entry), or has day and month swapped. Golden set
    # hp-011 (1 day) and hp-013 (day/month swap) are true matches this must accept.
    screening_dob_near_miss_days: int = 31

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
