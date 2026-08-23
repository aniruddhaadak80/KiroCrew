"""The dotenv grammar, and the per-workspace file layer built on it.

Two halves, because the parser and the layer fail in different ways:

* The grammar itself -- what counts as a line, how a value is unquoted, and the fact
  that every pair still goes through ``validate_pair``. A dotenv file must not become
  a way in for a value the API would refuse.
* The layer -- where the file lives, that a name which cannot be a filename gets no
  file rather than a guessed one, and that a malformed line costs its own line and
  not the whole workspace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.config import loader as loader_mod
from kiro_crew.config import variables_store as vstore
from kiro_crew.config.loader import (
    SCOPE_GLOBAL,
    SCOPE_WORKSPACE,
    SCOPE_WORKSPACE_FILE,
    VARIABLE_SCOPES,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    resolve_variables,
)
from kiro_crew.variables import MAX_VALUE_LEN, parse_dotenv, render_dotenv


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Redirect the config path so the store and its workspaces dir land in tmp_path.

    Redirects ``config_path`` and NOT the derived ``store_path``: the store's location
    is derived from the config directory, and patching the derived path would still
    pass if it were hardcoded somewhere else -- which is the one thing the derivation
    exists to prevent.
    """
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(loader_mod, "config_path", lambda: cfg_file)
    vstore.invalidate_cache()
    yield tmp_path
    vstore.invalidate_cache()


def _write_env(root: Path, workspace: str, text: str) -> Path:
    path = root / "variables" / "workspaces" / f"{workspace}.env"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestGrammar:
    def test_a_plain_pair(self):
        assert parse_dotenv("API_URL=https://x.test") == ({"API_URL": "https://x.test"}, [])

    def test_export_prefix_is_accepted(self):
        """Real .env files carry it, pasted from a shell profile."""
        assert parse_dotenv("export API_URL=https://x.test")[0] == {"API_URL": "https://x.test"}

    def test_blank_lines_and_comments_are_skipped(self):
        pairs, problems = parse_dotenv("# heading\n\n   \nA=1\n")
        assert pairs == {"A": "1"}
        assert problems == []

    def test_a_hash_only_starts_a_comment_at_the_start_of_a_line(self):
        """Mid-line it is an ordinary character. Treating it as a comment would
        silently truncate a URL fragment or a colour literal."""
        assert parse_dotenv("A=abc#123")[0] == {"A": "abc#123"}

    @pytest.mark.parametrize("quote", ["'", '"'])
    def test_one_matching_pair_of_surrounding_quotes_is_stripped(self, quote: str):
        assert parse_dotenv(f"A={quote}on call{quote}")[0] == {"A": "on call"}

    def test_an_interior_quote_is_literal(self):
        """Only a MATCHING surrounding pair is stripped, which is what lets
        `render_dotenv` skip escaping entirely."""
        assert parse_dotenv('A=say "hi"')[0] == {"A": 'say "hi"'}

    def test_mismatched_quotes_are_not_stripped(self):
        assert parse_dotenv("A='hi\"")[0] == {"A": "'hi\""}

    def test_whitespace_around_the_name_and_bare_value_is_trimmed(self):
        assert parse_dotenv("  A  =  spaced  ")[0] == {"A": "spaced"}

    def test_quotes_preserve_the_whitespace_trimming_would_remove(self):
        assert parse_dotenv('A="  padded  "')[0] == {"A": "  padded  "}

    def test_an_empty_value_is_legal(self):
        """An empty string is a deliberate override to empty, not a missing value --
        the same rule the cascade uses for key presence."""
        assert parse_dotenv("A=")[0] == {"A": ""}

    def test_an_escape_sequence_is_not_interpreted(self):
        r"""``\n`` stays two characters. Interpreting it would produce a newline,
        which ``validate_pair`` forbids -- turning a legal line into a rejected one."""
        assert parse_dotenv(r"A=one\ntwo")[0] == {"A": r"one\ntwo"}

    def test_a_value_containing_an_equals_sign_keeps_it(self):
        """The split is on the FIRST `=`; a base64 or query-string value has more."""
        assert parse_dotenv("A=a=b=c")[0] == {"A": "a=b=c"}


class TestProblemsAreReportedNotRaised:
    def test_a_line_without_an_equals_is_reported_with_its_number(self):
        pairs, problems = parse_dotenv("A=1\nnonsense\nB=2\n")
        assert pairs == {"A": "1", "B": "2"}, "the good lines still parse"
        assert problems == [(2, "expected NAME=value")]

    def test_a_duplicate_takes_the_last_value_and_is_still_reported(self):
        """Last-wins matches every other dotenv reader, but silently dropping one of
        two lines the operator wrote is the failure they would not notice."""
        pairs, problems = parse_dotenv("A=first\nA=second\n")
        assert pairs == {"A": "second"}
        assert len(problems) == 1 and problems[0][0] == 2
        assert "duplicate" in problems[0][1]

    def test_line_numbers_are_one_based_and_count_skipped_lines(self):
        _, problems = parse_dotenv("# c\n\nA=1\nbad\n")
        assert problems == [(4, "expected NAME=value")]


class TestTheFileObeysTheSameRulesAsThePanel:
    """A dotenv file must not be a way in for a value the API would refuse."""

    def test_a_name_that_is_not_an_identifier_is_refused(self):
        _, problems = parse_dotenv("1abc=x")
        assert problems and "name must start with a letter" in problems[0][1]

    def test_a_reserved_token_name_is_refused(self):
        _, problems = parse_dotenv("STOP_FILE=/tmp/x")
        assert problems and "reserved" in problems[0][1]

    def test_an_oversized_value_is_refused(self):
        _, problems = parse_dotenv("A=" + "x" * (MAX_VALUE_LEN + 1))
        assert problems and str(MAX_VALUE_LEN) in problems[0][1]

    def test_a_value_carrying_the_opening_delimiter_is_refused(self):
        """The rule that makes expansion idempotent across two boundaries, not merely
        single-pass within one."""
        _, problems = parse_dotenv("A={{other}}")
        assert problems and "{{" in problems[0][1]


class TestRoundTrip:
    def test_render_then_parse_returns_the_same_pairs(self):
        pairs = {"B": "two", "A": "one", "SPACED": "  pad  ", "EMPTY": "", "Q": 'say "hi"'}
        assert parse_dotenv(render_dotenv(pairs))[0] == pairs

    def test_render_sorts_by_name(self):
        assert render_dotenv({"B": "2", "A": "1"}) == "A=1\nB=2"

    def test_only_values_that_need_quoting_get_it(self):
        """A wall of unnecessary quotes is what makes a generated dotenv file look
        machine-owned and discourages hand-editing."""
        out = render_dotenv({"PLAIN": "value", "EMPTY": "", "PAD": " x "})
        assert "PLAIN=value" in out
        assert 'EMPTY=""' in out
        assert 'PAD=" x "' in out


class TestWorkspaceFilePath:
    def test_the_file_sits_under_the_fenced_store_directory(self, wired):
        """Inside `store_path().parent` so it inherits that directory's `security.py`
        fence -- the containment that is the whole reason these files are not in the
        workspace working directory the agent edits."""
        path = vstore.workspace_env_path("ops")
        assert path is not None
        assert path.parent.parent == vstore.store_path().parent
        assert path.name == "ops.env"

    @pytest.mark.parametrize(
        "name", ["../escape", "..", "", "a/b", "a\\b", ".hidden", "x" * 65, "wörk"]
    )
    def test_a_name_that_cannot_be_a_filename_gets_no_file(self, wired, name: str):
        """Refusing is safe -- the workspace still resolves from the JSON store.
        Guessing a sanitized name is not: two workspaces could sanitize onto one file
        and silently share their variables."""
        assert vstore.workspace_env_path(name) is None

    def test_a_refused_name_resolves_no_file_values(self, wired):
        assert vstore.workspace_env_values("../escape") == {}


class TestWorkspaceFileValues:
    def test_values_load_from_the_file(self, wired):
        _write_env(wired, "ops", "API_URL=https://ops.test\n")
        assert vstore.workspace_env_values("ops") == {"API_URL": "https://ops.test"}

    def test_a_missing_file_is_not_an_error(self, wired):
        assert vstore.workspace_env_values("ops") == {}

    def test_one_bad_line_costs_its_own_line_only(self, wired, caplog):
        """TOLERANT here, unlike the endpoint that shares the parser: nobody is
        watching, so a malformed line must not blank a whole workspace."""
        _write_env(wired, "ops", "GOOD=1\nnonsense\nALSO_GOOD=2\n")
        with caplog.at_level("WARNING"):
            values = vstore.workspace_env_values("ops")
        assert values == {"GOOD": "1", "ALSO_GOOD": "2"}
        assert "line 2" in caplog.text

    def test_an_unreadable_file_yields_no_values_rather_than_raising(self, wired):
        path = _write_env(wired, "ops", "A=1\n")
        path.write_bytes(b"\xff\xfe\x00invalid utf-8 \xc3\x28")
        assert vstore.workspace_env_values("ops") == {}


class TestTheFileLayerInTheCascade:
    """Ranked between global and the panel's workspace scope."""

    def _config(self) -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        cfg.workspaces = {"ops": WorkspaceConfig(dir="w-ops")}
        cfg.default_workspace = "ops"
        cfg.agents = {"crew1": KiroCrewAgentConfig(workspace="ops")}
        cfg.default_agent = "crew1"
        return cfg

    def _seed_store(self, root: Path, doc: dict) -> None:
        path = root / "variables" / "variables.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
        vstore.invalidate_cache()

    def test_the_scope_order_places_the_file_below_the_panel(self):
        assert VARIABLE_SCOPES.index(SCOPE_GLOBAL) < VARIABLE_SCOPES.index(SCOPE_WORKSPACE_FILE)
        assert VARIABLE_SCOPES.index(SCOPE_WORKSPACE_FILE) < VARIABLE_SCOPES.index(SCOPE_WORKSPACE)

    def test_the_file_overrides_global(self, wired):
        self._seed_store(wired, {"global": {"a": "from-global"}})
        _write_env(wired, "ops", "a=from-file\n")
        resolution = resolve_variables(self._config())
        assert resolution.values["a"] == "from-file"
        assert resolution.winning_scope["a"] == SCOPE_WORKSPACE_FILE
        assert SCOPE_GLOBAL in resolution.shadowed["a"]

    def test_the_panel_overrides_the_file(self, wired):
        """An edit made in the UI must take effect: a panel that silently loses to a
        file on disk is a panel that lies."""
        self._seed_store(wired, {"workspaces": {"ops": {"a": "from-panel"}}})
        _write_env(wired, "ops", "a=from-file\n")
        resolution = resolve_variables(self._config())
        assert resolution.values["a"] == "from-panel"
        assert resolution.winning_scope["a"] == SCOPE_WORKSPACE
        assert SCOPE_WORKSPACE_FILE in resolution.shadowed["a"]

    def test_a_file_only_key_still_resolves(self, wired):
        _write_env(wired, "ops", "only=from-file\n")
        assert resolve_variables(self._config()).values["only"] == "from-file"

    def test_a_workspace_with_no_config_entry_still_gets_its_file(self, wired):
        """Keyed on the NAME, not on the config object: a workspace can carry a file
        before it has a config entry, and dropping the layer then would make the file
        silently inert for exactly the operator who just created it."""
        _write_env(wired, "ops", "a=from-file\n")
        cfg = self._config()
        cfg.workspaces = {}
        assert resolve_variables(cfg).values.get("a") == "from-file"

    def test_the_file_of_another_workspace_is_not_consulted(self, wired):
        _write_env(wired, "other", "leaked=yes\n")
        assert "leaked" not in resolve_variables(self._config()).values
