"""Run limits and live-mode settings, read from the environment and an optional .env file."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Configuration is missing or invalid. The message tells the user how to fix it."""


@dataclass(frozen=True)
class Limits:
    """Hard bounds on one run."""

    max_steps: int = 25  # LLM calls per run
    max_test_attempts: int = 3  # test runs before the run gives up
    tool_timeout_s: float = 60.0  # per tool call, enforced for run_tests
    llm_timeout_s: float = 120.0  # per LLM call
    # Whole run. A safety stop, not the working budget: max_steps should end a run first, so a
    # result does not depend on the endpoint's speed (Phase 1 pilot: 300 s cut runs at step 15).
    wall_clock_s: float = 1800.0


@dataclass(frozen=True)
class LiveSettings:
    """Settings for a live model on an OpenAI-compatible chat server."""

    model: str
    api_base: str | None
    api_key: str | None


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens, as the provider bills them."""

    input_per_mtok: float
    output_per_mtok: float

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.input_per_mtok + tokens_out * self.output_per_mtok) / 1e6


def load_dotenv(path: Path, environ: MutableMapping[str, str]) -> None:
    """Load KEY=VALUE lines from path into environ. Existing values win. A missing file is fine."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in environ:
            environ[key] = value


def load_live_settings(environ: Mapping[str, str] = os.environ) -> LiveSettings:
    """Read live-mode settings. Fail fast with a clear message when the model is not set."""
    model = environ.get("MERGEWRIGHT_MODEL", "").strip()
    if not model:
        raise ConfigError(
            "Live mode needs MERGEWRIGHT_MODEL. Copy .env.example to .env and set it, "
            "or use --llm replay, which needs no model or key."
        )
    return LiveSettings(
        model=model,
        api_base=environ.get("MERGEWRIGHT_API_BASE", "").strip() or None,
        api_key=environ.get("MERGEWRIGHT_API_KEY", "").strip() or None,
    )


def load_price(environ: Mapping[str, str] = os.environ) -> Price | None:
    """The configured price, or None: a cost is never guessed."""
    names = ("MERGEWRIGHT_PRICE_IN_PER_MTOK", "MERGEWRIGHT_PRICE_OUT_PER_MTOK")
    values = [environ.get(name, "").strip() for name in names]
    if not any(values):
        return None
    try:
        return Price(*(float(value) for value in values))
    except ValueError as exc:
        raise ConfigError(f"set both {names[0]} and {names[1]} as numbers (US$)") from exc
