"""Per-session work ledger — durable loop state that lives on disk, not in context.

Long-horizon sessions (monitor loops, goal loops) accumulate their working
state as prior transcript turns, which the harness-owned compaction then
summarizes lossily. This module gives every session one small on-disk store
for that state instead: a mutable **state record** (goal, phase, next intent,
tried approaches, artifact pointers) carrying a bounded **event tail**. The
context window becomes a cache; the ledger is the authority.

Layout (see docs/system-specs/features/session-work-ledger.md):

    <data_home>/ledger/<store-name>/
        slot_key        # breadcrumb: the exact ledger key this dir belongs to
        state.json      # the whole record, replaced atomically on every write
        .lock           # cross-process mutex inode (never replaced by writes)

Design notes, each earned by a review finding:

- **One document, one atomic write.** State and its event land in the same
  ``atomic_write`` (temp file + rename), so a crash between "phase moved" and
  "event logged" cannot exist — the phase-requires-event invariant is
  crash-atomic by construction, not by ordering. The event tail is bounded
  (``_MAX_EVENTS``), so the file cannot grow without limit and every read is
  O(record), never O(history).
- **Exact-key identity.** :func:`ledger_key` only strips the dashboard
  prefixes (the same strip the permanent-delete funnel applies); it never
  folds the key's charset. Uniqueness comes from a digest over the exact key
  inside :func:`_store_name` — a lossy charset fold as the identity would map
  distinct channel session keys (colon-structured) onto one ledger.
- **Bounded lock.** The per-ledger lock acquire is a bounded poll and fails
  closed with ``OSError`` — a wedged cross-process holder costs one refused
  write, never an executor thread parked forever.
- **Size ceiling before parse.** A state file past ``_MAX_STATE_BYTES`` is
  treated as corrupt/absent rather than parsed, so a hostile or damaged file
  cannot make every nudge fire allocate its size.

Callers pass keys through :func:`ledger_key`; this module never imports
dashboard state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import IS_POSIX, file_lock

if IS_POSIX:
    import fcntl

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Phases that end a workstream. ``finished_at`` is stamped when the record
#: enters one of these; anything else is an in-flight phase.
TERMINAL_PHASES = frozenset({"done", "abandoned"})

#: Vocabulary for event lines. A phase change REQUIRES one of these; an event
#: without a phase change coerces an unrecognized kind to ``note`` (the text
#: is the payload there, the kind only a filter).
EVENT_KINDS = frozenset({"progress", "decision", "tried", "blocked", "unblocked", "phase", "note"})

# Bounds. The ledger is injected into nudge turns and read back every cycle,
# so every field is capped at write time; a runaway writer degrades to a
# clamped record instead of an unbounded file.
_MAX_TEXT = 2000
_MAX_TRIED = 50
_MAX_ARTIFACTS = 32
_MAX_EVENTS = 100
_MAX_EVENT_TAIL = 20
#: Refuse to parse a state file past this size: with every field clamped the
#: legitimate maximum is well under it, so anything bigger is damage or
#: tampering, and parsing it would cost what the clamps exist to prevent.
_MAX_STATE_BYTES = 1_000_000

#: Lock acquire budget. Every in-tree critical section is a sub-millisecond
#: read + atomic rename, so this is a ceiling against a wedged cross-process
#: holder, not a normal wait. On expiry the write FAILS CLOSED with OSError.
_LOCK_TIMEOUT_SECS = 5.0
_LOCK_POLL_SECS = 0.05

_STATE_FILE = "state.json"
_KEY_FILE = "slot_key"
_LOCK_FILE = ".lock"

#: Identical fold to ``crew_chat._store_name`` — kept in lockstep so a slot
#: key and its stores share one spelling family. Reimplemented rather than
#: imported: ``crew_chat`` drags the whole crew orchestrator import graph into
#: what must stay a leaf module usable from the gateway boot path. The fold
#: shapes only the READABLE half of a directory name; identity is the digest
#: over the exact key.
_STORE_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_STORE_NAME_READABLE_MAX = 80


def ledger_key(session_key: str) -> str:
    """Fold a session/slot key to the ledger's identity spelling — LOSSLESSLY.

    Only the dashboard prefixes are stripped, because one dashboard session is
    legitimately spelled both ``dashboard_chat-X`` (history/API) and
    ``chat-X`` (live slot, nudge loop) and both must reach one ledger. Nothing
    else is rewritten: a charset fold here would be lossy, and two distinct
    channel session keys that fold to the same string would share a ledger —
    one session reading and overwriting another's state.
    """
    key = session_key or ""
    if key.startswith("dashboard:"):
        key = key[len("dashboard:") :]
    while key.startswith("dashboard_"):
        key = key[len("dashboard_") :]
    return key


def _store_name(slot_key: str) -> str:
    """Directory name for *slot_key*'s ledger — unique per EXACT key.

    Readable fold capped for filesystem name limits; uniqueness comes from the
    digest over the FULL key, so ``Foo``/``foo`` and long shared prefixes all
    get distinct directories on every filesystem. The name is not decodable
    back to the key — the key is persisted inside the store instead.
    """
    readable = _STORE_NAME_UNSAFE.sub("_", slot_key)[:_STORE_NAME_READABLE_MAX]
    digest = hashlib.sha256(slot_key.encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}"


def _ledger_root() -> Path:
    """Ledger root, resolved against the live data home per call.

    Never captured at import: an import-time binding freezes the data home and
    defeats pod isolation and test isolation (same rule as
    ``subagent_persistence._subagents_dir``).
    """
    return data_home() / "ledger"


def ledger_dir(slot_key: str) -> Path:
    """Validated per-session ledger directory for *slot_key*.

    Raises ``ValueError`` on an empty or path-hostile key. The fold in
    :func:`_store_name` already removes separators, but the raw key is checked
    too so a hostile key is refused loudly instead of silently folded, and the
    resolved path is required to stay inside the ledger root (symlink-safe).
    """
    if not slot_key or "\0" in slot_key or "/" in slot_key or "\\" in slot_key:
        raise ValueError(f"Invalid slot key for ledger: {slot_key!r}")
    base = _ledger_root()
    resolved = (base / _store_name(slot_key)).resolve()
    parent = base.resolve()
    if resolved == parent or not resolved.is_relative_to(parent):
        raise ValueError(f"Path traversal blocked for slot key: {slot_key!r}")
    return resolved


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clamp(value: Any, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


@contextmanager
def _locked(dir_path: Path) -> Iterator[None]:
    """Bounded cross-process exclusive lock over one ledger directory.

    The lock file is a dedicated inode that writes never replace (replacing
    the locked inode would let a second writer lock the NEW inode and
    interleave). The acquire is a bounded poll on POSIX (``LOCK_NB`` + sleep)
    and delegates to :func:`platform_compat.file_lock` on Windows, which is
    already a bounded spin; both FAIL CLOSED with ``OSError`` rather than
    entering the critical section unserialized or parking a worker thread on
    a wedged holder forever.
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    lock_path = dir_path / _LOCK_FILE
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if IS_POSIX:
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise OSError("ledger lock is held by another process; try again")
                    time.sleep(_LOCK_POLL_SECS)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        else:
            with file_lock(fd, exclusive=True):
                yield
    finally:
        os.close(fd)


def _empty_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "goal": "",
        "phase": "",
        "next": "",
        "tried": [],
        "artifacts": {},
        "events": [],
        "created_at": "",
        "last_progress_at": "",
        "finished_at": "",
    }


def _coerce_state(raw: Any) -> dict[str, Any]:
    """Fold whatever is on disk into a well-typed state record.

    Unknown fields are preserved (forward compatibility for a newer writer);
    known fields with the wrong type are reset to their defaults. Never raises.
    """
    state = _empty_state()
    if not isinstance(raw, dict):
        return state
    extra = {k: v for k, v in raw.items() if k not in state}
    for key in ("goal", "phase", "next", "created_at", "last_progress_at", "finished_at"):
        if isinstance(raw.get(key), str):
            state[key] = _clamp(raw[key])
    if isinstance(raw.get("tried"), list):
        tried: list[dict[str, str]] = []
        for item in raw["tried"]:
            if isinstance(item, dict) and isinstance(item.get("approach"), str):
                tried.append(
                    {
                        "approach": _clamp(item["approach"]),
                        "rejected_because": _clamp(item.get("rejected_because", "")),
                        "at": _clamp(item.get("at", ""), 64),
                    }
                )
        state["tried"] = tried[-_MAX_TRIED:]
    if isinstance(raw.get("artifacts"), dict):
        arts: dict[str, str] = {}
        for k, v in raw["artifacts"].items():
            if isinstance(k, str) and isinstance(v, str) and len(arts) < _MAX_ARTIFACTS:
                arts[_clamp(k, 128)] = _clamp(v)
        state["artifacts"] = arts
    if isinstance(raw.get("events"), list):
        events: list[dict[str, str]] = []
        for item in raw["events"]:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                events.append(
                    {
                        "ts": _clamp(item.get("ts", ""), 64),
                        "kind": _clamp(item.get("kind", ""), 32),
                        "text": _clamp(item["text"]),
                    }
                )
        state["events"] = events[-_MAX_EVENTS:]
    schema = raw.get("schema")
    if isinstance(schema, int) and not isinstance(schema, bool) and schema > 0:
        state["schema"] = schema
    state.update(extra)
    return state


def _read_state_unlocked(dir_path: Path) -> dict[str, Any]:
    path = dir_path / _STATE_FILE
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            logger.warning("ledger state file over size ceiling; treating as absent")
            return _empty_state()
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return _empty_state()
    return _coerce_state(raw)


def read_state(slot_key: str) -> dict[str, Any]:
    """Best-effort read of the state record. Absent/malformed reads as empty.

    Lock-free by design: ``atomic_write`` means a reader sees the old or the
    new document, never a torn one, and the events ride the same document so
    state and event tail are always from one transaction.
    """
    try:
        dir_path = ledger_dir(slot_key)
    except ValueError:
        return _empty_state()
    return _read_state_unlocked(dir_path)


def has_ledger(slot_key: str) -> bool:
    """Whether *slot_key* has ever recorded anything (cheap existence probe)."""
    try:
        return (ledger_dir(slot_key) / _STATE_FILE).exists()
    except ValueError:
        return False


def record(
    slot_key: str,
    *,
    goal: str | None = None,
    phase: str | None = None,
    next_step: str | None = None,
    tried_approach: str | None = None,
    tried_rejected_because: str | None = None,
    artifacts: dict[str, str] | None = None,
    event: str | None = None,
    event_kind: str | None = None,
) -> dict[str, Any]:
    """Apply one partial update to the state record, in one locked transaction.

    Enforces the crew-ledger discipline: passing *phase* without *event* and a
    recognized *event_kind* is refused with ``ValueError`` — a phase must
    never move without a logged, classified reason. State and event land in
    the SAME atomic write, so the invariant holds across crashes too.

    Returns the resulting state record.
    """
    if phase is not None:
        if not (event and event.strip()):
            raise ValueError("phase change requires an event: pass event + event_kind")
        if (event_kind or "").strip() not in EVENT_KINDS:
            kinds = ", ".join(sorted(EVENT_KINDS))
            raise ValueError(f"phase change requires event_kind (one of: {kinds})")
    dir_path = ledger_dir(slot_key)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        now = _now_iso()
        created = not state["created_at"]
        if created:
            state["created_at"] = now
            # Breadcrumb so the folded directory name can be mapped back to
            # its key by a human (the fold itself is not decodable).
            try:
                atomic_write(dir_path / _KEY_FILE, slot_key + "\n", mode=0o600)
            except OSError:
                logger.debug("ledger: slot_key breadcrumb write failed", exc_info=True)
        if goal is not None:
            state["goal"] = _clamp(goal)
        if phase is not None:
            state["phase"] = _clamp(phase, 128)
            state["finished_at"] = now if state["phase"] in TERMINAL_PHASES else ""
        if next_step is not None:
            state["next"] = _clamp(next_step)
        if tried_approach:
            tried = list(state["tried"])
            tried.append(
                {
                    "approach": _clamp(tried_approach),
                    "rejected_because": _clamp(tried_rejected_because or ""),
                    "at": now,
                }
            )
            state["tried"] = tried[-_MAX_TRIED:]
        if artifacts:
            merged = dict(state["artifacts"])
            for k, v in artifacts.items():
                if isinstance(k, str) and isinstance(v, str):
                    merged[_clamp(k, 128)] = _clamp(v)
            # Cap by insertion order: oldest pointers age out first.
            if len(merged) > _MAX_ARTIFACTS:
                merged = dict(list(merged.items())[-_MAX_ARTIFACTS:])
            state["artifacts"] = merged
        if event and event.strip():
            kind = (event_kind or "").strip()
            if kind not in EVENT_KINDS:
                kind = "note"
            events = list(state["events"])
            events.append({"ts": now, "kind": kind, "text": _clamp(event.strip())})
            state["events"] = events[-_MAX_EVENTS:]
        state["last_progress_at"] = now
        state["schema"] = SCHEMA_VERSION
        atomic_write(dir_path / _STATE_FILE, json.dumps(state) + "\n", mode=0o600)
        return state


def read_events(slot_key: str, limit: int = _MAX_EVENT_TAIL) -> list[dict[str, str]]:
    """Tail of the event log, newest last."""
    return read_state(slot_key)["events"][-max(1, limit) :]


def purge(slot_key: str) -> None:
    """Delete *slot_key*'s ledger directory. Best-effort, never raises.

    Ledger content is disposable intermediate state — nothing reconstructs
    from it — so this runs unconditionally on permanent session deletion.
    A write racing the delete can at worst recreate an orphan directory that
    the next delete sweeps; it can never touch another session's ledger, so
    the funnel narrows the window (purge after the slot's turn is torn down)
    instead of buying a tombstone protocol for disposable state.
    """
    try:
        dir_path = ledger_dir(slot_key)
    except ValueError:
        return
    shutil.rmtree(dir_path, ignore_errors=True)


#: Ceiling for the injected snapshot block. A nudge turn carries this every
#: cycle, so it must stay small even against a clamped-but-full record.
_SNAPSHOT_MAX_CHARS = 1600
_SNAPSHOT_FIELD_MAX = 300
_SNAPSHOT_TRIED_TAIL = 3


def render_snapshot(slot_key: str) -> str:
    """Compact ``[work ledger]`` block for per-cycle injection, or ``""``.

    Empty when the session has no ledger, the record is empty, or the
    workstream is finished — a terminal ledger has nothing to steer. Reads
    are lock-free (see :func:`read_state`); callers on an event loop must
    dispatch this to a worker thread — it does filesystem I/O.
    """
    if not has_ledger(slot_key):
        return ""
    state = read_state(slot_key)
    if not any((state["goal"], state["phase"], state["next"], state["tried"], state["artifacts"])):
        return ""
    if state["phase"] in TERMINAL_PHASES:
        return ""

    def _field(v: str) -> str:
        v = " ".join(v.split())
        return v[:_SNAPSHOT_FIELD_MAX]

    lines = [
        "[work ledger — durable state for this session; authoritative over memory of prior cycles]"
    ]
    if state["goal"]:
        lines.append(f"goal: {_field(state['goal'])}")
    if state["phase"]:
        lines.append(f"phase: {_field(state['phase'])}")
    if state["next"]:
        lines.append(f"next: {_field(state['next'])}")
    for item in state["tried"][-_SNAPSHOT_TRIED_TAIL:]:
        why = f" (rejected: {_field(item['rejected_because'])})" if item["rejected_because"] else ""
        lines.append(f"tried: {_field(item['approach'])}{why}")
    for k, v in state["artifacts"].items():
        lines.append(f"artifact {k}: {_field(v)}")
    block = "\n".join(lines)
    if len(block) > _SNAPSHOT_MAX_CHARS:
        block = block[: _SNAPSHOT_MAX_CHARS - 1] + "…"
    return block
