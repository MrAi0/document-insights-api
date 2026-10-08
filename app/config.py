from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    log_json: bool = True

    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "document_insights"
    mongo_timeout_ms: int = 3000

    redis_url: str = "redis://localhost:6379/0"
    # Kept short on purpose. If Redis is down we want to fall back quickly,
    # not hang every request for seconds.
    redis_timeout_seconds: float = 0.5

    max_active_jobs_per_user: int = Field(default=3, ge=1)
    # Safety net for the rate limiter. If a worker dies without releasing a slot,
    # the slot is dropped once it's this old instead of blocking the user forever.
    active_job_ttl_seconds: int = Field(default=60 * 60, ge=1)
    # How long a summary stays in Redis. Older summaries are still found in Mongo
    # (and put back in Redis), so this decides what's kept in memory, not what's reused.
    summary_cache_ttl_seconds: int = Field(default=24 * 60 * 60, ge=1)

    worker_concurrency: int = Field(default=4, ge=1)
    # An idle worker sleeps until a new document is announced over Redis or the
    # next retry is due. This caps the sleep, in case a wake-up got lost.
    worker_max_idle_seconds: float = Field(default=30, gt=0)
    # Only used while Redis is down, when there are no wake-ups and we fall back to polling.
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    job_lease_seconds: int = Field(default=60, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    retry_backoff_seconds: float = Field(default=5.0, ge=0)

    processing_min_seconds: float = Field(default=10, ge=0)
    processing_max_seconds: float = Field(default=30, ge=0)
    failure_rate: float = Field(default=0.1, ge=0, le=1)

    @model_validator(mode="after")
    def check_timings(self) -> "Settings":
        if self.processing_min_seconds > self.processing_max_seconds:
            raise ValueError("PROCESSING_MIN_SECONDS can't be greater than PROCESSING_MAX_SECONDS")
        if self.job_lease_seconds <= self.processing_max_seconds:
            # otherwise a slow but healthy job looks abandoned and gets picked up twice
            raise ValueError("JOB_LEASE_SECONDS must be longer than PROCESSING_MAX_SECONDS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
