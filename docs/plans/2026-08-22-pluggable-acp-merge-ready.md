# Pluggable ACP backends — merge-ready plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Get `origin/feat/pluggable-acp-backends` locally merge-ready (rebased, honest, fail-closed, gates green) without opening a pull request.

**Architecture:** Keep the existing registry + descriptor + tool-gate design. Do not add goose `fs/*` / `terminal/*` handlers and do not restore KAS steer. The merge-ready work is: rebase onto current `main`, collapse registry ids onto hand-written backends, make the operator docs and Settings card tell the truth about routing, and run the local gates.

**Tech Stack:** Python 3.10+ (`kiro_crew.acp`), React + TS dashboard, pytest / vitest, i18n catalogs under `website/src/i18n/locales/`.

**Do not:** `gh pr create`, `git push` unless the user later asks, implement goose client callbacks, add a dashboard toggle for `acp_backend_allow_ungated_tools`, or drive a billed adapter turn as a merge blocker.

**Decisions already made (do not re-open):**

1. Keep the `AGENTS.md` amendment. ACP adapters at `agent.acp_backend` are the point of this work. Update the task spec to match; do not revert the rule.
2. Keep goose in `ACP_BACKENDS_SELECTABLE`. It is an experimental, fail-closed row. Fix the comments and docs that claim SELECTABLE means the gate is armed.
3. Persist the hand-written id (`codex`, `claude`, `goose`), never the registry id (`codex-acp`, `claude-acp`). Alias on resolve.
4. Leave `ACP_BACKENDS_STEER = {ACP_BACKEND_KIRO}`. Call the KAS change out in the harness-parity spec. Do not restore KAS without a measured test.
5. Harness-capsule / plan-limit strings already live in `en.manual.json` and the 12 locales. Do not copy them into `en.json` (that shadows). After rebase, only fix what `npm run i18n:check` actually reports.

**Conflict magnets on rebase:** `src/kiro_crew/acp/client.py` (+801 on the branch), `src/kiro_crew/acp/types.py`, `src/kiro_crew/acp/session_handle.py`, `src/kiro_crew/providers/acp.py`, `AGENTS.md`, `test/test_acp_client.py`, `test/test_harness_parity.py`, `website/src/App.tsx`, `website/src/components/ChatInput.tsx`.

**Harness-parity while resolving conflicts:** identity is positive (`== ACP_BACKEND_KIRO` / membership sets). Never express kiro as `not is_claude`. Never add a required step to the kiro spawn path. Never put a registry adapter in `ACP_BACKENDS_INTERNAL_SANDBOX`. Cite H2 / H5 / H6 / H7 / H8 in review notes, not in new comments that narrate the rebase.

---

### Task 1: Worktree and rebase onto origin/main

**Files:** none yet (git only).

The current Cursor worktree is **not** this branch. Do the rebase in a dedicated worktree so `cursor/892f24a5` stays untouched.

**Step 1: Fetch and make a worktree**

```bash
git fetch origin main feat/pluggable-acp-backends
git worktree add ../pluggable-acp-merge origin/feat/pluggable-acp-backends
cd ../pluggable-acp-merge
git checkout -B feat/pluggable-acp-backends
```

Expected: branch tip is `e232ad078`, working tree clean.

**Step 2: Rebase onto origin/main**

```bash
git rebase origin/main
```

Expected: conflicts, concentrated in the files listed above. Resolve one file at a time. Keep branch behavior for spec adapters; keep main's kiro-path changes unless they fight a branch invariant.

When a conflict is "main added a kiro-only field / branch added a spec-only field", keep both. When a conflict is a negative identity test main still has, rewrite it to a positive membership assertion before continuing.

**Step 3: Do not continue past an unresolved semantic conflict**

If `AcpClient._spawn` cannot keep a kiro fall-through plus positive adapter arms, stop and write down the exact hunk. Do not collapse spawn argv into one form every harness accepts (H2).

**Step 4: Commit is the rebase itself**

No extra commit. After `git rebase --continue` finishes:

```bash
git log --oneline origin/main..HEAD
```

Expected: the original four commits, possibly more if rebase splits, all rebased on current main.

---

### Task 2: Align the policy documents

**Files:**
- Modify: `docs/task-specs/2026/08/pluggable-acp-backends/README.md`
- Modify: `AGENTS.md` (only if rebase reintroduced the old "do NOT re-add the public registration glue" paragraph)
- Modify: `docs/system-specs/modules/harness-parity.md` (adapted-harness list)
- Modify: `docs/system-specs/modules/providers.md` and `docs/system-specs/modules/security.md` if rebase dropped the branch's additions — restore them from `origin/feat/pluggable-acp-backends` if missing

**Step 1: Confirm AGENTS.md still has the amendment**

```bash
rg -n "ACP adapter" AGENTS.md
```

Expected: the "What is NO LONGER forbidden is ACP adapter support" paragraph. If rebase restored the old text, put the amendment back.

**Step 2: Rewrite the task-spec opener**

Replace the "AGENTS.md forbids it / contradiction is deliberate" frame with: the rule was amended on this branch; adapters are a shipped goal under the conditions already listed (provider stays `acp`, no API-key path, refuse-unless-routed). Keep the upstream-issue table. Keep "this work is not an answer to #1693".

**Step 3: One-line KAS steer call-out in harness-parity.md**

Add under the STEER membership discussion (or the capability-sets section the branch already added): KAS is not in `ACP_BACKENDS_STEER` until independently measured. This is a behavior change versus older main, fail-closed on purpose. `test/test_harness_parity.py` already pins `ACP_BACKEND_KAS not in ACP_BACKENDS_STEER`.

**Step 4: Commit**

```bash
git add docs/task-specs/2026/08/pluggable-acp-backends/README.md \
        docs/system-specs/modules/harness-parity.md AGENTS.md
git commit -m "$(cat <<'EOF'
docs: record the acp-adapter policy amendment

The task spec still described a standing contradiction. The branch
amended AGENTS.md; the spec now matches that decision.
EOF
)"
```

---

### Task 3: Alias registry ids onto hand-written backends

**Files:**
- Modify: `src/kiro_crew/acp/backends.py` (`descriptor_for`, `selectable_ids`, add `canonical_backend_id`)
- Modify: `src/kiro_crew/config/loader.py` (`_normalize_acp_backend`)
- Test: `test/test_acp_backend_registry.py`
- Test: `test/test_config_patch.py` only if the PATCH allowlist test names raw registry ids

A cached registry entry `codex-acp` currently synthesizes `Routing.UNVERIFIED`. The hand-written `codex` descriptor is `SESSION_CONFIG`. Same adapter, two trust paths. Claude is the same pair (`claude` / `claude-acp`).

**Step 1: Write the failing tests**

Add to `test/test_acp_backend_registry.py`:

```python
def test_canonical_backend_id_maps_registry_ids() -> None:
    from kiro_crew.acp.backends import canonical_backend_id
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX

    assert canonical_backend_id("codex-acp") == ACP_BACKEND_CODEX
    assert canonical_backend_id("claude-acp") == ACP_BACKEND_CLAUDE
    assert canonical_backend_id("codex") == ACP_BACKEND_CODEX
    assert canonical_backend_id("pi-acp") == "pi-acp"


class TestCachedRegistryAdapters:
    def test_hand_written_registry_id_is_not_a_second_trust_path(self, monkeypatch) -> None:
        from kiro_crew.acp import registry
        from kiro_crew.acp.backends import Routing, descriptor_for, selectable_ids

        adapter = registry.RegistryAdapter(
            id="codex-acp",
            name="Codex",
            version="1.4.0",
            description="",
            repository="",
            license="Apache-2.0",
            icon="",
            kind="npx",
            package="@agentclientprotocol/codex-acp@1.4.0",
            args=(),
            env=(),
        )
        monkeypatch.setattr(registry, "cached", lambda: {adapter.id: adapter})

        descriptor = descriptor_for(adapter.id)
        assert descriptor.id == "codex"
        assert descriptor.routing is Routing.SESSION_CONFIG
        assert "codex-acp" not in selectable_ids()
        assert "codex" in selectable_ids()
```

Add next to the existing `_normalize_acp_backend` tests (same module that already covers unknown → kiro):

```python
def test_normalize_aliases_registry_id_to_hand_written_backend() -> None:
    from kiro_crew.config.loader import _normalize_acp_backend
    from kiro_crew.acp.types import ACP_BACKEND_CODEX

    assert _normalize_acp_backend("codex-acp") == ACP_BACKEND_CODEX
```

**Step 2: Run tests to verify they fail**

```bash
python -m pytest test/test_acp_backend_registry.py::test_canonical_backend_id_maps_registry_ids \
  test/test_acp_backend_registry.py::TestCachedRegistryAdapters::test_hand_written_registry_id_is_not_a_second_trust_path \
  -q
```

Expected: FAIL — `canonical_backend_id` is not defined, and `descriptor_for("codex-acp")` returns an UNVERIFIED synth.

**Step 3: Minimal implementation**

In `src/kiro_crew/acp/backends.py`:

```python
def canonical_backend_id(backend: str) -> str:
    """Map a registry id onto the hand-written backend it names, if any.

    The registry's identity for Codex is ``codex-acp``; Kiro Crew persists
    ``codex``. Resolving the registry spelling as a synthesized UNVERIFIED
    descriptor would refuse a backend we already know how to gate.
    """
    for descriptor in _BY_ID.values():
        if descriptor.registry_id and descriptor.registry_id == backend:
            return descriptor.id
    return backend


def descriptor_for(backend: str) -> BackendDescriptor:
    resolved = canonical_backend_id(backend)
    descriptor = _BY_ID.get(resolved)
    if descriptor is not None:
        return descriptor
    # existing registry-synth + UnknownAcpBackend path, keyed on `resolved`
    ...


def selectable_ids(*, refresh_registry: bool = False) -> frozenset[str]:
    from kiro_crew.acp import registry
    from kiro_crew.acp.types import ACP_BACKENDS_SELECTABLE

    adapters = registry.fetch() if refresh_registry else registry.cached()
    owned = {d.registry_id for d in _BY_ID.values() if d.registry_id}
    discovered = {
        adapter.id
        for adapter in adapters.values()
        if adapter.is_launchable and adapter.id not in owned
    }
    return frozenset({*ACP_BACKENDS_SELECTABLE, *discovered})
```

In `_normalize_acp_backend` (`src/kiro_crew/config/loader.py`), canonicalize before the selectable check:

```python
from kiro_crew.acp.backends import canonical_backend_id, selectable_ids

if isinstance(value, str):
    value = canonical_backend_id(value)
    if value in selectable_ids():
        return value
```

Export `canonical_backend_id` from `backends.py` `__all__`.

**Step 4: Run tests to verify they pass**

```bash
python -m pytest test/test_acp_backend_registry.py test/test_acp_backends_endpoint.py -q
```

Expected: PASS. The existing `test_launchable_cached_adapter_is_selectable_and_described` (`pi-acp`) must still pass — that id has no hand-written row.

**Step 5: Fix the SELECTABLE comment in types.py**

`src/kiro_crew/acp/types.py` currently says membership means "the gate can be shown to route this backend's tool calls". That is false for goose. Rewrite to: membership means an operator may persist the value; routing is a separate axis on the descriptor (`Routing.UNVERIFIED` still refuses at session start).

**Step 6: Commit**

```bash
git add src/kiro_crew/acp/backends.py src/kiro_crew/acp/types.py \
        src/kiro_crew/config/loader.py test/test_acp_backend_registry.py
git commit -m "$(cat <<'EOF'
fix(acp): alias registry ids onto hand-written backends

codex-acp and claude-acp were selectable as UNVERIFIED synths beside
the gated descriptors. Persist the hand-written id only.
EOF
)"
```

---

### Task 4: Make the operator docs match SESSION_CONFIG

**Files:**
- Modify: `src/kiro_crew/docs/experimental-acp-adapters.md`
- Modify: `src/kiro_crew/docs/README.md` only if the doc is missing from the index (it should already be there)
- Modify: `src/kiro_crew/acp/codex.py` module / `remediation_hint` docstring if it still tells the operator to edit `approval_policy` as the session-start requirement
- Test: `test/test_acp_backend_goose.py` file header only (soften "earns ROUTED structurally")

Read `docs/system-specs/modules/security.md` on the branch first — it already explains why the `config.toml` probe was wrong. The user-facing doc must say the same thing.

**Step 1: Rewrite the opening table sentence**

Replace "These are selectable because their tool calls can be shown to reach Kiro Crew's security gate" with: selectable means the value may be persisted. Codex and Claude are gated before the first prompt. goose is selectable and **refused** until Crew implements `fs/*` and `terminal/*`, unless `agent.acp_backend_allow_ungated_tools` is on.

**Step 2: Replace the Codex `approval_policy` section**

Delete the `config.toml` `approval_policy = "untrusted"` block as the required start condition. Write:

- Crew applies ACP v1 session config `mode=read-only` after `session/new` / `session/load` and refuses if the adapter does not advertise that value or the write fails.
- `read-only` still permits passive reads without a permission prompt. Commands and writes must ask.
- `$CODEX_HOME/config.toml` is not consulted for this decision. An earlier probe of `approval_policy` resolved ROUTED for a session that did not emit permission frames.

Keep the install (`codex-acp`, not `codex acp`) and `codex login` sections.

**Step 3: Fix the Claude vendored-copy sentence**

Replace "Kiro Crew ships a vendored copy and will find it automatically in most installs" with: install `@agentclientprotocol/claude-agent-acp` yourself (`npm install -g`). Crew resolves an explicit override, then a project/`_vendor` `node_modules` copy if one happens to exist, then PATH. The published wheel does not bundle the adapter.

**Step 4: Soften the goose test module docstring**

`test/test_acp_backend_goose.py` may still claim goose "earns ROUTED structurally". Change it to: binary analysis suggests delegation; Crew does not implement the callbacks, so the descriptor stays `UNVERIFIED`.

**Step 5: Commit**

```bash
git add src/kiro_crew/docs/experimental-acp-adapters.md \
        src/kiro_crew/acp/codex.py test/test_acp_backend_goose.py
git commit -m "$(cat <<'EOF'
docs: describe session-config gating, not approval_policy

The live Codex gate is mode=read-only before the first prompt. The
file probe and the vendored-Claude claim were both stale.
EOF
)"
```

---

### Task 5: Show the tool-gate verdict on the Settings card

**Files:**
- Modify: `website/src/pages/overview/AcpBackendCard.tsx`
- Modify: `website/src/test/AcpBackendCard.test.tsx`
- Modify: `website/src/i18n/locales/en.manual.json`
- Modify: every `website/src/i18n/locales/<tag>.json` except `en.json` (do not put manual keys in `en.json`)
- Read first: `website/docs/i18n-catalog.md`, `docs/ci/i18n-gates.md`

`GET /api/acp-backends` already returns `routing_verdict`, `routing_reason`, `allow_ungated_tools`. The card types them and never renders them. An operator who picks goose (or any UNVERIFIED row) currently gets a silent refuse at session start.

**Step 1: Write the failing frontend test**

In `website/src/test/AcpBackendCard.test.tsx`, add a case whose mock payload is:

```ts
{
  active: 'goose',
  allow_ungated_tools: false,
  routing_verdict: 'indeterminate',
  routing_reason: 'Kiro Crew has not established how this adapter routes tool calls',
  backends: [
    /* existing fixtures plus goose */
  ],
}
```

Assert the card text includes the translated indeterminate verdict (query via the English `en.manual.json` value you will add, or via a role/name the test already uses). Assert that when `allow_ungated_tools: true` the card shows the opt-out warning.

Run:

```bash
cd website && npx vitest run src/test/AcpBackendCard.test.tsx
```

Expected: FAIL — no verdict node.

**Step 2: Add catalog keys (all languages, same commit)**

Add only to `en.manual.json` (no source literal in JSX — you will call `i18nT`):

```json
"pages": {
  "overview": {
    "acpBackend": {
      "routing_routed": "Tool calls reach Kiro Crew's security gate",
      "routing_indeterminate": "Tool-call routing is not verified — new sessions will refuse",
      "routing_bypassed": "Tool calls would bypass Kiro Crew's security gate — new sessions will refuse",
      "routing_reason": "{{reason}}",
      "ungated_opt_out": "Ungated tools are allowed. Denied-command rules, sensitive-path blocking, and the governance ceiling are not consulted for this backend's self-approved calls."
    }
  }
}
```

Copy the same keys into `zh-CN`, `hi`, `es`, `fr`, `bn`, `pt`, `ru`, `de`, `ja`, `ko`, `it`, and regenerate `en-XA` with `npm run i18n:pseudo`. English values in the non-English catalogs are acceptable for this pass; do not invent translations. Never add these keys to `en.json`.

**Step 3: Render the verdict on the active backend**

Above the backend list (or on the active row), when `data.routing_verdict` is non-empty:

```tsx
const ROUTING_KEYS: Record<string, string> = {
  routed: 'pages.overview.acpBackend.routing_routed',
  indeterminate: 'pages.overview.acpBackend.routing_indeterminate',
  bypassed: 'pages.overview.acpBackend.routing_bypassed',
}

// ...
{data.routing_verdict && (
  <p className={data.routing_verdict === 'routed' ? 'text-ok' : 'text-warn'} role="status">
    {i18nT(ROUTING_KEYS[data.routing_verdict] ?? 'pages.overview.acpBackend.routing_indeterminate')}
    {data.routing_reason ? ` — ${data.routing_reason}` : ''}
  </p>
)}
{data.allow_ungated_tools && (
  <p className="text-warn" role="status">
    {i18nT('pages.overview.acpBackend.ungated_opt_out')}
  </p>
)}
```

`routing_reason` is an English backend string today. Render it verbatim (same as `limit_type` and install commands). Do not invent a catalog for doctor reasons in this task.

**Step 4: Run the card test and i18n check**

```bash
cd website && npx vitest run src/test/AcpBackendCard.test.tsx
cd website && npm run i18n:check
```

Expected: both PASS. If `i18n:check` reports `[key-refs]` or locale parity, the missing language file is the bug — fix it, do not skip the gate.

**Step 5: Commit**

```bash
git add website/src/pages/overview/AcpBackendCard.tsx \
        website/src/test/AcpBackendCard.test.tsx \
        website/src/i18n/locales
git commit -m "$(cat <<'EOF'
feat(website): show the adapter tool-gate verdict

The API already returned routing_verdict. The card now surfaces it
so an UNVERIFIED backend is visibly going to refuse.
EOF
)"
```

---

### Task 6: Types.py comment and doctor copy that still mention approval_policy

**Files:**
- Grep the branch for `approval_policy` after the doc rewrite
- Modify whatever still claims it is the session-start requirement: likely `src/kiro_crew/acp/doctor.py`, `src/kiro_crew/acp/codex.py` `remediation_hint`, `docs/system-specs/modules/security.md` (keep the history of why the probe was wrong; do not keep it as current enforcement)

**Step 1: Find leftovers**

```bash
rg -n "approval_policy" src/kiro_crew docs/system-specs src/kiro_crew/docs
```

Expected: historical mentions in security.md are fine. Doctor / remediation / user docs must describe `mode=read-only`.

**Step 2: Fix and run doctor tests**

```bash
python -m pytest test/test_acp_backend_doctor.py test/test_acp_tool_gate.py -q
```

Expected: PASS.

**Step 3: Commit** if anything changed

```bash
git commit -m "$(cat <<'EOF'
docs(acp): drop leftover approval_policy start requirements

Doctor and remediation text now name session mode=read-only, which
is what the handshake actually enforces.
EOF
)"
```

---

### Task 7: Local gates

Read `docs/system-specs/common/testing-conventions.md` and `website/AGENTS.md` before running.

**Step 1: Format and Python lint**

```bash
black src/kiro_crew test && isort src/kiro_crew test
flake8 src/kiro_crew test && mypy src/kiro_crew
```

Expected: clean. Fix anything the rebase or new code introduced. No `# type: ignore` to silence new errors.

**Step 2: Harness-parity and brand gates**

```bash
HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py
BRAND_BASE_REF=origin/main python3 scripts/check_brand_name.py
```

Expected: exit 0. A new `not is_*` under `src/kiro_crew/` is a real fail — rewrite to a positive test.

**Step 3: Docs lint**

```bash
scripts/docs-lint.sh
```

Expected: exit 0. If you added a spec file, it must be in that directory's README.

**Step 4: Focused pytest, then the adapter suite**

```bash
python -m pytest \
  test/test_acp_backend_registry.py \
  test/test_acp_tool_gate.py \
  test/test_acp_adapter_spawn.py \
  test/test_acp_codex_adapter.py \
  test/test_acp_claude_backend.py \
  test/test_acp_backend_goose.py \
  test/test_acp_spec_mcp_delivery.py \
  test/test_acp_backends_endpoint.py \
  test/test_harness_parity.py \
  test/test_acp_client.py \
  -q
```

Then, if that is green:

```bash
python -m pytest
```

A multi-test `--override-ini` MUST keep `-n auto --dist loadgroup --max-worker-restart=2`. Prefer no override.

**Step 5: Frontend**

```bash
cd website && npx tsc -b && npm run i18n:check && npm run test
```

Do not use `npm run typecheck` — `website/AGENTS.md` says it checks zero files.

**Step 6: Commit only if formatters or gates forced a fix**

```bash
git commit -m "$(cat <<'EOF'
chore: satisfy lint after the acp-adapter rebase

Formatter and gate fallout from replaying the branch onto current main.
EOF
)"
```

---

### Task 8: Optional live-turn check (not a merge blocker)

Only if this machine has `codex-acp` or `claude-agent-acp` installed and a signed-in vendor CLI.

**Step 1:** Preview flag on → Developer → Config → switch to Codex (or Claude) → new chat → one short prompt.

**Step 2:** Confirm `session/request_permission` frames appear for a write or command. If they do not, that is a High — stop and treat Task 3/4 as incomplete. Do not "fix" it by setting `acp_backend_allow_ungated_tools`.

**Step 3:** Switch back to `""`. Do not leave the host on an experimental backend.

If the host has no adapter, skip. The preview-flag copy already says no successful non-kiro session has been observed. Merge-ready for an experimental preview is honest docs + tested refusal paths, not a subscription.

---

### Task 9: Squash locally, stop before a PR

Kiro Crew PRs are one commit (`prepare-pr` / `single_commit = true`). Squash now so a later PR is a fast-forward of one commit. **Do not push. Do not open a PR.**

**Step 1: Inspect**

```bash
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
```

**Step 2: Soft-reset and recommit**

```bash
git reset --soft origin/main
git commit -m "$(cat <<'EOF'
feat: drive ACP adapters as a selectable client backend

Adds Codex, Claude Code, goose, and a fail-closed registry path
behind the ACP-adapters preview flag. agent.provider stays acp.
Adapters are operator-installed. Ungovernable tool routing refuses
unless acp_backend_allow_ungated_tools is set.
EOF
)"
```

Do not `--no-verify`. If the hook reformats, make a **new** commit for the hook's edits (do not amend unless the conditions in the commit rule are met — here HEAD is yours and unpushed, but a hook failure must be a new commit).

**Step 3: Confirm**

```bash
git status
git log --oneline origin/main..HEAD
```

Expected: one commit, clean tree, branch still `feat/pluggable-acp-backends`. No remote update.

---

## Out of scope (file later, not this plan)

- goose `fs/*` and `terminal/*` client handlers (the thing that would make `CLIENT_DELEGATED` real)
- Dashboard control for `acp_backend_allow_ungated_tools`
- Translating the new card strings into the 12 languages beyond English copies
- Answering #1693 / API-key providers
- Opening or driving the GitHub PR (`prepare-pr` only after the user asks)
