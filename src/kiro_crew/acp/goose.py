"""Resolution for the goose ACP adapter.

goose differs from the claude and codex adapters in a way that shapes this whole
module: it serves ACP from its OWN binary via the ``goose acp`` subcommand, so
there is no separate npm package to find and no Node runtime to pair. The thing to
resolve is goose itself, and the argv is always ``[<goose>, "acp"]``.

That also means the Node-major floor the other two ladders enforce
(``_MIN_ADAPTER_NODE_MAJOR``) does not apply here — goose ships as a native binary,
so there is no interpreter whose version could silently mismatch.

Contrast with the codex module's warning about ``codex acp``: for the Codex CLI the
``acp`` subcommand does NOT serve the protocol and burns a billed turn instead. For
goose it genuinely is the ACP server — the goose docs describe the client running
``goose acp`` over stdio. The shipped binary contains ACP filesystem and terminal method names, but Kiro
Crew does not advertise those client methods. Privileged tools still go through
``session/request_permission``, which is :data:`~kiro_crew.acp.backends.Routing.PERMISSION_REQUEST`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

GOOSE_BIN = "goose"

#: The subcommand that starts goose's ACP server over stdio.
GOOSE_ACP_SUBCOMMAND = "acp"

# Sentinel distinguishing "never resolved" from "resolved to nothing", so a failed
# resolution is retried on the next spawn instead of being cached as absent.
_UNRESOLVED: object = object()
_argv_cache: object = _UNRESOLVED


def resolve_argv() -> list[str] | None:
    """Find the goose binary and return the argv that starts its ACP server.

    Ladder, in order:

    1. ``GOOSE_BIN`` — explicit operator override. Required to be an existing file
       so a stale env var falls through to the scan rather than producing a spawn
       that fails with a confusing ENOENT.
    2. ``mise which goose`` — goose is not a Node package, but mise manages
       arbitrary tools and an operator who installed it that way expects that copy.
    3. The augmented PATH, best match first.

    ``augmented_path`` is used rather than the bare inherited ``PATH`` because a
    gateway started by systemd or launchd has a minimal environment that typically
    omits ``~/.local/bin`` — which is exactly where goose's own installer puts the
    binary. ``_ordered_path_matches`` rather than ``shutil.which`` for the same
    reason the other ladders use it: it returns every match so a concrete install
    can be preferred over a shim.

    NOT ``trusted_system_bin``: that helper deliberately resolves only from
    root-owned directories, because it exists for system tools (``ps``, ``lsof``)
    where a same-uid-writable PATH entry would be an injection vector. goose is an
    operator-installed user binary living under ``~/.local/bin`` by design, so
    requiring a root-owned path would report a correctly-installed goose as
    missing. The trade is stated plainly: resolving a user-writable path means an
    operator who can write their own PATH can change which binary Kiro Crew spawns
    — which is already true of every adapter, and of kiro-cli itself.
    """
    from kiro_crew.acp.client import _mise_which, _normalize_exe_casing, _ordered_path_matches
    from kiro_crew.env import augmented_path

    candidates: list[str] = []

    override = os.environ.get("GOOSE_BIN")
    if override and Path(override).is_file():
        candidates.append(override)

    mise_resolved = _mise_which(GOOSE_BIN)
    if mise_resolved:
        candidates.append(mise_resolved)

    candidates.extend(_ordered_path_matches(GOOSE_BIN, augmented_path()))

    for candidate in candidates:
        resolved = _normalize_exe_casing(candidate)
        if not resolved:
            continue
        if not os.access(resolved, os.X_OK):
            # A non-executable hit is not a goose we can spawn. Unlike the Node
            # adapters there is no interpreter to wrap it with, so skip rather
            # than attempt a pairing that cannot work.
            continue
        return [resolved, GOOSE_ACP_SUBCOMMAND]

    return None


def resolve_argv_cached() -> list[str] | None:
    """``resolve_argv`` memoised for the process, mirroring the codex ladder.

    A successful resolution is stable for the process lifetime; a failure is NOT
    cached as ``None`` but left as the sentinel, so an operator who installs goose
    while the gateway is running does not have to restart it.
    """
    global _argv_cache  # noqa: PLW0603
    if _argv_cache is _UNRESOLVED:
        resolved = resolve_argv()
        if resolved is None:
            return None
        _argv_cache = resolved
    return _argv_cache if isinstance(_argv_cache, list) else None


def missing_adapter_message() -> str:
    """What to tell an operator whose host has no goose binary."""
    return (
        "goose not found. Install goose (see https://goose-docs.ai, or "
        "`brew install block-goose-cli`), or set GOOSE_BIN to the binary's "
        "path. Unlike the Claude and Codex adapters there is no separate npm "
        "package to install: goose serves ACP from its own binary via "
        "`goose acp`. Then configure a provider with `goose configure`."
    )


def signin_hint() -> str:
    """goose owns its own provider credentials; Kiro Crew never reads them."""
    return "Configure a provider with `goose configure`."


__all__ = [
    "GOOSE_ACP_SUBCOMMAND",
    "GOOSE_BIN",
    "missing_adapter_message",
    "resolve_argv",
    "resolve_argv_cached",
    "signin_hint",
]
