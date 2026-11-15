"""Content-addressed message store for the spike: one JSON file per message, named by its hash.

A stand-in for the Phase 3 Postgres message store (spec Section 3, "Messages"). Graph state and
Temporal payloads carry the hashes only. All workers on one host share the directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class MessageStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def put(self, message: dict[str, Any]) -> str:
        """Store a message and return its hash. The same message always gets the same hash."""
        body = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(body).hexdigest()
        path = self._path(digest)
        if not path.exists():
            # Write then rename, so a reader never sees a half-written file after a crash.
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
        return digest

    def get(self, digest: str) -> dict[str, Any]:
        """Return the message for a hash. Raises KeyError if it is not stored."""
        path = self._path(digest)
        if not path.is_file():
            raise KeyError(digest)
        return json.loads(path.read_bytes())

    def put_all(self, messages: list[dict[str, Any]]) -> list[str]:
        return [self.put(message) for message in messages]

    def get_all(self, digests: list[str]) -> list[dict[str, Any]]:
        return [self.get(digest) for digest in digests]

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise KeyError(digest)  # not a sha256 hex digest; never build a path from it
        return self.root / f"{digest}.json"
