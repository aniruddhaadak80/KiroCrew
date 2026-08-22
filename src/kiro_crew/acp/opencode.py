"""Resolution for the OpenCode ACP adapter.

OpenCode serves ACP from its own binary via ``opencode acp``, the same shape as
goose: no separate npm adapter and no Node floor. The registry lists it as a
binary distribution. Privileged tools on that path fire
``session/request_permission``, which is what makes the backend ROUTED.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

OPENCODE_BIN = "opencode"
OPENCODE_ACP_SUBCOMMAND = "acp"

_UNRESOLVED: object = object()
_argv_cache: object = _UNRESOLVED


def resolve_argv() -> list[str] | None:
    """Find the OpenCode binary and return the argv that starts its ACP server."""
    from kiro_crew.acp.client import _mise_which, _normalize_exe_casing, _ordered_path_matches
    from kiro_crew.env import augmented_path

    candidates: list[str] = []

    override = os.environ.get("OPENCODE_BIN")
    if override and Path(override).is_file():
        candidates.append(override)

    mise_resolved = _mise_which(OPENCODE_BIN)
    if mise_resolved:
        candidates.append(mise_resolved)

    candidates.extend(_ordered_path_matches(OPENCODE_BIN, augmented_path()))

    for candidate in candidates:
        resolved = _normalize_exe_casing(candidate)
        if not resolved:
            continue
        if not os.access(resolved, os.X_OK):
            continue
        return [resolved, OPENCODE_ACP_SUBCOMMAND]

    return None


def resolve_argv_cached() -> list[str] | None:
    """``resolve_argv`` memoised for the process. Failures are not cached."""
    global _argv_cache  # noqa: PLW0603
    if _argv_cache is _UNRESOLVED:
        resolved = resolve_argv()
        if resolved is None:
            return None
        _argv_cache = resolved
    return _argv_cache if isinstance(_argv_cache, list) else None


def missing_adapter_message() -> str:
    """What to tell an operator whose host has no OpenCode binary."""
    return (
        "opencode not found. Install OpenCode (see https://opencode.ai), or set "
        "OPENCODE_BIN to the binary's path. OpenCode serves ACP from its own "
        "binary via `opencode acp`. Then sign in with `opencode auth login`."
    )


def signin_hint() -> str:
    """OpenCode owns its provider credentials; Kiro Crew never reads them."""
    return "Sign in with `opencode auth login`."


__all__ = [
    "OPENCODE_ACP_SUBCOMMAND",
    "OPENCODE_BIN",
    "missing_adapter_message",
    "resolve_argv",
    "resolve_argv_cached",
    "signin_hint",
]
