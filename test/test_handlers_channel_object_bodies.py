"""Channels write handlers must reject non-object JSON and validate create
payloads BEFORE the first mutation (#5586).

Valid JSON that is not an object (array/string/number/null) used to reach
``.get(...)`` and surface as HTTP 500, and ``api_channel_create`` created +
broadcast a channel BEFORE validating ``agents`` — so a malformed payload
published a half-built channel and then answered 500. These tests pin:

  * every write entry point answers deterministic 400 for non-object bodies;
  * an invalid agents payload never reaches ``ChannelManager.create``;
  * valid payloads are unchanged.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers_channel import api_channel_add_agent, api_channel_create


def _request(body: object) -> web.Request:
    request = MagicMock()
    request.match_info = {}
    request.json = AsyncMock(return_value=body)

    mgr = MagicMock()
    # create() would mutate + broadcast; any call inside a test below is a
    # failure the assertions name explicitly.
    mgr.create.return_value = MagicMock(to_dict=lambda: {"id": "ch-x"})
    request.app = {"state": MagicMock(), "channel_manager": mgr}
    return request


@pytest.fixture(autouse=True)
def _route_mgr(monkeypatch: pytest.MonkeyPatch):
    holder: dict = {}

    def _bind(request):
        return request.app["channel_manager"]

    monkeypatch.setattr("kiro_crew.dashboard.handlers_channel._mgr", _bind)
    holder["mgr"] = None
    yield holder


def _mgr_of(request) -> MagicMock:
    return request.app["channel_manager"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [[1, 2], "topic", 5, None])
async def test_create_rejects_non_object_json_without_creating(bad) -> None:
    req = _request(bad)
    with pytest.raises(web.HTTPBadRequest) as ei:
        await api_channel_create(req)
    payload = json.loads(str(ei.value.text))
    assert payload["error"] in ("invalid JSON", "body must be a JSON object")
    if bad is not None:
        assert payload.get("code") == "channels.body_not_object"
    _mgr_of(req).create.assert_not_called()


@pytest.mark.asyncio
async def test_create_rejects_non_string_topic_before_creating() -> None:
    req = _request({"topic": ["incident"]})
    resp = await api_channel_create(req)
    assert resp.status == 400
    json.loads(resp.text)["error"] == "topic must be a string"
    _mgr_of(req).create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("agents", ["not-a-list", [1, 2], [{"role": "A"}, "oops"], {}])
async def test_create_validates_agents_before_the_first_mutation(agents) -> None:
    req = _request({"topic": "incident", "agents": agents})
    resp = await api_channel_create(req)
    assert resp.status == 400
    _mgr_of(req).create.assert_not_called(), (
        "an invalid agents payload must not publish + broadcast a channel"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", ["bogus", ["all"], 3])
async def test_create_rejects_an_unknown_approval_policy_before_creating(
    approval,
) -> None:
    # ApprovalPolicy(...) raises on an unknown policy — AFTER create() had
    # already published the channel — so the policy is validated up front.
    req = _request({"topic": "incident", "agents": [{"role": "A", "approval": approval}]})
    resp = await api_channel_create(req)
    assert resp.status == 400
    _mgr_of(req).create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent",
    [
        {"role": 7},
        {"role": "A", "task": ["x"]},
        {"role": "A", "agent": {"deep": 1}},
    ],
)
async def test_create_rejects_non_string_agent_fields_before_creating(agent) -> None:
    # role/task/agent are concatenated or persisted downstream; a non-string
    # value must be refused before the channel exists, not crash it after.
    req = _request({"topic": "incident", "agents": [dict({"role": "A"}, **agent)]})
    resp = await api_channel_create(req)
    assert resp.status == 400
    _mgr_of(req).create.assert_not_called()


@pytest.mark.asyncio
async def test_a_known_approval_policy_passes_validation() -> None:
    req = _request(
        {
            "topic": "incident",
            "agents": [
                {
                    "role": "Orchestrator",
                    "is_orchestrator": True,
                    "approval": "trusted",
                }
            ],
        }
    )
    resp = await api_channel_create(req)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_a_valid_create_still_reaches_the_manager() -> None:
    req = _request(
        {"topic": "incident", "agents": [{"role": "Orchestrator", "is_orchestrator": True}]}
    )
    resp = await api_channel_create(req)
    assert resp.status == 200
    _mgr_of(req).create.assert_called_once_with("incident")


# --- api_channel_add_agent: same approval contract, no raw 500 -------------


def _add_agent_request(body: object) -> web.Request:
    req = _request(body)
    req.match_info = {"id": "ch-1"}
    ch = MagicMock()
    # add_agent validates the defaulted task (body.get("task", ch.topic)) as
    # a string BEFORE approval, so the mock needs a real topic.
    ch.topic = "incident"
    # add_agent would dereference ApprovalPolicy(...) and persist; any call
    # on an invalid payload is exactly the regression these tests pin.
    ch.add_agent.return_value = MagicMock(to_dict=lambda: {"id": "ag-1"})
    _mgr_of(req).get.return_value = ch
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", ["bogus", ["all"], 3])
async def test_add_agent_rejects_an_unknown_approval_policy(approval) -> None:
    req = _add_agent_request({"approval": approval})
    resp = await api_channel_add_agent(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert "approval must be a valid policy" in body["error"]
    assert body["code"] == "channel_agent_approval_invalid"
    _mgr_of(req).get.return_value.add_agent.assert_not_called(), (
        "an unknown approval policy must never reach ApprovalPolicy(...) -> 500"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["all", "writes", "trusted"])
async def test_add_agent_accepts_every_known_policy(policy, monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers_channel._spawn_agent_task",
        lambda agent, coro: coro.close(),
    )
    req = _add_agent_request({"role": "Reviewer", "approval": policy})
    resp = await api_channel_add_agent(req)
    assert resp.status == 200
