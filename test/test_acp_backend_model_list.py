"""/api/models must not shell out to kiro-cli on another ACP backend.

That subprocess is both impossible (the binary may be absent) and wrong (its ids
are kiro-namespace and the other backend rejects them). The advertised list from a
live session is the only correct source.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_AUTO_MODEL,
    ACP_BACKENDS_SELECTABLE,
)
from kiro_crew.dashboard.handlers import agents as agents_handler


class _Provider:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def available_models(self) -> list[dict[str, str]]:
        return self._rows


class _NestedProvider:
    """A runtime-backed provider exposes the reader on its inner client."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.client = _Provider(rows)


def _request(providers: list[Any]) -> MagicMock:
    sessions = MagicMock()
    sessions.active_providers = lambda: providers
    state = MagicMock()
    state.sessions = sessions
    request = MagicMock()
    request.app = {"state": state}
    return request


class TestAdvertisedModels:
    def test_ids_are_passed_through_verbatim(self) -> None:
        """A Codex row is spelled <model>[<effort>].

        Rewriting it would produce an id the adapter never advertised, which
        surfaces as "model unavailable" rather than a wire error.
        """
        rows = [{"modelId": "gpt-5.2[high]", "name": "GPT 5.2 (high)"}]
        models = agents_handler._advertised_alt_backend_models(_request([_Provider(rows)]))
        assert models == [
            {
                "model_name": "gpt-5.2[high]",
                "display_name": "GPT 5.2 (high)",
                "description": "",
            }
        ]

    def test_newest_session_wins(self) -> None:
        """An older session may predate a backend switch."""
        old = _Provider([{"modelId": "old-model"}])
        new = _Provider([{"modelId": "new-model"}])
        models = agents_handler._advertised_alt_backend_models(_request([old, new]))
        assert [m["model_name"] for m in models] == ["new-model"]

    def test_lists_are_not_merged_across_sessions(self) -> None:
        """Merging two namespaces would offer ids the active backend rejects."""
        a = _Provider([{"modelId": "a"}])
        b = _Provider([{"modelId": "b"}])
        models = agents_handler._advertised_alt_backend_models(_request([a, b]))
        assert len(models) == 1

    def test_an_empty_advertisement_falls_through_to_an_older_session(self) -> None:
        empty = _Provider([])
        older = _Provider([{"modelId": "found"}])
        models = agents_handler._advertised_alt_backend_models(_request([older, empty]))
        assert [m["model_name"] for m in models] == ["found"]

    def test_a_runtime_backed_provider_is_read_through_its_client(self) -> None:
        rows = [{"modelId": "nested"}]
        models = agents_handler._advertised_alt_backend_models(_request([_NestedProvider(rows)]))
        assert [m["model_name"] for m in models] == ["nested"]

    def test_display_name_defaults_to_the_id(self) -> None:
        models = agents_handler._advertised_alt_backend_models(
            _request([_Provider([{"modelId": "bare"}])])
        )
        assert models[0]["display_name"] == "bare"

    def test_rows_without_an_id_are_dropped(self) -> None:
        rows = [{"name": "no id"}, {"modelId": ""}, {"modelId": "keep"}]
        models = agents_handler._advertised_alt_backend_models(_request([_Provider(rows)]))
        assert [m["model_name"] for m in models] == ["keep"]

    def test_no_sessions_yields_an_empty_list(self) -> None:
        assert agents_handler._advertised_alt_backend_models(_request([])) == []

    def test_a_provider_that_raises_is_skipped(self) -> None:
        class _Boom:
            def available_models(self) -> list[dict[str, str]]:
                raise RuntimeError("nope")

        good = _Provider([{"modelId": "ok"}])
        models = agents_handler._advertised_alt_backend_models(_request([good, _Boom()]))
        assert [m["model_name"] for m in models] == ["ok"]

    def test_a_missing_state_does_not_raise(self) -> None:
        request = MagicMock()
        request.app = {}
        assert agents_handler._advertised_alt_backend_models(request) == []


class TestBackendReader:
    def test_fails_closed_to_kiro(self) -> None:
        assert agents_handler._configured_acp_backend() in ("", "codex", "claude", "kas")


class TestEndpointContract:
    def test_the_degraded_code_is_machine_readable(self) -> None:
        """The client must tell an unfixable-by-retry refusal from a timeout.

        Asserted against the handler source because reaching the branch needs a
        full app fixture; the string is the contract the frontend keys on.
        """
        import inspect

        source = inspect.getsource(agents_handler.api_models)
        assert "acp_backend_models_unavailable" in source
        assert "503" in source

    def test_the_backend_branch_precedes_the_kiro_spawn(self) -> None:
        """Ordering is the point: the spawn must not happen at all."""
        import inspect

        source = inspect.getsource(agents_handler.api_models)
        branch_at = source.index("_advertised_alt_backend_models")
        spawn_at = source.index("--list-models")
        assert branch_at < spawn_at

    def test_the_auto_capability_is_reported_on_success_and_on_refusal(self) -> None:
        """The picker cannot infer ``auto`` from a model list it does not have.

        The degraded 503 IS the steady state of an adapter with no live session,
        so a flag sent only on success would be absent exactly when the picker has
        to decide whether it may synthesize an Auto row. Asserted against the
        source for the same reason as the sibling tests: reaching either branch
        needs a full app fixture.

        Counted as the whole assignment rather than the bare word: that pins both
        halves at once -- the key ships twice, and each value is READ from the
        membership set instead of restated as a literal -- and prose explaining
        either cannot make the count drift.
        """
        import inspect

        source = inspect.getsource(agents_handler.api_models)
        assert source.count('"serves_auto": _alt_backend in ACP_BACKENDS_AUTO_MODEL') == 2


class TestAutoModelMembership:
    """``auto`` is a kiro-namespace id, so membership is opt-in (H6)."""

    def test_the_kiro_agent_family_serves_auto(self) -> None:
        assert ACP_BACKEND_KIRO in ACP_BACKENDS_AUTO_MODEL
        assert ACP_BACKEND_KAS in ACP_BACKENDS_AUTO_MODEL

    def test_spec_adapters_do_not(self) -> None:
        """claude advertises ``default``; codex advertises ``openai.*`` ids.

        Neither has an ``auto`` row, and offering one renders a sole option that
        is rejected at the wire.
        """
        assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_AUTO_MODEL
        assert ACP_BACKEND_CODEX not in ACP_BACKENDS_AUTO_MODEL
        assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_AUTO_MODEL

    def test_membership_is_a_deliberate_subset_of_selectable(self) -> None:
        """A backend nobody can select cannot claim a capability."""
        assert ACP_BACKENDS_AUTO_MODEL <= ACP_BACKENDS_SELECTABLE

    def test_a_new_backend_does_not_inherit_the_capability(self) -> None:
        """The whole point of opt-in membership.

        An unknown registry adapter must not acquire ``auto`` by being absent
        from some negation elsewhere.
        """
        assert "some-future-adapter" not in ACP_BACKENDS_AUTO_MODEL
