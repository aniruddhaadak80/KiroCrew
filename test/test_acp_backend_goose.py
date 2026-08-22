"""goose, and the fail-closed default for every adapter Kiro Crew has not verified.

Two additions are tested together because they are the two halves of one decision:
adapters differ on the ONE property that matters — whether their tool calls reach
Kiro Crew's PreToolUse gate — so that property is established per adapter and
defaults to "no".

Binary analysis of the shipped 1.46.0 binary suggests delegation: it carries
``fs/read_text_file``, ``fs/write_text_file``, the four ``terminal/*`` methods,
``session/request_permission`` and a ``goose::acp::fs::acp_read_text_file``
symbol. Kiro Crew does not implement those callbacks, so the descriptor stays
``UNVERIFIED``.

pi-acp is the counter-example that motivated the default. Its own ``dist`` contains
no ``fs/*`` or ``terminal/*`` call at all — the matches are in the ACP SDK it
vendors, not in its code — and its single ``requestPermission`` call site is
``requestExtensionPermission``, which forwards a pi EXTENSION's UI dialog
(``toolCallId: pi-ui-*``, ``kind: "other"``). So it does have a permission path; it
simply does not gate its own tool execution with it, matching its stated limitation
that pi reads, writes and executes locally.

The decisive part is that those two adapters are indistinguishable at
``initialize``. Both advertise the same shape — ``loadSession``,
``mcpCapabilities``, ``promptCapabilities``, ``sessionCapabilities`` — and neither
says anything about delegation or permission gating. Governability therefore cannot
be discovered from the handshake, which is exactly why the unverified default has
to refuse rather than probe and hope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.acp import backends, tool_gate
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.types import (
    ACP_BACKEND_GOOSE,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SELECTABLE,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
)


class TestGooseDescriptor:
    def test_goose_is_known_and_selectable(self) -> None:
        assert ACP_BACKEND_GOOSE in ACP_BACKENDS_KNOWN
        assert ACP_BACKEND_GOOSE in ACP_BACKENDS_SELECTABLE

    def test_goose_routing_is_unverified_until_callbacks_exist(self) -> None:
        descriptor = backends.descriptor_for(ACP_BACKEND_GOOSE)
        assert descriptor.routing is backends.Routing.UNVERIFIED

    def test_goose_speaks_the_spec_dialect_not_kiros(self) -> None:
        """``_meta.kiro`` extensions are kiro-cli's, not a third party's."""
        assert backends.dialect_of(ACP_BACKEND_GOOSE) is backends.Dialect.SPEC

    def test_goose_gets_no_seatbelt_waiver(self) -> None:
        """The one capability that fails OPEN (harness parity H7).

        Membership makes ``sandbox.wrap_argv`` skip Kiro Crew's own confinement in
        favour of the harness's internal sandbox. goose does not ship one, so
        granting this would leave the agent process unconfined.
        """
        assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_INTERNAL_SANDBOX

    def test_goose_claims_no_kiro_family_capability(self) -> None:
        """It runs one process per session and implements no steer extension."""
        assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_SESSION_SHARING
        assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_STEER

    def test_goose_needs_no_adapter_install(self) -> None:
        """`goose acp` is served by the goose binary, not a separate package."""
        assert backends.descriptor_for(ACP_BACKEND_GOOSE).install_command == ""

    def test_native_resume_is_unavailable_as_the_handshake_reports(self) -> None:
        """Confirmed on the wire against goose 1.46.0, not guessed.

        Its ``initialize`` advertises ``sessionCapabilities`` of ``list`` and
        ``close`` only — no ``resume`` and no ``fork``, where claude offers both.
        The descriptor level has to match what the adapter actually serves, because
        a resume attempted against an adapter that cannot do it fails mid-session
        rather than at selection.
        """
        assert (
            backends.level(ACP_BACKEND_GOOSE, backends.CAP_NATIVE_RESUME)
            is backends.Level.UNAVAILABLE
        )

    def test_goose_profiles_work_differently_but_tool_search_is_absent(self) -> None:
        """Prompts and skills are injected; restricted tool profiles are refused."""
        assert (
            backends.level(ACP_BACKEND_GOOSE, backends.CAP_AGENT_PROFILES)
            is backends.Level.DEGRADED
        )
        assert (
            backends.level(ACP_BACKEND_GOOSE, backends.CAP_TOOL_SEARCH)
            is backends.Level.UNAVAILABLE
        )


class TestGooseFailsClosedWithoutClientCallbacks:
    def test_verdict_is_indeterminate(self, tmp_path: Path) -> None:
        verdict, reason = tool_gate.resolve_verdict(ACP_BACKEND_GOOSE, tmp_path)
        assert verdict is Verdict.INDETERMINATE
        assert "not established" in reason

    def test_enforce_refuses_without_the_opt_out(self, tmp_path: Path) -> None:
        with pytest.raises(tool_gate.ToolGateUnroutable):
            tool_gate.enforce(ACP_BACKEND_GOOSE, tmp_path, allow_ungated=False)

    def test_the_named_opt_out_permits_startup(self, tmp_path: Path) -> None:
        tool_gate.enforce(ACP_BACKEND_GOOSE, tmp_path, allow_ungated=True)

    def test_nothing_is_written_into_the_work_dir(self, tmp_path: Path) -> None:
        tool_gate.resolve_verdict(ACP_BACKEND_GOOSE, tmp_path)
        assert list(tmp_path.iterdir()) == []


class TestUnverifiedAdapterFailsClosed:
    """The generic path for the adapters Kiro Crew has NOT verified.

    Exercised through a SYNTHESISED descriptor rather than a registered backend
    id, because registering one would defeat the point: the case under test is
    precisely an adapter absent from the hand-written table.
    """

    def test_synthesised_descriptor_is_unverified(self) -> None:
        descriptor = backends.descriptor_for_registry_adapter("pi-acp", "pi ACP")
        assert descriptor.routing is backends.Routing.UNVERIFIED

    def test_it_marks_every_capability_unverified(self) -> None:
        """A capability is a claim about observed behaviour; nothing was observed."""
        descriptor = backends.descriptor_for_registry_adapter("pi-acp")
        for capability in backends.ALL_CAPABILITIES:
            assert descriptor.capabilities[capability] is backends.Level.UNVERIFIED

    def test_it_never_receives_the_seatbelt_waiver(self) -> None:
        """Third-party code of unknown provenance is always wrapped.

        Holds by construction: the waiver is granted by explicit listing in
        acp/types.py, and a synthesised descriptor is listed nowhere.
        """
        descriptor = backends.descriptor_for_registry_adapter("anything-at-all")
        assert descriptor.id not in ACP_BACKENDS_INTERNAL_SANDBOX
        assert descriptor.id not in ACP_BACKENDS_KNOWN

    def test_verdict_is_indeterminate_never_bypassed(self, unverified: str) -> None:
        """Kiro Crew does not claim the adapter ignores permissions.

        It claims only that it has no evidence — and absent evidence, the gate
        must not be reported as armed.
        """
        verdict, reason = tool_gate.resolve_verdict(unverified, "/tmp")
        assert verdict is Verdict.INDETERMINATE
        assert "not established" in reason

    def test_enforce_refuses_by_default(self, unverified: str, tmp_path: Path) -> None:
        with pytest.raises(tool_gate.ToolGateUnroutable) as excinfo:
            tool_gate.enforce(unverified, tmp_path, allow_ungated=False)
        message = str(excinfo.value)
        # The refusal must say what is not being enforced and name the one opt-out,
        # or an operator cannot tell a policy refusal from a crash.
        assert "denied-command" in message
        assert "sensitive-path" in message
        assert "acp_backend_allow_ungated_tools" in message

    def test_the_named_opt_out_still_works(self, unverified: str, tmp_path: Path) -> None:
        """One documented escape hatch, so an operator is never simply stuck."""
        tool_gate.enforce(unverified, tmp_path, allow_ungated=True)

    def test_remediation_does_not_invent_a_setting(self, unverified: str) -> None:
        """There is no config change that verifies an adapter, so promise none."""
        hint = tool_gate.remediation_for(unverified, "/tmp")
        assert "verified" in hint
        assert "approval_policy" not in hint
        assert "defaultMode" not in hint


@pytest.fixture
def unverified(monkeypatch) -> str:
    """Make ``descriptor_for`` resolve one synthesised, unverified adapter.

    Only that id is intercepted; every other backend still resolves through the
    real table, so a test cannot pass because lookups were broken wholesale.
    """
    descriptor = backends.descriptor_for_registry_adapter("pi-acp", "pi ACP")
    original = backends.descriptor_for

    def fake(backend: str) -> backends.BackendDescriptor:
        return descriptor if backend == descriptor.id else original(backend)

    monkeypatch.setattr(backends, "descriptor_for", fake)
    return descriptor.id
