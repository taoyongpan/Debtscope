"""Runtime configuration.

Precedence (highest wins): environment variables > ~/.debtscope/config.json
> provider presets. Configure interactively via `debtscope config`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .harness.config_store import CONFIG_PATH, PROVIDERS, FileConfig, load_config_file


def _is_local(base: str) -> bool:
    return "://127.0.0.1" in base or "://localhost" in base or "://0.0.0.0" in base


@dataclass
class Config:
    llm_enabled: bool
    provider: str
    api_base: str
    api_key: str | None
    model: str
    timeout: int
    max_retries: int
    config_path: str = CONFIG_PATH

    @classmethod
    def load(cls, use_llm: bool = True) -> "Config":
        fc: FileConfig = load_config_file()
        preset = PROVIDERS.get(fc.provider, PROVIDERS["custom"])

        api_base = (
            os.getenv("DEBTSCOPE_API_BASE")
            or os.getenv("OPENAI_API_BASE")
            or fc.api_base
            or preset["api_base"]
            or "https://api.openai.com/v1"
        ).rstrip("/")
        api_key = (
            os.getenv("DEBTSCOPE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or fc.api_key
        )
        model = os.getenv("DEBTSCOPE_MODEL") or fc.model or preset["model"]
        timeout = int(os.getenv("DEBTSCOPE_TIMEOUT", str(fc.timeout or 45)))

        enabled = bool(use_llm and api_base and (api_key or _is_local(api_base)))
        return cls(
            llm_enabled=enabled,
            provider=fc.provider,
            api_base=api_base,
            api_key=api_key,
            model=model,
            timeout=timeout,
            max_retries=1,
        )

    # backwards-compatible alias used in earlier code paths
    @classmethod
    def from_env(cls, use_llm: bool = True) -> "Config":
        return cls.load(use_llm=use_llm)
