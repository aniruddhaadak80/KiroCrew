"""The descriptor-pinned staging primitive, and snapshot/restore's use of it.

Written as an A/B suite rather than a list of assertions. The point of this change is
that a pinned traversal is strictly stronger than a by-name one, and the only honest
way to show that is to run the SAME attack against both paths and watch one leak while
the other refuses -- an exception-only assertion cannot tell "pinned" from "got lucky".

The mid-walk swap is performed inside the ``ignore`` callback, which both paths invoke
once per directory with that directory's contents. That is the check-to-use window
made deterministic: the screen has just looked at the entry and declared it fine, and
the copy of that entry has not happened yet.
"""

from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path

import pytest

from kiro_crew import pinned_fs, snapshot

pinned_only = pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="requires O_DIRECTORY, O_NOFOLLOW, dir_fd and fd-listdir support (POSIX)",
)


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("creating a symlink needs privilege on this host")


def _tree_with_victim(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A source tree to stage, plus a victim directory nothing should reach.

    Returns ``(src_root, mid_dir, victim_dir)``. ``mid_dir`` is the ancestor that gets
    swapped: it is a real directory when the screen looks at it and a link to
    ``victim_dir`` by the time the copy of its contents happens.
    """
    src = tmp_path / "source"
    mid = src / "mid"
    mid.mkdir(parents=True)
    (mid / "ordinary.txt").write_text("ordinary\n", encoding="utf-8")

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "stolen.txt").write_text("CREDENTIAL\n", encoding="utf-8")
    return src, mid, victim


def _swap_on_screen(mid: Path, victim: Path):
    """An ``ignore`` callback that swaps *mid* for a link to *victim*, once.

    Fires when the walk screens the directory that CONTAINS *mid*, i.e. after that
    directory has been listed and before its entries are copied.
    """
    state = {"done": False}

    def _ignore(directory: str, contents: list[str]) -> set[str]:
        if not state["done"] and mid.name in contents:
            state["done"] = True
            mid.rename(mid.parent / "mid-moved")
            _symlink_or_skip(mid, victim)
        return set()

    return _ignore


# ── The differential: pinned refuses where by-name follows ────────────────────


@pinned_only
def test_an_ancestor_swapped_after_the_screen_leaks_by_name_and_not_when_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole reason this module exists, asserted on WHERE THE BYTES LANDED.

    Run twice against identical trees. The by-name traversal is expected to copy the
    victim's contents, because ``shutil.copytree`` captures its entries with one
    ``scandir`` and then descends into ``mid`` BY NAME -- the cached ``DirEntry`` still
    says "directory", and a fresh listing of that name now lands in the victim. The
    pinned traversal re-stats through the descriptor it holds and skips it.

    The control half has to force the by-name branch by patching the platform probe.
    ``allow_unpinned=True`` alone does not: it is a permission for a platform that
    cannot pin, not a switch that turns pinning off where it works -- which is itself
    worth pinning, since a flag that silently downgraded a capable platform would be a
    far worse bug than the one this change fixes.

    If the by-name half ever stops leaking, this test has stopped testing anything and
    the assertion below says so rather than passing quietly.
    """
    src_a, mid_a, victim_a = _tree_with_victim(tmp_path / "by_name")
    dst_a = tmp_path / "staged_by_name"
    with monkeypatch.context() as no_pinning:
        no_pinning.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
        snapshot._copytree_safe(
            src_a, dst_a, allow_unpinned=True, ignore=_swap_on_screen(mid_a, victim_a)
        )
    leaked = (dst_a / "mid" / "stolen.txt").exists()

    src_b, mid_b, victim_b = _tree_with_victim(tmp_path / "pinned")
    dst_b = tmp_path / "staged_pinned"
    snapshot._copytree_safe(src_b, dst_b, ignore=_swap_on_screen(mid_b, victim_b))

    assert leaked, (
        "the by-name control did not leak, so this test no longer demonstrates the "
        "difference the pinned walk exists to make -- fix the control, do not delete "
        "the assertion"
    )
    assert not (
        dst_b / "mid" / "stolen.txt"
    ).exists(), "the pinned walk followed an ancestor swapped after the screen approved it"
    assert "Skipping symlink in source tree" in capsys.readouterr().out


def test_the_opt_in_flag_does_not_turn_pinning_off_where_it_works(tmp_path: Path) -> None:
    """``--allow-unpinned-staging`` permits a fallback; it never requests one.

    Stated as its own test because the distinction is load-bearing: if the flag were a
    mode switch, anyone passing it once (to get past an unrelated failure, or in a
    script copied from a Windows runbook) would silently give up the pinning on every
    platform.
    """
    src, mid, victim = _tree_with_victim(tmp_path)
    dst = tmp_path / "staged"
    snapshot._copytree_safe(src, dst, allow_unpinned=True, ignore=_swap_on_screen(mid, victim))
    assert not (dst / "mid" / "stolen.txt").exists()


@pinned_only
def test_open_dir_pinned_refuses_a_parent_that_became_a_link(tmp_path: Path) -> None:
    """The root's OWN ancestor chain is pinned, which the preserved branches did not do.

    ``os.open(root, O_DIRECTORY | O_NOFOLLOW)`` refuses a link AT the root's name but
    walks every ancestor by name to get there. Here the parent is captured before the
    swap -- the state a caller is in when it loses the resolve-to-open race -- and the
    pinned chain has to refuse it.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "inside").mkdir()

    parent = tmp_path / "holder"
    parent.mkdir()
    (parent / "inside").mkdir()
    resolved_parent = os.path.realpath(parent)

    parent.rename(tmp_path / "holder-moved")
    _symlink_or_skip(parent, victim)

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        pinned_fs.open_in_pinned_parent(
            resolved_parent,
            "inside",
            flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            mode=0o700,
            what="staging root",
        )
    assert "became a symbolic link" in str(excinfo.value)


# ── Hardlink aliases ─────────────────────────────────────────────────────────


def test_a_hardlinked_source_is_refused_rather_than_dereferenced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``shutil.copy2`` would have shipped a credential alias as ordinary bytes.

    A hardlink shares its target's inode, so ``realpath`` yields the alias's own name,
    ``is_symlink()`` is False, and ``O_NOFOLLOW`` has no link to refuse. The check has
    to happen on the open descriptor, which is what this pins.
    """
    secret = tmp_path / "credential"
    secret.write_text("AKIA-not-real\n", encoding="utf-8")
    alias = tmp_path / "config.json"
    try:
        os.link(secret, alias)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("hard links are unavailable on this host")

    dst = tmp_path / "staged.json"
    copied = pinned_fs.copy_file_pinned(str(alias), str(dst), on_skip=snapshot._report_skip)

    assert copied is False
    assert not dst.exists()
    assert "hardlinked or non-regular" in capsys.readouterr().out


def test_a_single_link_regular_file_still_copies_with_mode_and_mtime(tmp_path: Path) -> None:
    """The refusal above must not be a blanket one: the ordinary case still works."""
    src = tmp_path / "plain.txt"
    src.write_text("content\n", encoding="utf-8")
    os.chmod(src, 0o640)
    dst = tmp_path / "copied.txt"

    assert pinned_fs.copy_file_pinned(str(src), str(dst)) is True
    assert dst.read_text(encoding="utf-8") == "content\n"
    assert (dst.stat().st_mode & 0o777) == 0o640
    assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns


# ── The restore side ─────────────────────────────────────────────────────────


@pinned_only
def test_restore_moves_a_symlinked_core_file_aside_instead_of_writing_through_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#3797's third finding, plus the silent partial restore hiding behind it.

    Before this change the name-based screen declined to MOVE a symlinked core file to
    the backup and then ``shutil.copy2`` wrote through the very link it had just
    declined to move. Skipping the whole entry instead would have been no better: the
    archive's version of that file would silently never be restored, and the command
    would still report success.

    Both are asserted here -- the victim keeps its bytes, AND the snapshot's version
    actually lands.
    """
    snap = tmp_path / "payload"
    snap.mkdir()
    (snap / "crons.json").write_text("[]\n", encoding="utf-8")

    victim = tmp_path / "victim.json"
    victim.write_text("PRECIOUS\n", encoding="utf-8")

    mc = tmp_path / "home"
    mc.mkdir()
    _symlink_or_skip(mc / "crons.json", victim)

    backup = mc / "backup"
    backup.mkdir()

    snapshot._backup_and_copy(mc, backup, snap, "crons")

    assert (
        victim.read_text(encoding="utf-8") == "PRECIOUS\n"
    ), "the restore wrote through the symlink it was supposed to move aside"
    assert (mc / "crons.json").read_text(
        encoding="utf-8"
    ) == "[]\n", "the archive's version was not restored -- a silent partial restore"
    assert not (mc / "crons.json").is_symlink()
    assert (backup / "crons.json").is_symlink(), "the link itself belongs in the backup"
    assert "Moving symlinked core file aside" in capsys.readouterr().out


@pinned_only
def test_restore_refuses_when_the_name_is_still_occupied_after_the_backup_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop for the case the move above cannot resolve.

    The move is neutralised rather than made to fail, because the invariant being
    pinned is about the STATE the copy runs in ("this name is still taken"), not about
    any particular reason the move did not happen. Exclusive creation is what turns
    that state into a refusal instead of a write into whatever now sits there -- and
    the live bytes have to survive it.
    """
    snap = tmp_path / "payload"
    snap.mkdir()
    (snap / "crons.json").write_text("[]\n", encoding="utf-8")

    mc = tmp_path / "home"
    mc.mkdir()
    (mc / "crons.json").write_text("occupied\n", encoding="utf-8")
    backup = mc / "backup"
    backup.mkdir()

    monkeypatch.setattr(snapshot.os, "rename", lambda *a, **k: None)

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        snapshot._backup_and_copy(mc, backup, snap, "crons")

    assert "still occupies that name" in str(excinfo.value)
    assert (mc / "crons.json").read_text(
        encoding="utf-8"
    ) == "occupied\n", "the live file was overwritten even though it was never moved aside"


@pinned_only
def test_merge_does_not_overwrite_and_skips_a_symlinked_source(tmp_path: Path) -> None:
    """The no-overwrite promise is now exclusive creation rather than a prior exists()."""
    src = tmp_path / "from_snapshot"
    (src / "nested").mkdir(parents=True)
    (src / "nested" / "new.txt").write_text("new\n", encoding="utf-8")
    (src / "nested" / "kept.txt").write_text("from snapshot\n", encoding="utf-8")
    _symlink_or_skip(src / "nested" / "link.txt", tmp_path / "anything")

    dst = tmp_path / "live"
    (dst / "nested").mkdir(parents=True)
    (dst / "nested" / "kept.txt").write_text("LOCAL WINS\n", encoding="utf-8")

    snapshot._copy_tree_no_overwrite(src, dst)

    assert (dst / "nested" / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert (dst / "nested" / "kept.txt").read_text(encoding="utf-8") == "LOCAL WINS\n"
    assert not (dst / "nested" / "link.txt").exists()


# ── Platforms that cannot pin ────────────────────────────────────────────────


def test_a_platform_that_cannot_pin_refuses_until_the_operator_says_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse rather than fall back by name -- and name the flag that permits it.

    Patched on ``pinned_fs`` because that is where the capability question lives; the
    snapshot module reads it through the module rather than binding it at import.
    """
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
    src = tmp_path / "source"
    src.mkdir()
    (src / "file.txt").write_text("x\n", encoding="utf-8")

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        snapshot._copytree_safe(src, tmp_path / "refused")
    assert "--allow-unpinned-staging" in str(excinfo.value)
    assert not (tmp_path / "refused").exists()

    snapshot._copytree_safe(src, tmp_path / "permitted", allow_unpinned=True)
    assert (tmp_path / "permitted" / "file.txt").read_text(encoding="utf-8") == "x\n"


def test_the_manifest_records_which_traversal_built_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader deciding whether to trust an archive needs the mode on the record."""
    mc = tmp_path / "home"
    (mc / "workspace").mkdir(parents=True)
    (mc / "workspace" / "note.md").write_text("hi\n", encoding="utf-8")
    out = tmp_path / "snapshots"

    pinned = snapshot._build_snapshot(mc, out, "pinned-archive")
    with tarfile.open(str(pinned)) as tar:
        member = tar.extractfile("pinned-archive/MANIFEST.json")
        assert member is not None
        assert json.load(member)["staging"] == "pinned"

    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
    unpinned = snapshot._build_snapshot(mc, out, "unpinned-archive", allow_unpinned=True)
    with tarfile.open(str(unpinned)) as tar:
        member = tar.extractfile("unpinned-archive/MANIFEST.json")
        assert member is not None
        assert json.load(member)["staging"] == "unpinned"


def test_the_snapshot_cli_reports_a_refusal_instead_of_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deliberate refusal must read as a decision, with a non-zero exit code."""
    mc = tmp_path / "home"
    (mc / "workspace").mkdir(parents=True)
    (mc / "workspace" / "note.md").write_text("hi\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(mc))
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)

    rc = snapshot.snapshot_main([str(tmp_path / "out")])

    assert rc == 1
    assert "--allow-unpinned-staging" in capsys.readouterr().out
    assert not list((tmp_path / "out").glob("*.tar.gz"))


# ── The destination side is pinned too ───────────────────────────────────────


@pinned_only
def test_the_destination_root_cannot_be_repointed_once_the_walk_holds_it(
    tmp_path: Path,
) -> None:
    """Asserted on where the bytes landed, because an exception cannot prove pinning.

    The first revision of `stage_tree_pinned` pinned only the SOURCE. That was
    defensible while the only destination was a private temporary directory, and wrong
    the moment a restore used it: then the destination IS the live data home, and an
    ancestor swapped there lands the archive's bytes outside it. Review caught it.

    Here the destination root is renamed away and a link to a victim directory is put
    at its old name, from inside the `ignore` callback -- i.e. after the walk has
    opened the destination and before it writes. A pinned destination keeps writing
    into the directory it opened; a by-name one would follow the link.
    """
    src = tmp_path / "source"
    src.mkdir()
    (src / "payload.txt").write_text("REAL\n", encoding="utf-8")

    victim = tmp_path / "victim"
    victim.mkdir()

    dst = tmp_path / "live"
    state = {"done": False}

    def _swap_destination(directory: str, contents: list[str]) -> set[str]:
        if not state["done"]:
            state["done"] = True
            dst.rename(tmp_path / "live-moved")
            _symlink_or_skip(dst, victim)
        return set()

    pinned_fs.stage_tree_pinned(src, dst, what="tree", ignore=_swap_destination)

    assert (tmp_path / "live-moved" / "payload.txt").read_text(
        encoding="utf-8"
    ) == "REAL\n", "the write did not land in the directory the walk had already opened"
    assert not (
        victim / "payload.txt"
    ).exists(), "the write followed the destination link planted after the walk opened it"


# ── The gate runs before anything is staged ──────────────────────────────────


def test_core_files_alone_still_consult_the_platform_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data home with core files and no trees must not stage unpinned unasked.

    The first revision gated inside `_copytree_safe` only, so the opt-in was consulted
    exactly when a tree happened to exist. A data home holding `crons.json` and no
    `workspace/` staged its core files on a platform that cannot pin without ever
    asking -- the gate was reachable only through a path that might not run. Raised in
    review; the gate now runs once, up front, which is also what makes the manifest's
    `staging` value true of the whole archive.
    """
    mc = tmp_path / "home"
    mc.mkdir()
    (mc / "crons.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        snapshot._build_snapshot(mc, tmp_path / "out", "archive")
    assert "--allow-unpinned-staging" in str(excinfo.value)
    assert not list((tmp_path / "out").glob("*.tar.gz"))


# ── An incomplete archive says so ────────────────────────────────────────────


def test_the_manifest_records_what_was_omitted(tmp_path: Path) -> None:
    """A skipped file used to be a console warning and nothing else.

    That is the same "silent partial" shape this PR fixes on the restore side: exit 0,
    a success message, and an archive quietly missing a file. Raised in review. The
    omission is now in `MANIFEST.json`, and `_print_manifest` shows it so the record
    has a reader that is not "untar the archive by hand".
    """
    mc = tmp_path / "home"
    (mc / "workspace").mkdir(parents=True)
    (mc / "workspace" / "kept.txt").write_text("kept\n", encoding="utf-8")

    secret = tmp_path / "credential"
    secret.write_text("AKIA-not-real\n", encoding="utf-8")
    try:
        os.link(secret, mc / "workspace" / "alias.json")
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("hard links are unavailable on this host")

    archive = snapshot._build_snapshot(mc, tmp_path / "out", "archive")
    with tarfile.open(str(archive)) as tar:
        member = tar.extractfile("archive/MANIFEST.json")
        assert member is not None
        manifest = json.load(member)
        assert tar.getnames().count("archive/workspace/kept.txt") == 1
        assert "archive/workspace/alias.json" not in tar.getnames()

    omitted = {e["path"]: e["reason"] for e in manifest["skipped"]}
    assert omitted == {os.path.join("workspace", "alias.json"): pinned_fs.SKIP_NOT_REGULAR}


# ── The opt-in has to be reachable from the shipped command ──────────────────


def test_the_shipped_cli_accepts_the_opt_in_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal names a flag, so the flag must exist on the real parser.

    Review found it defined only on the fallback parsers INSIDE `snapshot_main` /
    `restore_main`, which the console script never reaches -- it builds its own
    subparsers in `cli.py` and passes `parsed=`. So a user on a platform that cannot
    pin was refused with a message naming a flag argparse would then reject: a dead
    end, and exactly the "withdraws the command from the platform" outcome the design
    note said it was avoiding.

    Asserted against the real `cli.main` with a control, because a test that only
    checked the flag parses could pass on a parser nobody runs.
    """
    from kiro_crew import cli

    def _run(argv: list[str]) -> object:
        monkeypatch.setattr("sys.argv", ["kirocrew"] + argv)
        try:
            cli.main()
            return "accepted"
        except SystemExit as exc:
            return exc.code

    assert _run(["snapshot", "--list", "--allow-unpinned-staging"]) == "accepted"
    assert _run(["restore", "--list-components", "--allow-unpinned-staging"]) == "accepted"
    assert (
        _run(["snapshot", "--list", "--no-such-flag"]) == 2
    ), "the control passed, so this test would accept a parser that ignores unknown flags"


def test_the_import_path_lets_the_refusal_propagate() -> None:
    """`apply_import_zip` must NOT swallow the refusal into a returned summary.

    The first revision caught it and returned the summary so the caller would not see a
    crash. Review pointed out that is worse than the crash: a returned summary reads as
    success, so the dashboard rendered "Import complete" over a data home nothing had
    been written to -- a false success in place of a loud failure. The refusal now
    propagates to the existing error path.

    Pinned on the source because the alternative -- a swallowed refusal reaching a UI as
    a success message -- is invisible until a user on a platform that cannot pin tries
    it, and by then they believe their import worked.
    """
    import inspect

    from kiro_crew import portability

    source = inspect.getsource(portability.apply_import_zip)
    assert (
        "except PinnedPathRefusal" not in source
    ), "the refusal is being swallowed again; a returned summary reads as success"
    assert "rejected_replace" not in source
    assert "_do_replace(snap, mc, None)" in source


# ── A failed copy leaves nothing behind ──────────────────────────────────────


def test_a_failed_copy_removes_the_partial_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the fragment survives the fix and the retry skips the real file.

    Raised in review, and the consequence is the sharp part: on the merge path
    `skip_existing` treats any existing name as "already there", so a destination left
    half-written by ENOSPC is never replaced -- the retry that should heal it skips it
    instead, and the corrupt file is permanent.
    """
    src = tmp_path / "source.txt"
    src.write_text("payload\n", encoding="utf-8")
    dst = tmp_path / "dest.txt"

    def _boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pinned_fs.shutil, "copyfileobj", _boom)

    with pytest.raises(OSError):
        pinned_fs.copy_file_pinned(str(src), str(dst))

    assert not dst.exists(), (
        "a partial destination survived the failure, so a merge retry would skip the "
        "archive's real file and keep the fragment forever"
    )


def test_mode_and_mtime_are_applied_through_the_written_descriptor(tmp_path: Path) -> None:
    """`chmod(name, dir_fd=...)` re-resolves the name; `fchmod(fd)` cannot be redirected.

    The metadata calls used to address the destination by name under the pinned
    directory, which leaves a window where the final component is swapped between the
    write and the chmod and the mode lands on the replacement. Asserted here as the
    ordinary-case behaviour the descriptor form has to preserve.
    """
    src = tmp_path / "source.txt"
    src.write_text("payload\n", encoding="utf-8")
    os.chmod(src, 0o640)
    dst = tmp_path / "dest.txt"

    assert pinned_fs.copy_file_pinned(str(src), str(dst)) is True
    assert (dst.stat().st_mode & 0o777) == 0o640
    assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns


@pinned_only
def test_a_planted_name_in_a_fresh_destination_tree_is_refused_not_skipped(
    tmp_path: Path,
) -> None:
    """Suppressing `FileExistsError` on mkdir turned a planted link into a silent gap.

    In a destination tree this operation created, a name already occupying a
    subdirectory is a link or a file planted there. The suppressed error let the pinned
    open below refuse the subtree and the restore then reported success with the
    archive's subtree missing -- the same silent-partial shape fixed elsewhere in this
    change. A merge legitimately meets existing directories, so `skip_existing` still
    tolerates them.
    """
    src = tmp_path / "source"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "file.txt").write_text("payload\n", encoding="utf-8")

    dst = tmp_path / "dest"
    dst.mkdir()
    _symlink_or_skip(dst / "sub", tmp_path / "elsewhere")

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        pinned_fs.stage_tree_pinned(src, dst, what="tree")
    assert "already occupies" in str(excinfo.value)

    # The merge path must still tolerate a real existing directory.
    dst2 = tmp_path / "dest2"
    (dst2 / "sub").mkdir(parents=True)
    pinned_fs.stage_tree_pinned(src, dst2, what="tree", skip_existing=True)
    assert (dst2 / "sub" / "file.txt").read_text(encoding="utf-8") == "payload\n"


@pinned_only
def test_the_destination_root_is_created_through_a_pinned_parent(tmp_path: Path) -> None:
    """Only the final component is created, and only relative to a pinned parent.

    `Path(dst).mkdir(parents=True)` created every missing component BY NAME. Two
    separate properties come out of replacing it, and it is worth being exact about
    which one this buys, because my first version of this test claimed the stronger one
    and passed for the wrong reason:

    1. A missing ancestor is no longer silently materialised through whatever the path
       resolves to. The parent must already exist, so a caller cannot accidentally
       write a tree into a linked directory it never validated. Asserted below.
    2. An ancestor swapped for a link AFTER the parent was resolved is refused, by
       `pin_parent`'s per-component `O_NOFOLLOW`. That is covered by
       `test_open_dir_pinned_refuses_a_parent_that_became_a_link`.

    What this does NOT buy is refusing an ancestor that was ALREADY a link when the
    parent was resolved: `realpath` follows it, deliberately, because refusing every
    symlinked ancestor would break a destination under `/tmp` on macOS. That residual is
    documented on `pin_parent` and asserted at the end of this test so the limit is on
    the record rather than assumed away.
    """
    src = tmp_path / "source"
    src.mkdir()
    (src / "file.txt").write_text("payload\n", encoding="utf-8")

    # (1) A missing parent is not created by name.
    with pytest.raises((pinned_fs.PinnedPathRefusal, OSError)):
        pinned_fs.stage_tree_pinned(src, tmp_path / "absent" / "deep" / "dest", what="tree")
    assert not (tmp_path / "absent").exists(), "a missing ancestor chain was materialised by name"

    # The documented residual, stated rather than implied: a parent that is already a
    # link is followed by the resolution, so the tree lands in its target.
    victim = tmp_path / "victim"
    victim.mkdir()
    holder = tmp_path / "holder"
    holder.mkdir()
    _symlink_or_skip(holder / "linked", victim)

    pinned_fs.stage_tree_pinned(src, holder / "linked" / "dest", what="tree")
    assert (victim / "dest" / "file.txt").read_text(encoding="utf-8") == "payload\n", (
        "if this now refuses, the pre-existing-link residual has been closed and this "
        "assertion should become the refusal it documents"
    )


# ── Round 3: the alias hazard reached the databases too ──────────────────────


def test_a_hardlinked_database_is_refused_before_sqlite_reads_it(tmp_path: Path) -> None:
    """The `.db` path had been left out of the alias screen, and SQLite is faithful.

    `sqlite3` cannot open a descriptor, so the connection has to name a path -- but the
    inode can still be judged first. Review found the consequence: aliasing `memory.db`
    onto a credential-bearing database elsewhere would have had SQLite dutifully back up
    its rows into the archive, with no symlink for any path test to notice, because a
    hardlink shares its target's inode.
    """
    import sqlite3 as _sqlite

    mc = tmp_path / "home"
    mc.mkdir()
    secrets = tmp_path / "credential-store.sqlite3"
    conn = _sqlite.connect(str(secrets))
    conn.execute("CREATE TABLE tokens (v TEXT)")
    conn.execute("INSERT INTO tokens VALUES ('super-secret-token')")
    conn.commit()
    conn.close()

    try:
        os.link(secrets, mc / "memory.db")
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("hard links are unavailable on this host")

    archive = snapshot._build_snapshot(mc, tmp_path / "out", "archive")
    with tarfile.open(str(archive)) as tar:
        names = tar.getnames()
        member = tar.extractfile("archive/MANIFEST.json")
        assert member is not None
        manifest = json.load(member)

    assert (
        "archive/memory.db" not in names
    ), "the aliased database was copied into the archive, tokens and all"
    assert any(e["path"] == "memory.db" for e in manifest["skipped"])


def test_a_symlinked_database_is_still_refused_and_recorded(tmp_path: Path) -> None:
    """The symlink half of the same screen, kept because the connection would follow it."""
    mc = tmp_path / "home"
    mc.mkdir()
    elsewhere = tmp_path / "elsewhere.db"
    elsewhere.write_bytes(b"SQLite format 3\x00")
    _symlink_or_skip(mc / "memory.db", elsewhere)

    archive = snapshot._build_snapshot(mc, tmp_path / "out", "archive")
    with tarfile.open(str(archive)) as tar:
        assert "archive/memory.db" not in tar.getnames()
        member = tar.extractfile("archive/MANIFEST.json")
        assert member is not None
        assert any(e["path"] == "memory.db" for e in json.load(member)["skipped"])


def test_merge_consults_the_gate_before_writing_any_core_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core copies run before any tree call, so the gate has to be at entry.

    Gating inside the tree helpers meant a merge on a platform that cannot pin wrote
    `memory.db`, `crons.json` and the security files FIRST and only then met the refusal
    -- either redirecting those writes through a planted link, or aborting with the
    restore already half applied. Raised in review; same gate-placement defect as the
    snapshot side, one path over.
    """
    snap = tmp_path / "payload"
    snap.mkdir()
    (snap / "crons.json").write_text("[]\n", encoding="utf-8")
    mc = tmp_path / "home"
    mc.mkdir()
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)

    with pytest.raises(pinned_fs.PinnedPathRefusal):
        snapshot._do_merge(snap, mc, None)

    assert not (
        mc / "crons.json"
    ).exists(), "a core file was written before the platform gate was consulted"


@pinned_only
def test_a_linked_destination_root_raises_the_refusal_not_a_raw_oserror(
    tmp_path: Path,
) -> None:
    """`O_NOFOLLOW` already refused it; the gap was that it escaped as a traceback.

    Every other refusal on this surface is one type the CLI boundary contains, and this
    one arrived as a bare `ELOOP`/`ENOTDIR`, so a restore ended in a stack trace instead
    of the sentence explaining what to remove. Found by my own probe of a review finding,
    then named by the reviewer.
    """
    src = tmp_path / "source"
    src.mkdir()
    (src / "file.txt").write_text("payload\n", encoding="utf-8")

    victim = tmp_path / "victim"
    victim.mkdir()
    holder = tmp_path / "holder"
    holder.mkdir()
    _symlink_or_skip(holder / "dest", victim)

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        pinned_fs.stage_tree_pinned(src, holder / "dest", what="tree")
    assert "symbolic link or not a directory" in str(excinfo.value)


def test_metadata_falls_back_to_a_path_where_fd_operations_do_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.fchmod` does not exist on Windows, and calling it unconditionally crashed.

    An earlier revision applied metadata only by descriptor, so a Windows snapshot using
    the declared by-name traversal raised `AttributeError` the moment it reached a core
    file -- a crash introduced by a hardening change, which is the worst kind. Simulated
    by deleting the attribute, the way the bench suite simulates a missing `O_NOFOLLOW`.
    """
    monkeypatch.delattr(pinned_fs.os, "fchmod", raising=False)
    monkeypatch.setattr(pinned_fs.os, "supports_fd", set())

    src = tmp_path / "source.txt"
    src.write_text("payload\n", encoding="utf-8")
    os.chmod(src, 0o640)
    dst = tmp_path / "dest.txt"

    assert pinned_fs.copy_file_pinned(str(src), str(dst)) is True
    assert dst.read_text(encoding="utf-8") == "payload\n"
    assert (dst.stat().st_mode & 0o777) == 0o640


def test_a_restored_security_file_is_locked_down_regardless_of_the_archive_mode(
    tmp_path: Path,
) -> None:
    """The archive is untrusted input, so its recorded mode cannot decide the result.

    The reviewer's suggested fix for the by-name lockdown was to drop it because "the
    copy already applies mode through the descriptor" -- but that applies the ARCHIVE's
    mode, and a hand-built tarball can record `0o777` on `telemetry_salt`. So the mode is
    forced through the descriptor instead of inherited or re-applied by name.
    """
    snap = tmp_path / "payload"
    snap.mkdir()
    salt = snap / "telemetry_salt"
    salt.write_text("salt\n", encoding="utf-8")
    os.chmod(salt, 0o777)

    mc = tmp_path / "home"
    mc.mkdir()
    backup = mc / "backup"
    backup.mkdir()

    snapshot._backup_and_copy(mc, backup, snap, "security")

    restored = mc / "telemetry_salt"
    assert restored.read_text(encoding="utf-8") == "salt\n"
    assert (
        restored.stat().st_mode & 0o777
    ) == 0o600, "the archive's permissive mode was inherited onto a restored secret"


# ── The helper's own contract ────────────────────────────────────────────────


def test_an_unknown_copytree_keyword_is_rejected_rather_than_dropped(tmp_path: Path) -> None:
    """``dirs_exist_ok`` is absorbed on purpose; anything else is a caller bug.

    Silently swallowing ``**kwargs`` is how a staging call would keep compiling after
    the flag it depends on stopped being honoured.
    """
    src = tmp_path / "source"
    src.mkdir()
    with pytest.raises(TypeError, match="unexpected keyword"):
        snapshot._copytree_safe(src, tmp_path / "dst", symlinks=True)


def test_the_tree_walk_capability_probe_names_a_function_that_supports_dir_fd() -> None:
    """Pins the bug that made every POSIX snapshot refuse before it was caught.

    ``os.lstat`` is NOT a member of ``os.supports_dir_fd`` even where the pinned walk
    works perfectly -- the capability belongs to ``os.stat``. Probing the wrong one
    reports False on a fully capable platform, which turns the deliberate refusal into
    a blanket outage. Asserted as a property of the stdlib so the probe cannot drift
    back.
    """
    assert os.stat in os.supports_dir_fd
    assert os.lstat not in os.supports_dir_fd
    if pinned_fs.supports_pinned_walk():
        assert pinned_fs.supports_pinned_tree_walk() is True
