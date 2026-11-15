"""Where the spike connects: the local dev server by default, or a Temporal CLI profile.

    TEMPORAL_PROFILE=cloud python -m spikes.runtime.worker --queues service,llm

Settings come from the Temporal CLI's config file (what `temporal --profile cloud config set`
writes) and the standard TEMPORAL_* environment variables, through the SDK's envconfig. The
key never appears in this code, in logs or in the repo. A profile with an API key turns on TLS.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from temporalio.client import Client
from temporalio.envconfig import ClientConfig

LOCAL = {"target_host": "localhost:7233", "namespace": "default"}


def connect_config(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Client.connect keyword arguments for the selected profile (TEMPORAL_PROFILE)."""
    loaded = ClientConfig.load_client_connect_config(
        override_env_vars=dict(os.environ if env is None else env)
    )
    return {**LOCAL, **{k: v for k, v in loaded.items() if v is not None}}


def describe(config: Mapping[str, Any]) -> str:
    """A one-line description for logs. Never includes the API key."""
    parts = [config["target_host"], f"namespace={config['namespace']}"]
    parts += [flag for flag, key in (("tls", "tls"), ("api-key", "api_key")) if config.get(key)]
    return " ".join(parts)


async def connect(env: Mapping[str, str] | None = None) -> Client:
    return await Client.connect(**connect_config(env))
