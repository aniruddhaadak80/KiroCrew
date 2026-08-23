"""KiroCrew snapshot and restore — portable state management."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from kiro_crew import pinned_fs, platform_compat

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

try:
    from kiro_crew.config.loader import DASHBOARD_PORT as _DASHBOARD_PORT
except Exception:  # pragma: no cover - optional during early/standalone import
    _DASHBOARD_PORT = int(os.environ.get("KIROCREW_PORT", 5476))

VALID_COMPONENTS = ("memory", "crons", "config", "skills", "workspace", "notifications", "security")

# Files that must always have 0o600 permissions in snapshots and on restore.
SECURITY_SENSITIVE_FILES: frozenset = frozenset({"sel_hmac.key", "telemetry_salt"})

# Files that must NEVER ride a snapshot: sel_hmac.key is regenerated on restore
# so audit-log HMACs stay bound to the host that wrote them.
#
# This set is matched by BASENAME inside `_data_filter`, which runs over the
# ENTIRE tar — including the staged workspace/, plan_memory/ and skills/ trees.
# So any name added here also silently drops a USER file that happens to share
# it. Keep the set minimal for that reason.
#
# The beacon's per-install identity (beacon_install_id / beacon_last_sent) is
# deliberately NOT here: snapshot staging copies an explicit per-component file
# list (CORE_FILES) plus those three directories, and no component lists a beacon
# file, so a root beacon file is never staged in the first place. The
# id-cloning hazard is closed by that non-selection, not by a basename filter.
NEVER_SNAPSHOT_FILES: frozenset = frozenset({"sel_hmac.key"})


def _data_filter(info: tarfile.TarInfo, _dest: str = "") -> tarfile.TarInfo | None:
    """Equivalent to tarfile ``"data"`` filter (Python 3.12+), with 3.10 fallback.

    Also rejects path traversal, symlinks, and hardlinks to eliminate TOCTOU
    race between pre-scan and extraction.
    Excludes sel_hmac.key (must be regenerated on restore, not shipped).
    Security-sensitive files get 0o600 permissions.
    """
    # Reject path traversal. POSIX checks apply everywhere; the Windows-syntax
    # checks (backslash separators, drive letters — incl. the drive-RELATIVE
    # `C:foo` form is_absolute() misses, which resolves against the drive CWD
    # at extraction) apply ONLY when extracting on Windows, where tarfile
    # honors '\' as a native separator. They must NOT run on POSIX: ':' and
    # '\' are legal characters in Linux/macOS filenames, so a workspace file
    # named `a:1` or `notes..\old` would be silently dropped from a
    # Linux-to-Linux restore.
    name = info.name
    traversal = (
        name.startswith("/")
        or ".." in PurePosixPath(name).parts
        or PurePosixPath(name).is_absolute()
    )
    if not traversal and platform_compat.IS_WINDOWS:
        traversal = (
            name.startswith("\\")
            or ".." in PureWindowsPath(name).parts
            or PureWindowsPath(name).is_absolute()
            or bool(PureWindowsPath(name).drive)
        )
    if traversal:
        print(f"⚠️  Rejecting path traversal entry: {info.name}")
        return None
    # Reject symlinks and hardlinks
    if info.issym() or info.islnk():
        print(f"⚠️  Rejecting symlink/hardlink entry: {info.name}")
        return None
    # Never ship these — each must be regenerated on the restoring host.
    basename = PurePosixPath(info.name).name
    if basename in NEVER_SNAPSHOT_FILES:
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    # Security-sensitive files get restricted permissions
    if not info.isdir() and basename in SECURITY_SENSITIVE_FILES:
        info.mode = 0o600
    else:
        info.mode = 0o755 if info.isdir() else 0o644
    return info


def _default_snapshot_dir() -> str:
    """Return snapshot directory from config, falling back to <config_dir>/snapshots."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        d = KiroCrewConfig.load().snapshot_dir
        if d:
            return str(Path(d).expanduser())
    except Exception:
        pass
    try:
        from kiro_crew.config.paths import config_dir

        return str(config_dir() / "snapshots")
    except Exception:
        return str(Path.home() / ".kiro" / "crew" / "snapshots")


def _audit(event_type: str, resources: str) -> None:
    """Emit a SEL audit event for snapshot/restore operations."""
    try:
        from kiro_crew.sel import SecurityEvent, sel

        sel().log(
            SecurityEvent(
                event_id=os.urandom(8).hex(),
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type=event_type,
                caller_identity=os.environ.get("USER", "unknown"),
                agent="kirocrew",
                source="cli",
                operation=event_type,
                outcome="completed",
                resources=resources,
            )
        )
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("SEL audit event '%s' failed: %s", event_type, e)


CORE_FILES: dict[str, tuple[str, ...]] = {
    "memory": ("memory.db", "memory_index.db"),
    "crons": ("crons.json",),
    "config": ("config.json", "session_map.json", "hooks.json", "project_dir", "workspace_dir"),
    "notifications": ("notifications.jsonl",),
    "security": ("telemetry_salt",),  # sel_hmac.key excluded — regenerated on restore
}

COMPONENT_HELP = {
    "memory": "memory.db, memory_index.db (semantic, episodic, knowledge graph)",
    "crons": "crons.json (scheduled jobs)",
    "config": "config.json, session_map.json, hooks.json, project_dir, workspace_dir",
    "skills": "skills/ directory",
    "workspace": "workspace/, plan_memory/ directories",
    "notifications": "notifications.jsonl (notification history)",
    "security": "telemetry_salt (sel_hmac.key excluded — regenerated on restore)",
}


def _mc_dir() -> Path:
    # Use the shared resolver so snapshot/restore honor the documented
    # KIROCREW_HOME override (and the same ~/.kiro/crew default) as every other
    # module — not an undocumented KIROCREW_DIR, which would make snapshots
    # silently target the real home even when state was relocated.
    from kiro_crew.config.loader import config_dir

    return config_dir()


def _fsize(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _want(components: list[str] | None, name: str) -> bool:
    return components is None or name in components


def _list_components() -> None:
    print("Available components:")
    for k, v in COMPONENT_HELP.items():
        print(f"  {k:16s} {v}")
    print("\nCombine with commas: --components memory,crons,skills")


def _report_skip(reason: str, path: str) -> None:
    """Word a primitive's skip classification in this module's existing voice.

    The primitive classifies and never prints, so these strings stay byte-identical
    to what snapshot/restore printed before the migration.
    """
    if reason == pinned_fs.SKIP_SYMLINK:
        print(f"⚠️  Skipping symlink in source tree: {path}")
    elif reason == pinned_fs.SKIP_VANISHED:
        print(f"⚠️  Skipping vanished entry during snapshot copy: {path}")
    else:
        print(f"⚠️  Skipping hardlinked or non-regular file during snapshot copy: {path}")


def _staging_is_pinned(*, allow_unpinned: bool, what: str) -> bool:
    """Whether staging may proceed, and whether it will be descriptor-pinned.

    Returns True for a pinned traversal, False for the by-name traversal the caller
    explicitly asked for. Raises rather than returning False when the platform cannot
    pin and no one said that is acceptable.

    This is the whole "refuse rather than fall back" rule, in one place. The reason it
    is a refusal and not a warning: a by-name walk is not a slightly weaker version of
    a pinned walk, it is the mechanism whose failure closed two pull requests. An
    operator who needs a snapshot on a platform without ``dir_fd`` can still have one,
    but they say so on the command line and the archive records that they did, so the
    weaker mode is never something the tool chose on their behalf.
    """
    if pinned_fs.supports_pinned_tree_walk():
        return True
    if allow_unpinned:
        return False
    raise pinned_fs.PinnedPathRefusal(
        f"refusing to stage the {what}: this platform cannot open a directory "
        "relative to a descriptor, so every component would be re-opened by name and "
        "an ancestor swapped mid-walk could redirect the copy into a credential "
        "store. Re-run with --allow-unpinned-staging to accept a by-name traversal; "
        "the archive will record that it was staged unpinned."
    )


def _copytree_safe(
    src: Path,
    dst: Path,
    *,
    allow_unpinned: bool = False,
    on_skip: pinned_fs.SkipReporter | None = None,
    **kwargs,
) -> None:
    """Copy a tree for staging, with the source traversal pinned where possible.

    Was: ``shutil.copytree`` with an ignore callback that tested ``os.path.islink`` on
    a NAME. That screened the final component of each entry and nothing else, so an
    ancestor directory swapped for a link between the listing and the copy redirected
    every deeper open, and the screen had nothing to report -- what it found inside
    the replaced tree was an ordinary file. Now the traversal is descriptor-pinned by
    :func:`kiro_crew.pinned_fs.stage_tree_pinned`, including the chain above the root.

    ``dirs_exist_ok`` is accepted and ignored: the pinned walk always creates
    destination directories with ``exist_ok=True``, so the flag has no remaining
    meaning. Every other keyword is rejected rather than silently dropped.

    *on_skip* lets a caller both print and RECORD what was skipped. It defaults to
    printing only, which is right for restore; the snapshot path passes a recorder so
    an incomplete archive says so in its own manifest instead of only in the console
    output of whoever ran it.
    """
    report = on_skip or _report_skip
    outer_ignore = kwargs.pop("ignore", None)
    kwargs.pop("dirs_exist_ok", None)
    if kwargs:
        raise TypeError(f"_copytree_safe got unexpected keyword arguments: {sorted(kwargs)}")

    if _staging_is_pinned(allow_unpinned=allow_unpinned, what=f"tree {src.name!r}"):
        pinned_fs.stage_tree_pinned(
            src,
            dst,
            what=f"tree {src.name!r}",
            ignore=outer_ignore,
            on_skip=report,
        )
        return

    # Declared by-name traversal. The link screen is kept because it is still the
    # only thing standing between staging and a symlinked entry, but it protects
    # final components only -- which is exactly why reaching this branch has to be
    # asked for.
    def _ignore_symlinks(directory, contents):
        skipped = {name for name in contents if os.path.islink(os.path.join(directory, name))}
        for name in skipped:
            report(pinned_fs.SKIP_SYMLINK, os.path.join(directory, name))
        if outer_ignore:
            skipped |= set(outer_ignore(directory, contents))
        return skipped

    shutil.copytree(str(src), str(dst), ignore=_ignore_symlinks, dirs_exist_ok=True)


def _copy_tree_no_overwrite(src: Path, dst: Path, *, allow_unpinned: bool = False) -> None:
    """Merge *src* into *dst* without overwriting, with both ends pinned.

    The destination side is where #3797's third finding lives. The previous version
    walked the source with ``rglob`` and wrote each file with ``shutil.copy2`` to a
    path composed by name, so the destination's ancestor chain was never pinned: a
    component of *dst* swapped for a link after ``mkdir`` redirected the write, and
    ``not target.exists()`` answered for whatever the link pointed at rather than for
    the directory the caller validated.

    This is now one call into the shared primitive with ``skip_existing=True``, which
    is what makes the no-overwrite promise real: exclusive creation is atomic, so "it
    did not exist a moment ago" and "this call created it" are the same statement
    rather than two with a window between them.

    An earlier revision open-coded a second pinned walk here, with its own copy body.
    Review pointed out the two had already diverged -- this one's child-directory open
    lacked the ``ELOOP``/``ENOTDIR`` handling, so the very swap the staging walk skips
    would have escaped restore as a raw ``OSError`` -- which is the argument for a
    parameter on one primitive rather than a parallel implementation the shared
    module's own docstring says should not exist.
    """
    if not _staging_is_pinned(allow_unpinned=allow_unpinned, what=f"restore of {dst.name!r}"):
        for item in src.rglob("*"):
            if item.is_symlink():
                continue
            target = dst / item.relative_to(src)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.is_file() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(item), str(target))
        return

    pinned_fs.stage_tree_pinned(
        src,
        dst,
        what=f"restore of {dst.name!r}",
        on_skip=_report_skip,
        skip_existing=True,
    )


# ── Snapshot ──────────────────────────────────────────────────────────────────


def _db_source_is_safe(src: Path, *, on_skip: pinned_fs.SkipReporter) -> bool:
    """Whether a SQLite source may be handed to ``sqlite3.connect`` by name.

    ``sqlite3`` cannot open a descriptor, so this is the one place on the staging path
    that must name a path. The inode is still judged first, on an open descriptor, which
    is what makes the hardlink case answerable at all: an alias shares its target's
    inode, so ``realpath`` yields the alias's own name and no path test can see it.

    Returns False (and reports the omission) rather than raising, matching how every
    other unusable source on this path is handled -- the archive records what it left
    out.
    """
    try:
        fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            on_skip(pinned_fs.SKIP_SYMLINK, str(src))
            return False
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            on_skip(pinned_fs.SKIP_NOT_REGULAR, str(src))
            return False
        return True
    finally:
        os.close(fd)


def _build_snapshot(mc: Path, out: Path, name: str, *, allow_unpinned: bool = False) -> Path:
    """Stage the data home into a temporary tree and publish it as one tarball.

    Extracted from ``snapshot_main`` so the staging pass has a boundary a refusal can
    be contained at: everything in here either produces a finished archive or raises,
    and the caller turns a :class:`kiro_crew.pinned_fs.PinnedPathRefusal` into an exit
    code rather than a traceback.
    """
    # Decided BEFORE anything is staged, not per-tree. An earlier revision gated
    # inside _copytree_safe only, so a data home with core files and no trees staged
    # them on a platform that cannot pin without ever consulting the opt-in -- the
    # gate was reachable only through a path that happened to exist. Asking once, up
    # front, is also what makes the manifest's "staging" value true of the whole
    # archive rather than of whichever component ran last.
    pinned = _staging_is_pinned(allow_unpinned=allow_unpinned, what="data home")

    # Every skip is recorded, not just printed. A snapshot that omitted a hardlinked
    # or symlinked file used to report success with a console warning and nothing in
    # the archive -- the same "silent partial" shape this change fixes on the restore
    # side, raised in review. Paths are stored relative to the data home so the record
    # names the file without carrying the absolute layout of the machine into an
    # archive that may be moved somewhere else.
    skipped: list[dict[str, str]] = []

    def _record_skip(reason: str, path: str) -> None:
        try:
            rel = str(Path(path).relative_to(mc))
        except ValueError:
            rel = Path(path).name
        skipped.append({"reason": reason, "path": rel})
        _report_skip(reason, path)

    with tempfile.TemporaryDirectory() as work:
        stage = Path(work) / name
        for d in ("workspace", "skills", "plan_memory"):
            (stage / d).mkdir(parents=True, exist_ok=True)

        # Core files. Copied through the pinned primitive rather than shutil.copy2:
        # copy2 dereferences a hardlink into ordinary-looking regular bytes, and the
        # tar pass's hardlink screen then has no link left to reject, so an alias
        # planted at a core file's name would have shipped as content. The name-based
        # islink check is gone with it -- it answered about a name, and the open that
        # followed could land on a different inode.
        #
        # Both ends are pinned where the platform allows it: the data home is opened
        # once and every core file is opened relative to THAT descriptor, so an
        # ancestor of the data home swapped mid-run cannot redirect the read. Opening
        # `mc / f` by name was a real gap in the first revision of this PR, caught in
        # review -- the file's own O_NOFOLLOW says nothing about the directories walked
        # to reach it.
        mc_fd = pinned_fs.open_dir_pinned(mc, what="data home") if pinned else None
        try:
            for files in CORE_FILES.values():
                for f in files:
                    src = mc / f
                    if not src.is_file():
                        continue
                    if f.endswith(".db"):
                        # sqlite3 has no fd-based open, so the connection below must
                        # name a path. That does NOT mean the inode cannot be judged
                        # first: the name is opened here, the DESCRIPTOR is checked,
                        # and only then is sqlite pointed at the path.
                        #
                        # Both screens matter and for different reasons. A symlink is
                        # refused because the connection would follow it. A HARDLINK is
                        # refused because it is invisible to every path check -- review
                        # found that aliasing memory.db onto a credential-bearing
                        # database elsewhere would have had SQLite faithfully back up
                        # its token rows into the archive, with no link for any name
                        # test to see. That is the same alias hazard copy_file_pinned
                        # exists for, and the .db path had been left out of it.
                        if not _db_source_is_safe(src, on_skip=_record_skip):
                            continue
                        with (
                            closing(sqlite3.connect(str(src))) as src_conn,
                            closing(sqlite3.connect(str(stage / f))) as dst_conn,
                        ):
                            src_conn.backup(dst_conn)
                    elif mc_fd is not None:
                        pinned_fs.copy_file_pinned(
                            str(src),
                            str(stage / f),
                            dir_fd=mc_fd,
                            name=f,
                            on_skip=_record_skip,
                        )
                    else:
                        pinned_fs.copy_file_pinned(str(src), str(stage / f), on_skip=_record_skip)
        finally:
            if mc_fd is not None:
                os.close(mc_fd)

        # Workspace (exclude hygiene_data, insert_facts*.py)
        if (mc / "workspace").is_dir():
            _copytree_safe(
                mc / "workspace",
                stage / "workspace",
                allow_unpinned=allow_unpinned,
                on_skip=_record_skip,
                ignore=shutil.ignore_patterns("hygiene_data", "insert_facts*.py"),
            )

        # Plan memory
        if (mc / "plan_memory").is_dir():
            _copytree_safe(
                mc / "plan_memory",
                stage / "plan_memory",
                allow_unpinned=allow_unpinned,
                on_skip=_record_skip,
            )

        # Skills
        if (mc / "skills").is_dir():
            _copytree_safe(
                mc / "skills",
                stage / "skills",
                allow_unpinned=allow_unpinned,
                on_skip=_record_skip,
            )

        # Manifest
        ws_files = sum(1 for _ in (stage / "workspace").rglob("*") if _.is_file())
        pm_files = sum(1 for _ in (stage / "plan_memory").rglob("*") if _.is_file())
        sk_count = sum(1 for _ in (stage / "skills").iterdir() if _.is_dir())
        # Recorded so a reader can tell how the archive was built. "unpinned" means
        # the trees were walked by name, which an ancestor swap during staging could
        # have redirected. Someone deciding whether to trust this archive needs that
        # on the record rather than in the memory of whoever ran the command.
        staging_mode = "pinned" if pinned else "unpinned"
        manifest = {
            "version": 2,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hostname": socket.gethostname(),
            "user": os.environ.get("USER", "unknown"),
            "kirocrew_dir": str(mc),
            "staging": staging_mode,
            "skipped": skipped,
            "contents": {
                "memory_db": _fsize(stage / "memory.db"),
                "memory_index_db": _fsize(stage / "memory_index.db"),
                "crons_json": _fsize(stage / "crons.json"),
                "config_json": _fsize(stage / "config.json"),
                "notifications_jsonl": _fsize(stage / "notifications.jsonl"),
                "workspace_files": ws_files,
                "plan_memory_files": pm_files,
                "skill_count": sk_count,
            },
        }
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
        if staging_mode == "unpinned":
            print(
                "⚠️  Staged by path name (--allow-unpinned-staging): this platform "
                "cannot pin a directory by descriptor, so an ancestor swapped during "
                "staging could have redirected a copy. Recorded in MANIFEST.json."
            )

        # Tarball — write to temp file and rename atomically to avoid corrupt partials
        out.mkdir(parents=True, exist_ok=True)
        outfile = out / f"{name}.tar.gz"
        tmp_tar = outfile.with_suffix(".tar.gz.tmp")
        try:
            with tarfile.open(str(tmp_tar), "w:gz") as tar:
                tar.add(str(stage), arcname=name, filter=_data_filter)
            tmp_tar.rename(outfile)
        except BaseException:
            tmp_tar.unlink(missing_ok=True)
            raise
    return outfile


def snapshot_main(
    argv: list[str] | None = None, *, parsed: argparse.Namespace | None = None
) -> int:
    if parsed is None:
        p = argparse.ArgumentParser(
            prog="kirocrew-snapshot",
            description="Create a portable .tar.gz snapshot of Kiro Crew state.",
        )
        p.add_argument("output_dir", nargs="?", default=_default_snapshot_dir())
        p.add_argument("--keep", type=int, default=7)
        p.add_argument("--list", action="store_true", dest="list_snapshots")
        p.add_argument(
            "--allow-unpinned-staging",
            action="store_true",
            dest="allow_unpinned",
            help=(
                "Stage by path name on a platform that cannot open a directory "
                "relative to a descriptor. Without this the snapshot is refused there "
                "rather than taken with a traversal an ancestor swap could redirect. "
                "The archive's MANIFEST.json records that it was staged unpinned."
            ),
        )
        parsed = p.parse_args(argv)
    args = parsed
    allow_unpinned = bool(getattr(args, "allow_unpinned", False))

    if args.keep <= 0:
        print(f"❌ --keep value must be a positive integer, got: {args.keep}")
        return 1

    out = Path(args.output_dir or _default_snapshot_dir())

    if args.list_snapshots:
        if not out.is_dir():
            print(f"No snapshots found in {out}")
            return 0
        snaps = sorted(
            out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True
        )
        for s in snaps:
            print(s)
        if not snaps:
            print(f"No snapshots found in {out}")
        return 0

    mc = _mc_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"kirocrew-snapshot-{ts}"

    # Pre-flight size estimate
    if mc.is_dir():
        total_bytes = sum(
            f.stat().st_size for f in mc.rglob("*") if f.is_file() and not f.is_symlink()
        )
        total_mb = total_bytes / (1024 * 1024)
        if total_mb > 500:
            print(f"⚠️  {mc} is {total_mb:.0f} MB — snapshot may be large and slow")

    # WAL checkpoint
    if (mc / "memory.db").is_file():
        try:
            with closing(sqlite3.connect(str(mc / "memory.db"))) as c:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            print(
                "⚠️  WAL checkpoint failed (DB may be locked by gateway). "
                "The backup API still produces a consistent copy."
            )

    try:
        outfile = _build_snapshot(mc, out, name, allow_unpinned=allow_unpinned)
    except pinned_fs.PinnedPathRefusal as exc:
        # A refusal is a decision this command made on purpose. A traceback would
        # read like a crash and bury the sentence saying what to do about it.
        print(f"❌ {exc}")
        return 1

    sz = outfile.stat().st_size
    # restrict_to_owner (fail-loud), NOT chmod_safe: this tarball can contain
    # sel_hmac.key (see the warning below). chmod_safe swallows OSError and
    # would let the snapshot land group/world-readable while still printing
    # success. Fail loudly instead — better to abort than ship a
    # secret-bearing archive under-protected. POSIX applies chmod 0o600;
    # Windows applies an owner-only DACL via icacls.
    # Unlink+reraise on failure so the "abort" the comment promises actually
    # removes the exposed artifact — otherwise the tarball would sit on disk
    # with the destination's inherited DACL after a Python traceback.
    try:
        platform_compat.restrict_to_owner(str(outfile))
    except OSError:
        outfile.unlink(missing_ok=True)
        raise
    human = f"{sz // 1024}K" if sz < 1024 * 1024 else f"{sz / 1024 / 1024:.1f}M"
    print(f"✅ Snapshot created: {outfile} ({human})")

    _audit("snapshot_created", f"{outfile} ({human})")

    # Prune
    snaps = sorted(
        out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True
    )
    for old in snaps[args.keep :]:
        old.unlink()
        print(f"🗑  Pruned: {old.name}")

    remaining = len(list(out.glob("kirocrew-snapshot-*.tar.gz")))
    print(f"📦 Snapshots in {out}: {remaining} (keep={args.keep})")
    return 0


# ── Restore ───────────────────────────────────────────────────────────────────


def _print_manifest(snap: Path) -> None:
    mf = snap / "MANIFEST.json"
    if not mf.is_file():
        return
    try:
        m = json.loads(mf.read_text())
        print("📋 Snapshot info:")
        print(f"  Created: {m.get('created_at', 'unknown')}")
        print(f"  From: {m.get('user', 'unknown')}@{m.get('hostname', 'unknown')}")
        c = m.get("contents", {})
        print(f"  Memory DB: {c.get('memory_db', 0) // 1024} KB")
        print(f"  Crons: {c.get('crons_json', 0) // 1024} KB")
        print(f"  Workspace files: {c.get('workspace_files', 0)}")
        print(f"  Skills: {c.get('skill_count', 0)}")
        print(f"  Notifications: {c.get('notifications_jsonl', 0) // 1024} KB")
        print(f"  Plan memory files: {c.get('plan_memory_files', 0)}")
        # Both of these are the record that makes an incomplete or weaker archive
        # visible. A value written but never displayed is only findable by untarring
        # the archive by hand, which is not a reader -- so they are shown here, where
        # anyone inspecting a snapshot before restoring it already looks.
        if m.get("staging") == "unpinned":
            print("  ⚠️  Staged by path name (unpinned): see --allow-unpinned-staging")
        for entry in m.get("skipped") or ():
            print(f"  ⚠️  Omitted ({entry.get('reason', '?')}): {entry.get('path', '?')}")
    except Exception as e:
        print(f"  (Could not read manifest: {e})")


_MERGE_ALLOWED_TABLES = frozenset(
    {
        "semantic_memory",
        "episodic_memories",
        "knowledge_facts",
        "knowledge_edges",
    }
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """Validate a SQL identifier against allowlist pattern. Raises ValueError if invalid."""
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def _merge_memory(src_db: Path, dst_db: Path) -> None:
    # Integrity check on source DB before ATTACH
    try:
        with sqlite3.connect(str(src_db)) as check_conn:
            result = check_conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if result != "ok":
            print(f"  ⚠️  Source DB integrity check failed: {result} — skipping merge")
            return
    except Exception as e:
        print(f"  ⚠️  Source DB unreadable: {e} — skipping merge")
        return

    conn = sqlite3.connect(str(dst_db))
    conn.execute("BEGIN")
    attached = False
    try:
        conn.execute("ATTACH DATABASE ? AS src", (str(src_db),))
        attached = True
        for table, cols, where in [
            (
                "semantic_memory",
                "key, value_json, confidence, source, created_at, updated_at, embedding",
                "WHERE is_deleted=0",
            ),
            (
                "episodic_memories",
                "id, conversation_id, text, embedding, tags, importance, created_at, last_accessed_at",
                "WHERE is_deleted=0",
            ),
            ("knowledge_facts", "subject, predicate, object, episode_id, created_at", ""),
            (
                "knowledge_edges",
                "source_key, target_key, relation, weight, metadata, created_at",
                "",
            ),
        ]:
            if table not in _MERGE_ALLOWED_TABLES:
                raise ValueError(f"Table {table!r} not in merge allowlist")
            for col in cols.split(", "):
                _validate_identifier(col.strip())
            try:
                before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({cols}) "
                    f"SELECT {cols} FROM src.{table} {where}"
                )
                after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                label = table.replace("_", " ").title()
                print(f"  {label} imported: {after - before}")
            except sqlite3.OperationalError as e:
                import logging

                logging.getLogger(__name__).warning("Skipping table %s: %s", table, e)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if attached:
            try:
                conn.execute("DETACH DATABASE src")
            except Exception:
                pass
        conn.close()


def _merge_crons(src_path: Path, dst_path: Path) -> None:
    try:
        src = json.loads(src_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ⚠️  Could not read {src_path}: {exc} — skipping cron merge")
        return
    try:
        dst = json.loads(dst_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ⚠️  Could not read {dst_path}: {exc} — skipping cron merge")
        return
    existing = {j.get("name") for j in dst.get("jobs", [])}
    imported = 0
    for job in src.get("jobs", []):
        name = job.get("name")
        if not name or name in existing:
            continue
        job["id"] = hashlib.md5(f"{name}-imported".encode(), usedforsecurity=False).hexdigest()[:8]
        dst.setdefault("jobs", []).append(job)
        imported += 1
    dst_path.write_text(json.dumps(dst, indent=2))
    total = len(src.get("jobs", []))
    print(f"  Cron jobs imported: {imported} (skipped {total - imported} duplicates)")


def _merge_notifications(src_path: Path, dst_path: Path) -> None:
    existing: set[str] = set()
    with open(dst_path) as f:
        for line in f:
            try:
                existing.add(json.loads(line).get("ts") or line.strip())
            except (ValueError, TypeError):
                pass
    imported = 0
    with open(dst_path, "a") as out, open(src_path) as f:
        for line in f:
            try:
                key = json.loads(line).get("ts") or line.strip()
                if key not in existing:
                    out.write(line)
                    existing.add(key)
                    imported += 1
            except (ValueError, TypeError):
                pass
    print(f"  Notifications imported: {imported}")


def _backup_and_copy(
    mc: Path, backup: Path, snap: Path, component: str, *, allow_unpinned: bool = False
) -> None:
    """Move the live core files aside, then restore the archive's, destination pinned.

    The restore side used to compose ``mc / f`` and hand it to ``shutil.copy2``, so
    the destination was reached by name every time. Two consequences, both real: a
    component of the data home swapped for a link redirected the write out of the
    data home entirely, and a symlink left at the core file's own name was written
    THROUGH rather than refused -- the name-based ``islink`` check above skipped the
    backup move and then the copy followed the link it had just declined to move.

    Now the data home is pinned once and each file is created relative to that
    descriptor with ``O_EXCL``. A name that is still occupied after the backup move
    is refused instead of written through, which is the symlink case above.
    """
    if not _staging_is_pinned(allow_unpinned=allow_unpinned, what=f"restore of {component!r}"):
        for f in CORE_FILES.get(component, ()):
            if (mc / f).is_file():
                if os.path.islink(mc / f):
                    print(f"⚠️  Skipping symlinked core file during backup: {mc / f}")
                    continue
                shutil.move(str(mc / f), str(backup / f))
            if (snap / f).is_file():
                if os.path.islink(snap / f):
                    print(f"⚠️  Skipping symlinked file from snapshot: {snap / f}")
                    continue
                shutil.copy2(str(snap / f), str(mc / f))
                _lock_down_restored(mc / f, component)
        return

    src_fd = pinned_fs.open_dir_pinned(snap, what=f"snapshot payload for {component!r}")
    try:
        dst_fd = pinned_fs.open_dir_pinned(mc, what=f"data home for {component!r}")
        try:
            backup_fd = pinned_fs.create_and_open_dir_pinned(
                backup, what=f"pre-restore backup for {component!r}"
            )
            try:
                for f in CORE_FILES.get(component, ()):
                    live = mc / f
                    # A symlink at a core file's name is MOVED aside like any other
                    # occupant, not skipped. The old code skipped the move and then let
                    # the copy write through the very link it had just declined to
                    # move; skipping the whole entry instead would be no better,
                    # because the archive's version of that file would then silently
                    # never be restored. Moving a symlink moves the LINK, never its
                    # target, so nothing outside the data home is touched.
                    #
                    # The move goes through both pinned descriptors rather than
                    # shutil.move on two composed paths: review pointed out that a
                    # by-name move re-resolves both ends, so an ancestor swapped
                    # between the check and the move would relocate something else.
                    # os.rename with src_dir_fd/dst_dir_fd cannot be redirected, and it
                    # is atomic within the data home, which a copy-then-delete is not.
                    if live.is_symlink():
                        print(f"⚠️  Moving symlinked core file aside during backup: {live}")
                        os.rename(f, f, src_dir_fd=dst_fd, dst_dir_fd=backup_fd)
                    elif live.is_file():
                        os.rename(f, f, src_dir_fd=dst_fd, dst_dir_fd=backup_fd)
                    if not (snap / f).is_file():
                        continue
                    try:
                        copied = pinned_fs.copy_file_pinned(
                            str(snap / f),
                            dir_fd=src_fd,
                            name=f,
                            dst_dir_fd=dst_fd,
                            dst_name=f,
                            # Owner-only applied through the destination DESCRIPTOR, in
                            # the same call that wrote the bytes. Two things wrong with
                            # the previous _lock_down_restored(mc / f) here, both raised
                            # in review: it reopened the freshly written file BY NAME, so
                            # a link swapped in at that instant had restrict_to_owner
                            # change the permissions of whatever it pointed at; and the
                            # mode cannot be inherited from the archive, which is
                            # untrusted input -- a hand-built tarball can record 0o777 on
                            # telemetry_salt. The reviewer's suggested fix was to drop
                            # the lockdown because "the copy already applies mode", which
                            # would have done exactly that: applied the ARCHIVE's mode.
                            force_mode=0o600 if component == "security" else None,
                            on_skip=_report_skip,
                        )
                    except FileExistsError as exc:
                        raise pinned_fs.PinnedPathRefusal(
                            f"refusing to restore {f!r}: something still occupies that "
                            "name in the data home after the backup pass, so it is a "
                            "hardlink alias or a name this restore could not move "
                            "aside. Writing to it could follow whatever it points at. "
                            "Remove it and re-run."
                        ) from exc
                    if copied and component == "security":
                        # Nothing to re-apply: force_mode above already set owner-only
                        # through the descriptor. On Windows the by-name branch still
                        # needs restrict_to_owner for its DACL, which is why that call
                        # survives there and not here.
                        pass
            finally:
                os.close(backup_fd)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def _lock_down_restored(path: Path, component: str) -> None:
    """Apply the owner-only lockdown a restored security file needs.

    restrict_to_owner (fail-loud), NOT chmod_safe (which swallows OSError): security
    files include sel_hmac.key. Mirrors the create path's deliberate fail-loud
    lockdown -- better to abort than silently land a restored secret group- or
    world-readable. POSIX applies chmod 0o600; Windows applies an owner-only DACL via
    icacls. The freshly copied file is unlinked on failure so the abort this promises
    actually removes the exposed artifact, instead of leaving the restored secret
    under the destination's inherited DACL after the OSError propagates.
    """
    if component != "security":
        return
    try:
        platform_compat.restrict_to_owner(str(path))
    except OSError:
        path.unlink(missing_ok=True)
        raise


def _do_replace(
    snap: Path, mc: Path, components: list[str] | None, *, allow_unpinned: bool = False
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = mc / f"pre-restore-{ts}"
    backup.mkdir(exist_ok=True)
    print("🔄 Replace mode — backing up current state...")

    for comp in ("memory", "crons", "config", "notifications", "security"):
        if _want(components, comp):
            _backup_and_copy(mc, backup, snap, comp, allow_unpinned=allow_unpinned)
            print(f"  ✅ {comp}")

    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            d = mc / dirname
            if d.is_dir():
                _copytree_safe(d, backup / dirname, allow_unpinned=allow_unpinned)
            sd = snap / dirname
            if sd.is_dir():
                if d.is_dir():
                    shutil.rmtree(str(d))
                _copytree_safe(sd, d, allow_unpinned=allow_unpinned)
        print("  ✅ workspace")

    if _want(components, "skills"):
        sk = mc / "skills"
        if sk.is_dir():
            _copytree_safe(sk, backup / "skills", allow_unpinned=allow_unpinned)
        snap_sk = snap / "skills"
        if snap_sk.is_dir():
            if sk.is_dir():
                shutil.rmtree(str(sk))
            _copytree_safe(snap_sk, sk, allow_unpinned=allow_unpinned)
        print("  ✅ skills")

    try:
        backup.rmdir()
    except OSError:
        print(f"  Previous state saved to: {backup}/")
    print("✅ Replace complete.")


def _do_merge(
    snap: Path, mc: Path, components: list[str] | None, *, allow_unpinned: bool = False
) -> None:
    # Asked once, at entry, BEFORE any mutation. The core-file copies below run before
    # any tree call, so gating inside the tree helpers meant a merge on a platform that
    # cannot pin wrote memory.db, crons.json and the security files first and only then
    # met the refusal -- either redirecting those writes through a planted link, or
    # aborting with the restore already half applied. Review caught it; it is the same
    # gate-placement defect as the snapshot side, one path over.
    _staging_is_pinned(allow_unpinned=allow_unpinned, what="merge restore")
    print("🔀 Merge mode — importing...")

    if _want(components, "memory") and (snap / "memory.db").is_file():
        if not (mc / "memory.db").is_file():
            shutil.copy2(str(snap / "memory.db"), str(mc / "memory.db"))
            if (snap / "memory_index.db").is_file():
                shutil.copy2(str(snap / "memory_index.db"), str(mc / "memory_index.db"))
            print("  Memory: copied (no existing memory.db)")
        else:
            _merge_memory(snap / "memory.db", mc / "memory.db")
        print("  ✅ memory")

    if _want(components, "crons"):
        sc, dc = snap / "crons.json", mc / "crons.json"
        if sc.is_file():
            if dc.is_file():
                _merge_crons(sc, dc)
            else:
                shutil.copy2(str(sc), str(dc))
                print("  Crons: copied (no existing crons)")
        print("  ✅ crons")

    if _want(components, "config"):
        for f in CORE_FILES["config"]:
            s, d = snap / f, mc / f
            if s.is_file() and not d.is_file():
                shutil.copy2(str(s), str(d))
                print(f"  {f}: restored (was missing)")
        print("  ✅ config")

    if _want(components, "notifications"):
        sn, dn = snap / "notifications.jsonl", mc / "notifications.jsonl"
        if sn.is_file():
            if dn.is_file():
                _merge_notifications(sn, dn)
            else:
                shutil.copy2(str(sn), str(dn))
                print("  Notifications: copied")
        print("  ✅ notifications")

    if _want(components, "security"):
        for f in CORE_FILES["security"]:
            s, d = snap / f, mc / f
            if s.is_file() and not d.is_file():
                shutil.copy2(str(s), str(d))
                # restrict_to_owner (fail-loud), NOT chmod_safe — security
                # files include sel_hmac.key; mirror the create path. Windows
                # applies an owner-only DACL via icacls. Unlink the freshly
                # copied file on
                # failure so an icacls error doesn't leave a restored secret
                # under the destination-inherited DACL.
                try:
                    platform_compat.restrict_to_owner(str(d))
                except OSError:
                    d.unlink(missing_ok=True)
                    raise
                print(f"  {f}: restored (was missing)")
        print("  ✅ security")

    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            sd = snap / dirname
            if sd.is_dir():
                dd = mc / dirname
                dd.mkdir(parents=True, exist_ok=True)
                _copy_tree_no_overwrite(sd, dd, allow_unpinned=allow_unpinned)
        print("  ✅ workspace")

    if _want(components, "skills"):
        if (snap / "skills").is_dir():
            (mc / "skills").mkdir(parents=True, exist_ok=True)
            _copy_tree_no_overwrite(snap / "skills", mc / "skills", allow_unpinned=allow_unpinned)
        print("  ✅ skills")

    print("✅ Merge complete.")


def _is_gateway_running() -> bool:
    """Check if the KiroCrew gateway is listening on its dashboard port."""
    # Deterministic override (used by tests / scripted restores) — avoids a real
    # socket probe whose result is environment-dependent.
    override = os.environ.get("KIROCREW_ASSUME_GATEWAY_RUNNING")
    if override is not None:
        return override.strip().lower() not in ("", "0", "false", "no")
    port = _DASHBOARD_PORT
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def restore_main(argv: list[str] | None = None, *, parsed: argparse.Namespace | None = None) -> int:
    if parsed is None:
        p = argparse.ArgumentParser(
            prog="kirocrew-restore", description="Restore KiroCrew state from a snapshot."
        )
        p.add_argument("snapshot", nargs="?")
        p.add_argument("--mode", choices=("replace", "merge"))
        p.add_argument("--dry-run", action="store_true")
        p.add_argument(
            "--force", action="store_true", help="Allow restore even if gateway is running"
        )
        p.add_argument("--components")
        p.add_argument("--list-components", action="store_true")
        p.add_argument(
            "--allow-unpinned-staging",
            action="store_true",
            dest="allow_unpinned",
            help=(
                "Restore by path name on a platform that cannot open a directory "
                "relative to a descriptor. Without this the restore is refused there "
                "rather than run with a destination an ancestor swap could redirect."
            ),
        )
        parsed = p.parse_args(argv)
    args = parsed
    allow_unpinned = bool(getattr(args, "allow_unpinned", False))

    if args.list_components:
        _list_components()
        return 0

    if not args.snapshot:
        print("❌ snapshot file is required (unless --list-components is given)")
        return 1

    force = getattr(args, "force", False)
    if not force and _is_gateway_running():
        _audit("state_restore_rejected", "reason=gateway_running")
        print("❌ Gateway is running. Stop it first (kirocrew stop) or use --force.")
        return 1

    snap_path = Path(args.snapshot)
    if not snap_path.is_file():
        print(f"❌ File not found: {snap_path}")
        return 1

    # Parse components
    components: list[str] | None = None
    if args.components:
        components = [c.strip() for c in args.components.split(",")]
        for c in components:
            if c not in VALID_COMPONENTS:
                print(f"❌ Unknown component: {c}\n")
                _list_components()
                return 1

    mc = _mc_dir()
    mode = args.mode or ("merge" if (mc / "memory.db").is_file() else "replace")

    with tempfile.TemporaryDirectory() as work_str:
        work = Path(work_str)

        # Security checks are enforced inside _data_filter (no TOCTOU gap)
        with tarfile.open(str(snap_path), "r:gz") as tar:
            try:
                tar.extractall(work, filter=_data_filter)
            except TypeError:
                # Python < 3.11.4: filter param not supported, apply manually
                members = [m for m in tar.getmembers() if _data_filter(m) is not None]
                tar.extractall(work, members=members)

        snap_dirs = [
            d for d in work.iterdir() if d.is_dir() and d.name.startswith("kirocrew-snapshot-")
        ]
        if not snap_dirs:
            print("❌ Invalid snapshot format")
            return 1
        snap = snap_dirs[0]

        _print_manifest(snap)
        if components:
            print(f"🔧 Components: {','.join(components)}")

        if args.dry_run:
            print(f"\n🔍 Dry run — would restore to {mc} in {mode} mode")
            print("Files in snapshot:")
            for f in sorted(snap.rglob("*")):
                if f.is_file():
                    print(f"  {f.relative_to(snap)}")
            return 0

        mc.mkdir(parents=True, exist_ok=True)
        # Contained here rather than allowed to propagate: a refusal is a decision
        # this command made on purpose, and a traceback would read like a crash and
        # bury the one sentence saying what to do about it.
        try:
            if mode == "replace":
                _do_replace(snap, mc, components, allow_unpinned=allow_unpinned)
            else:
                _do_merge(snap, mc, components, allow_unpinned=allow_unpinned)
        except pinned_fs.PinnedPathRefusal as exc:
            print(f"❌ {exc}")
            return 1

    # Integrity check
    if _want(components, "memory") and (mc / "memory.db").is_file():
        try:
            with sqlite3.connect(str(mc / "memory.db")) as conn:
                result = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        except Exception as e:
            result = str(e)
        if result == "ok":
            print("🔍 memory.db integrity: OK")
        else:
            print(f"⚠️  memory.db integrity check failed: {result}")
            _audit("state_restore_rejected", f"reason=integrity_check_failed from={snap_path.name}")
            return 1
        if not (mc / "memory_index.db").is_file():
            print(
                "⚠️  memory_index.db is missing — full-text search may not "
                "work until the FTS index is rebuilt."
            )

    comp_str = ",".join(components) if components else "all"
    _audit("state_restored", f"mode={mode} components={comp_str} from={snap_path.name}")

    print("\n⚠️  Restart kirocrew gateway to pick up changes: kirocrew restart")
    return 0
