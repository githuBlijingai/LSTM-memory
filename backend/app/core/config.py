from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str | None = None
    redis_url: str | None = None

    oidc_jwks_url: str | None = None
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_algorithms: str = "RS256"
    oidc_jwks_cache_seconds: int = Field(default=300, ge=30)
    dev_auth_enabled: bool = False

    embedding_provider: Literal["local", "remote"] | None = None
    embedding_model_name: str | None = None
    embedding_model_version: str | None = None
    embedding_api_base: str | None = None
    embedding_api_key: SecretStr | None = None
    reranker_enabled: bool = True
    reranker_provider: Literal["local", "remote"] | None = None
    reranker_model_name: str | None = None
    reranker_model_version: str | None = None
    reranker_api_base: str | None = None
    reranker_api_key: SecretStr | None = None
    event_model_uri: str | None = None
    event_model_version: str | None = None
    model_device: Literal["cpu", "cuda"] | None = None
    local_model_min_vram_gb: float = Field(default=8.0, gt=0)
    allow_cpu_models: bool = False
    model_http_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    bootstrap_non_interactive: bool | None = None

    search_candidate_k: int = Field(default=100, ge=1, le=1000)
    search_top_n: int = Field(default=10, ge=1, le=50)
    search_min_similarity: float = Field(default=0.15, ge=-1, le=1)
    memory_weight_cap: float = Field(default=2.5, gt=0)
    familiarity_daily_cap: float = Field(default=0.3, gt=0, le=0.3)
    replay_max_count: int = Field(default=2, ge=0, le=2)
    query_log_retention_days: int = Field(default=90, ge=1)
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "memory-system-api"
    otel_traces_sampler_ratio: float = Field(default=0.1, ge=0, le=1)

    # 入口防护（设计 9.8 / 9.9）。
    security_headers_enabled: bool = True
    content_security_policy: str = (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    hsts_max_age_seconds: int = Field(default=0, ge=0)
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024)
    rate_limit_enabled: bool = True
    rate_limit_search_per_minute: int = Field(default=60, ge=1)
    rate_limit_feedback_per_minute: int = Field(default=120, ge=1)
    rate_limit_sync_per_hour: int = Field(default=10, ge=1)
    rate_limit_redis_prefix: str = "rate_limit"

    # 观测与就绪检查。
    metrics_enabled: bool = True
    metrics_token: SecretStr | None = None
    readiness_model_check_enabled: bool = True
    readiness_model_cache_seconds: int = Field(default=30, ge=1)
    readiness_model_timeout_seconds: float = Field(default=2.0, gt=0, le=5)

    # 设计 4.1 模型与记忆默认参数，禁止在业务代码中散落魔法数。
    event_window_size: int = Field(default=16, ge=2, le=128)
    base_embedding_dim: int = Field(default=1024, ge=1)
    lstm_input_dim: int = Field(default=256, ge=1)
    lstm_hidden_dim: int = Field(default=256, ge=1)
    lstm_num_layers: int = Field(default=1, ge=1, le=4)
    decay_lambda: float = Field(default=0.01, gt=0)
    familiarity_gamma: float = Field(default=0.003, gt=0)
    familiarity_max: float = Field(default=2.0, gt=0)
    familiarity_step: float = Field(default=0.08, gt=0, le=0.08)
    familiarity_beta: float = Field(default=0.5, ge=0, le=1)
    wiki_familiarity_alpha: float = Field(default=0.6, ge=0, le=1)
    delete_threshold: float = Field(default=0.1, ge=0, le=1)
    replay_threshold: float = Field(default=1.0, ge=0, le=2)
    new_memory_protect_hours: int = Field(default=48, ge=0)
    archive_retention_days: int = Field(default=30, ge=0)
    feedback_dedup_minutes: int = Field(default=10, ge=0)

    # 检索命中强化（闭环：被检索到也算一次温和的熟悉度提升）。
    # 增量 = step × (1 - 当前熟悉度)，必须小于反馈步长，保证只逼近 1.0 而不越过。
    retrieval_hit_familiarity_step: float = Field(default=0.02, gt=0, le=0.08)
    retrieval_hit_dedup_minutes: int = Field(default=60, ge=1, le=1440)

    # 工作记忆与维护任务参数。
    working_memory_token_budget: int = Field(default=8000, ge=1)
    working_memory_reserved_tokens: int = Field(default=2000, ge=0)
    working_memory_summary_ratio: float = Field(default=0.8, gt=0, le=1)
    working_memory_verbatim_items: int = Field(default=20, ge=0)
    working_memory_important_threshold: float = Field(default=0.7, ge=0, le=1)
    working_session_ttl_hours: int = Field(default=24, ge=1)
    maintenance_utc_hour: int = Field(default=2, ge=0, le=23)
    maintenance_utc_minute: int = Field(default=0, ge=0, le=59)
    maintenance_batch_size: int = Field(default=500, ge=1, le=5000)

    # Wiki 同步与分块参数（设计 3.5）。
    wiki_chunk_min_chars: int = Field(default=200, ge=50, le=2000)
    wiki_chunk_max_chars: int = Field(default=500, ge=100, le=4000)
    wiki_chunk_overlap_chars: int = Field(default=50, ge=0, le=500)
    wiki_embed_batch_size: int = Field(default=100, ge=1, le=500)
    wiki_package_max_bytes: int = Field(default=536_870_912, ge=1)

    @field_validator("metrics_token", mode="before")
    @classmethod
    def empty_metrics_token_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def reject_dev_auth_in_production(self) -> Settings:
        if self.environment == "production" and self.dev_auth_enabled:
            raise ValueError("DEV_AUTH_ENABLED must be false in production")
        if self.wiki_chunk_max_chars <= self.wiki_chunk_min_chars:
            raise ValueError("WIKI_CHUNK_MAX_CHARS must be greater than WIKI_CHUNK_MIN_CHARS")
        if self.wiki_chunk_overlap_chars >= self.wiki_chunk_max_chars:
            raise ValueError("WIKI_CHUNK_OVERLAP_CHARS must be less than WIKI_CHUNK_MAX_CHARS")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def non_interactive(self) -> bool:
        if self.bootstrap_non_interactive is not None:
            return self.bootstrap_non_interactive
        return self.is_production

    @property
    def oidc_algorithm_list(self) -> list[str]:
        return [item.strip() for item in self.oidc_algorithms.split(",") if item.strip()]

    def missing_configuration(self, *, include_auth: bool = True) -> list[str]:
        missing: list[str] = []
        for name in ("database_url", "redis_url", "event_model_uri", "event_model_version"):
            if not getattr(self, name):
                missing.append(name.upper())
        if include_auth and self.is_production:
            for name in ("oidc_jwks_url", "oidc_issuer", "oidc_audience"):
                if not getattr(self, name):
                    missing.append(name.upper())
        missing.extend(self._provider_missing("embedding"))
        if self.reranker_enabled:
            missing.extend(self._provider_missing("reranker"))
        if (
            self.embedding_provider == "local"
            and self.model_device == "cpu"
            and not self.allow_cpu_models
        ):
            missing.append("ALLOW_CPU_MODELS=true (local CPU models are development-only)")
        if self.is_production and self.allow_cpu_models and self.model_device == "cpu":
            missing.append("Local CPU BGE/Reranker is not allowed in production")
        return sorted(set(missing))

    def _provider_missing(self, prefix: str) -> list[str]:
        provider = getattr(self, f"{prefix}_provider")
        upper = prefix.upper()
        if provider is None:
            return [f"{upper}_PROVIDER"]
        missing = []
        if not getattr(self, f"{prefix}_model_name"):
            missing.append(f"{upper}_MODEL_NAME")
        if not getattr(self, f"{prefix}_model_version") and prefix == "embedding":
            missing.append(f"{upper}_MODEL_VERSION")
        if provider == "remote":
            if not getattr(self, f"{prefix}_api_base"):
                missing.append(f"{upper}_API_BASE")
            if not getattr(self, f"{prefix}_api_key"):
                missing.append(f"{upper}_API_KEY")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
