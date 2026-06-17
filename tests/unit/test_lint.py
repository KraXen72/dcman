from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from python_on_whales.exceptions import DockerException

from dcman import lint
from dcman.errors import CmdError
from tests.helpers import make_workspace


@pytest.mark.unit
def test_sanitize_leaves_valid_names_unchanged() -> None:
	assert lint.sanitize_container_name("my-project") == "my-project"


@pytest.mark.unit
def test_sanitize_replaces_spaces_with_underscores() -> None:
	assert lint.sanitize_container_name("chrome extentions") == "chrome_extentions"


@pytest.mark.unit
def test_sanitize_replaces_consecutive_spaces() -> None:
	assert lint.sanitize_container_name("a  b") == "a__b"


@pytest.mark.unit
def test_sanitize_replaces_tabs() -> None:
	assert lint.sanitize_container_name("a\tb") == "a_b"


@pytest.mark.unit
def test_sanitize_replaces_slashes() -> None:
	assert lint.sanitize_container_name("foo/bar/baz") == "foo_bar_baz"


@pytest.mark.unit
def test_sanitize_replaces_colons() -> None:
	assert lint.sanitize_container_name("a:b:c") == "a_b_c"


@pytest.mark.unit
def test_sanitize_replaces_special_characters() -> None:
	assert lint.sanitize_container_name("hello@#$%world") == "hello____world"


@pytest.mark.unit
def test_sanitize_prepends_underscore_when_name_starts_with_non_alnum() -> None:
	assert lint.sanitize_container_name(".hidden") == "_.hidden"


@pytest.mark.unit
def test_sanitize_handles_empty_string() -> None:
	assert lint.sanitize_container_name("") == ""


@pytest.mark.unit
def test_sanitize_allows_dots_after_first_char() -> None:
	assert lint.sanitize_container_name("hello.world") == "hello.world"


@pytest.mark.unit
def test_sanitize_allows_hyphens_after_first_char() -> None:
	assert lint.sanitize_container_name("hello-world") == "hello-world"


@pytest.mark.unit
def test_sanitize_allows_underscores_after_first_char() -> None:
	assert lint.sanitize_container_name("hello_world") == "hello_world"


@pytest.mark.unit
def test_sanitize_allows_digits_at_start() -> None:
	assert lint.sanitize_container_name("123abc") == "123abc"


@pytest.mark.unit
def test_sanitize_replaces_unicode_chars() -> None:
	assert lint.sanitize_container_name("café") == "caf_"


@pytest.mark.unit
def test_sanitize_leading_space_is_handled() -> None:
	assert lint.sanitize_container_name(" leading") == "__leading"


@pytest.mark.unit
def test_sanitize_trailing_space_is_handled() -> None:
	assert lint.sanitize_container_name("trailing ") == "trailing_"


@pytest.mark.unit
def test_sanitize_idempotent() -> None:
	assert lint.sanitize_container_name("already_sanitized-1.0") == "already_sanitized-1.0"


@pytest.mark.unit
def test_sanitize_long_name_preserved() -> None:
	assert lint.sanitize_container_name("a" * 200) == "a" * 200


def _resolve(template: str, ws: Path) -> str:
	return lint._resolve_name_template(template, ws)[0]


@pytest.mark.unit
def test_resolve_localWorkspaceFolderBasename(tmp_path: Path) -> None:
	ws = tmp_path / "my project"
	ws.mkdir()
	assert _resolve("prefix_${localWorkspaceFolderBasename}", ws) == "prefix_my project"


@pytest.mark.unit
def test_resolve_localWorkspaceFolder(tmp_path: Path) -> None:
	ws = tmp_path / "my project"
	ws.mkdir()
	assert _resolve("path_${localWorkspaceFolder}", ws) == f"path_{ws}"


@pytest.mark.unit
def test_resolve_userHome(tmp_path: Path) -> None:
	ws = tmp_path / "p"
	ws.mkdir()
	result = _resolve("home_${userHome}", ws)
	assert result.startswith("home_/")


@pytest.mark.unit
def test_resolve_localEnv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	ws = tmp_path / "p"
	ws.mkdir()
	monkeypatch.setenv("MY_VAR", "hello")
	assert _resolve("env_${localEnv:MY_VAR}", ws) == "env_hello"


@pytest.mark.unit
def test_resolve_leaves_unknown_variable_as_is(tmp_path: Path) -> None:
	ws = tmp_path / "p"
	ws.mkdir()
	assert _resolve("${unknownVar}_suffix", ws) == "${unknownVar}_suffix"


@pytest.mark.unit
def test_resolve_leaves_env_variable_as_is(tmp_path: Path) -> None:
	ws = tmp_path / "p"
	ws.mkdir()
	assert _resolve("${env:SOME_VAR}", ws) == "${env:SOME_VAR}"


@pytest.mark.unit
def test_resolve_multiple_variables_in_one_template(tmp_path: Path) -> None:
	ws = tmp_path / "my project"
	ws.mkdir()
	result = _resolve("${localWorkspaceFolderBasename}_${userHome}", ws)
	assert result.startswith("my project_/")


@pytest.mark.unit
def test_resolve_no_variables(tmp_path: Path) -> None:
	ws = tmp_path / "p"
	ws.mkdir()
	assert _resolve("static-name", ws) == "static-name"


@pytest.mark.unit
def test_resolve_containerWorkspaceFolder_from_config(tmp_path: Path) -> None:
	ws = make_workspace(
		tmp_path / "my project",
		{".devcontainer.json": json.dumps({"workspaceFolder": "/workspaces/custom"})},
	)
	assert _resolve("${containerWorkspaceFolder}", ws) == "/workspaces/custom"


@pytest.mark.unit
def test_resolve_containerWorkspaceFolderBasename_from_config(tmp_path: Path) -> None:
	ws = make_workspace(
		tmp_path / "my project",
		{".devcontainer.json": json.dumps({"workspaceFolder": "/workspaces/custom"})},
	)
	assert _resolve("${containerWorkspaceFolderBasename}", ws) == "custom"


def _config(workspace: Path, run_args: list[str]) -> Path:
	return make_workspace(
		workspace,
		{".devcontainer.json": json.dumps({"runArgs": run_args})},
	)


@pytest.mark.unit
def test_validate_passes_when_no_runArgs(tmp_path: Path) -> None:
	ws = tmp_path / "my project"
	ws.mkdir(parents=True)
	lint.validate_runargs_container_name(ws)


@pytest.mark.unit
def test_validate_passes_when_runArgs_have_no_name(tmp_path: Path) -> None:
	ws = _config(tmp_path / "p", ["--publish=8080:80"])
	lint.validate_runargs_container_name(ws)


@pytest.mark.unit
def test_validate_passes_when_name_is_static_and_valid(tmp_path: Path) -> None:
	ws = _config(tmp_path / "chrome extentions", ["--name=dcman_my_container"])
	lint.validate_runargs_container_name(ws)


@pytest.mark.unit
def test_validate_passes_when_basename_is_already_valid(tmp_path: Path) -> None:
	ws = _config(tmp_path / "my-project", ["--name=dcman_${localWorkspaceFolderBasename}"])
	lint.validate_runargs_container_name(ws)


@pytest.mark.unit
def test_validate_passes_when_name_has_no_variables_and_is_valid(tmp_path: Path) -> None:
	ws = _config(tmp_path / "whatever", ["--name=valid_name-1.0"])
	lint.validate_runargs_container_name(ws)


@pytest.mark.unit
def test_validate_raises_for_invalid_basename(tmp_path: Path) -> None:
	ws = _config(tmp_path / "chrome extentions", ["--name=dcman_${localWorkspaceFolderBasename}"])
	with pytest.raises(CmdError, match="invalid devcontainer name"):
		lint.validate_runargs_container_name(ws)


@pytest.mark.unit
def test_validate_raises_for_invalid_workspace_folder(tmp_path: Path) -> None:
	ws = make_workspace(
		tmp_path / "project",
		{
			".devcontainer.json": json.dumps(
				{
					"runArgs": ["--name=based_on_${localWorkspaceFolder}"],
				}
			),
		},
	)
	with pytest.raises(CmdError) as excinfo:
		lint.validate_runargs_container_name(ws)
	assert "localWorkspaceFolder" in str(excinfo.value)


@pytest.mark.unit
def test_validate_includes_sanitized_suggestion_in_error(tmp_path: Path) -> None:
	ws = _config(tmp_path / "chrome extentions", ["--name=dcman_${localWorkspaceFolderBasename}"])
	with pytest.raises(CmdError) as excinfo:
		lint.validate_runargs_container_name(ws)
	assert "dcman_chrome_extentions" in str(excinfo.value)


@pytest.mark.unit
def test_validate_includes_variable_and_value_in_error(tmp_path: Path) -> None:
	ws = _config(tmp_path / "chrome extentions", ["--name=dcman_${localWorkspaceFolderBasename}"])
	with pytest.raises(CmdError) as excinfo:
		lint.validate_runargs_container_name(ws)
	msg = str(excinfo.value)
	assert "localWorkspaceFolderBasename" in msg
	assert "chrome extentions" in msg


@pytest.mark.unit
def test_validate_passes_with_valid_config_file(tmp_path: Path) -> None:
	ws = _config(tmp_path / "valid-project", ["--name=dcman_${localWorkspaceFolderBasename}"])
	lint.validate_runargs_container_name(ws)


@pytest.mark.unit
def test_validate_passes_for_hidden_dotfile_folder_without_name(tmp_path: Path) -> None:
	ws = _config(tmp_path / ".hidden", [])
	lint.validate_runargs_container_name(ws)


@pytest.mark.unit
def test_validate_reads_from_devcontainer_folder_config(tmp_path: Path) -> None:
	ws = make_workspace(
		tmp_path / "nested" / "project",
		{
			".devcontainer/devcontainer.json": json.dumps(
				{
					"runArgs": ["--name=dcman_${localWorkspaceFolderBasename}"],
				}
			),
		},
	)
	lint.validate_runargs_container_name(ws)


@pytest.mark.unit
def test_validate_skips_when_devcontainerId_remains_unresolved(tmp_path: Path) -> None:
	ws = _config(tmp_path / "p", ["--name=app-${devcontainerId}"])
	lint.validate_runargs_container_name(ws)


@pytest.mark.unit
def test_validate_skips_when_containerEnv_remains_unresolved(tmp_path: Path) -> None:
	ws = _config(tmp_path / "p", ["--name=app-${containerEnv:VERSION}"])
	lint.validate_runargs_container_name(ws)


@pytest.mark.unit
def test_validate_reads_from_devcontainer_json_when_both_exist(tmp_path: Path) -> None:
	ws = make_workspace(
		tmp_path / "both",
		{
			".devcontainer.json": json.dumps(
				{
					"runArgs": ["--name=dcman_${localWorkspaceFolderBasename}"],
				}
			),
			".devcontainer/devcontainer.json": json.dumps(
				{
					"runArgs": ["--name=dcman_other"],
				}
			),
		},
	)
	lint.validate_runargs_container_name(ws)


_CONFLICT_NAME = "--name=dcman_${localWorkspaceFolderBasename}"


def _make_fake_container(labels: dict | None = None) -> MagicMock:
	container = MagicMock()
	container.config.labels = labels or {}
	return container


@pytest.mark.unit
def test_conflict_passes_when_no_container_exists(tmp_path: Path) -> None:
	ws = make_workspace(
		tmp_path / "my-project",
		{".devcontainer.json": json.dumps({"runArgs": [_CONFLICT_NAME]})},
	)
	with patch("dcman.lint.DockerClient") as mock_cls:
		mock_cls.return_value.container.list.return_value = []
		lint.check_container_name_conflict(ws, engine="podman")


@pytest.mark.unit
def test_conflict_passes_when_existing_container_belongs_to_same_workspace(tmp_path: Path) -> None:
	ws = make_workspace(
		tmp_path / "my-project",
		{".devcontainer.json": json.dumps({"runArgs": [_CONFLICT_NAME]})},
	)
	fake = _make_fake_container({"devcontainer.local_folder": str(ws.resolve())})
	with patch("dcman.lint.DockerClient") as mock_cls:
		mock_cls.return_value.container.list.return_value = [fake]
		lint.check_container_name_conflict(ws, engine="podman")


@pytest.mark.unit
def test_conflict_raises_when_existing_container_belongs_to_different_workspace(tmp_path: Path) -> None:
	ws = make_workspace(
		tmp_path / "my-project",
		{".devcontainer.json": json.dumps({"runArgs": [_CONFLICT_NAME]})},
	)
	fake = _make_fake_container({"devcontainer.local_folder": "/some/other/workspace"})
	with patch("dcman.lint.DockerClient") as mock_cls:
		mock_cls.return_value.container.list.return_value = [fake]
		with pytest.raises(CmdError, match="already used by.*workspace"):
			lint.check_container_name_conflict(ws, engine="podman")


@pytest.mark.unit
def test_conflict_raises_when_existing_container_has_no_label(tmp_path: Path) -> None:
	ws = make_workspace(
		tmp_path / "my-project",
		{".devcontainer.json": json.dumps({"runArgs": [_CONFLICT_NAME]})},
	)
	fake = _make_fake_container(None)
	with patch("dcman.lint.DockerClient") as mock_cls:
		mock_cls.return_value.container.list.return_value = [fake]
		with pytest.raises(CmdError, match="not managed by dcman"):
			lint.check_container_name_conflict(ws, engine="podman")


@pytest.mark.unit
def test_conflict_handles_docker_exception_gracefully(tmp_path: Path) -> None:
	ws = make_workspace(
		tmp_path / "my-project",
		{".devcontainer.json": json.dumps({"runArgs": [_CONFLICT_NAME]})},
	)
	with patch("dcman.lint.DockerClient") as mock_cls:
		mock_cls.return_value.container.list.side_effect = DockerException(["podman", "container", "list"], 1)
		lint.check_container_name_conflict(ws, engine="podman")


@pytest.mark.unit
def test_conflict_passes_when_no_runArgs(tmp_path: Path) -> None:
	ws = tmp_path / "my-project"
	ws.mkdir(parents=True)
	lint.check_container_name_conflict(ws, engine="podman")


@pytest.mark.unit
def test_conflict_passes_when_resolved_name_is_invalid(tmp_path: Path) -> None:
	ws = make_workspace(
		tmp_path / "chrome extentions",
		{".devcontainer.json": json.dumps({"runArgs": [_CONFLICT_NAME]})},
	)
	with patch("dcman.lint.DockerClient") as mock_cls:
		lint.check_container_name_conflict(ws, engine="podman")
		mock_cls.assert_not_called()
