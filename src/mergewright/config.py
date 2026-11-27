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

    # Budgets from the Phase 1 pilots (docs/plans/phase-1.md, step 14): a run needs about 10
    # steps to find and edit the code, then test runs and fixes; 25 steps ended runs mid-check.
    max_steps: int = 50  # LLM calls per run
    max_test_attempts: int = 3  # gate runs before the run gives up
    # One test run, in the loop or at the gate. A full suite of a large repository takes minutes;
    # a run that times out fails the gate whatever the patch.
    tool_timeout_s: float = 900.0
    llm_timeout_s: float = 120.0  # per LLM call attempt (the SDK retries a timeout twice)
    # Whole run. A safety stop, not the working budget: max_steps should end a run first, so a
    # result does not depend on the endpoint's speed. Room for 50 steps and 3 full gate runs.
    wall_clock_s: float = 5400.0


@dataclass(frozen=True)
class LiveSettings:
    """Settings for a live model. api_style names the wire format the server speaks."""

    model: str
    api_base: str | None
    api_key: str | None
    api_style: str = "openai"  # one of API_STYLES


# openai: any OpenAI-compatible chat server (POST <base>/chat/completions).
# bedrock-converse: Amazon Bedrock Runtime's Converse API (POST <base>/model/<id>/converse),
# one format for every Bedrock model, Anthropic's included.
API_STYLES = ("openai", "bedrock-converse")


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
    api_style = environ.get("MERGEWRIGHT_API_STYLE", "").strip() or "openai"
    if api_style not in API_STYLES:
        raise ConfigError(f"MERGEWRIGHT_API_STYLE must be one of {', '.join(API_STYLES)}")
    api_base = environ.get("MERGEWRIGHT_API_BASE", "").strip() or None
    if api_style == "bedrock-converse" and not api_base:
        raise ConfigError(
            "bedrock-converse needs MERGEWRIGHT_API_BASE, for example "
            "https://bedrock-runtime.us-east-1.amazonaws.com"
        )
    api_key = environ.get("MERGEWRIGHT_API_KEY", "").strip() or None
    # The Bedrock key is a Bearer token: never send it in clear text.
    if api_style == "bedrock-converse" and api_key and not api_base.startswith("https://"):
        raise ConfigError("bedrock-converse sends the key as a Bearer token; use an https:// base")
    return LiveSettings(model=model, api_base=api_base, api_key=api_key, api_style=api_style)


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
