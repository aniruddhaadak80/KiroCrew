"""OpenCode and pi: selectable, ROUTED via session/request_permission, adapted only."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.acp import backends, tool_gate
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.types import (
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_AUTO_MODEL,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KIRO_CREDITS,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SELECTABLE,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
)


@pytest.mark.parametrize("backend", [ACP_BACKEND_OPENCODE, ACP_BACKEND_PI])
class TestOpenCodeAndPiAreRoutedAdapters:
    def test_known_and_selectable(self, backend: str) -> None:
        assert backend in ACP_BACKENDS_KNOWN
        assert backend in ACP_BACKENDS_SELECTABLE

    def test_spec_dialect_and_permission_request(self, backend: str) -> None:
        descriptor = backends.descriptor_for(backend)
        assert descriptor.dialect is backends.Dialect.SPEC
        assert descriptor.routing is backends.Routing.PERMISSION_REQUEST

    def test_no_kiro_runtime_capabilities(self, backend: str) -> None:
        assert backend not in ACP_BACKENDS_INTERNAL_SANDBOX
        assert backend not in ACP_BACKENDS_STEER
        assert backend not in ACP_BACKENDS_SESSION_SHARING
        assert backend not in ACP_BACKENDS_AUTO_MODEL
        assert backend not in ACP_BACKENDS_KIRO_CREDITS

    def test_enforce_succeeds_without_the_opt_out(self, backend: str, tmp_path: Path) -> None:
        tool_gate.enforce(backend, tmp_path, allow_ungated=False)

    def test_verdict_is_routed(self, backend: str, tmp_path: Path) -> None:
        verdict, reason = tool_gate.resolve_verdict(backend, tmp_path)
        assert verdict is Verdict.ROUTED
        assert "session/request_permission" in reason or "asks per privileged tool" in reason
