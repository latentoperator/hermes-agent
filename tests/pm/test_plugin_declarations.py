"""Plugin declaration safeguards from #113851, through PM's real resolver."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest

from pm.environment import PythonEnvironment
from pm.plugin_declarations import read_python_declaration, unsupported_requirements
from pm.workspace import _generate_pyproject, enabled_member_dirs, lock_and_sync
from tests.pm import _fixtures


def test_tool_only_pyproject_is_not_an_installable_plugin(tmp_path):
    (tmp_path / "plugin.yaml").write_text("name: browser-steel\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    declaration = read_python_declaration(tmp_path)
    assert not declaration.installs_packages


def test_profile_copies_keep_unique_dependency_only_workspace_members(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "pyproject.toml").write_text(
        '[project]\nname="hermes-agent"\nversion="0.0.0"\n', encoding="utf-8"
    )
    projects = []
    for profile in ("default", "dante"):
        plugin = tmp_path / profile / "plugins" / "hindsight"
        plugin.mkdir(parents=True)
        (plugin / "pyproject.toml").write_text(
            '[project]\nname="hermes-plugin-hindsight"\nversion="1"\n'
            'dependencies=["hindsight-client>=0.10.1,<1"]\n'
            '[tool.uv]\npackage=false\n', encoding="utf-8"
        )
        projects.append(plugin)
    workspace = tmp_path / "workspace"
    _generate_pyproject(projects, workspace, source=core)
    document = tomllib.loads((workspace / "pyproject.toml").read_text())
    members = document["tool"]["uv"]["workspace"]["members"]
    assert len(members) == 2
    generated = [tomllib.loads((workspace / member / "pyproject.toml").read_text())["project"]
                 for member in members]
    assert len({project["name"] for project in generated}) == 2
    assert all(project["dependencies"] == ["hindsight-client>=0.10.1,<1"] for project in generated)


@pytest.mark.parametrize("compatible", [True, False])
def test_virtual_profile_dependency_constraints_are_resolved_together(tmp_path, compatible):
    core = tmp_path / "core"
    core.mkdir()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for version in ("1.0", "2.0"):
        _fixtures._wheel(wheels, "fixturedep", version)
    (core / "pyproject.toml").write_text(
        '[project]\nname="core"\nversion="1"\nrequires-python=">=3.11"\n'
        '[tool.uv]\npackage=false\nno-index=true\n'
        f'find-links=[{json.dumps(wheels.as_posix())}]\n', encoding="utf-8")
    first = "fixturedep>=1,<3" if compatible else "fixturedep==1.0"
    projects = []
    for profile, spec in (("default", first), ("virgil", "fixturedep==2.0")):
        plugin = tmp_path / profile / "plugins" / "same-plugin"
        plugin.mkdir(parents=True)
        (plugin / "pyproject.toml").write_text(
            '[project]\nname="same-plugin"\nversion="1"\n'
            f'dependencies={json.dumps([spec])}\n[tool.uv]\npackage=false\n', encoding="utf-8")
        projects.append(plugin)
    workspace = tmp_path / "workspace"
    _generate_pyproject(projects, workspace, source=core)
    uv = shutil.which("uv")
    assert uv, "real offline resolver is required"
    result = subprocess.run(
        [uv, "lock", "--offline", "--no-index", "--find-links", str(wheels),
         "--python", sys.executable], cwd=workspace,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
             "UV_CACHE_DIR": str(tmp_path / "cache"), "UV_NO_CONFIG": "1"},
        capture_output=True, text=True, timeout=30, check=False)
    if compatible:
        assert result.returncode == 0, result.stderr
        packages = tomllib.loads((workspace / "uv.lock").read_text())["package"]
        assert any(p["name"] == "fixturedep" and p["version"] == "2.0" for p in packages)
    else:
        assert result.returncode != 0
        assert "No solution found" in result.stderr
        assert "fixturedep==1.0" in result.stderr and "fixturedep==2.0" in result.stderr
        assert "not found in the cache" not in result.stderr


@pytest.mark.parametrize("modern", [False, True])
def test_declaration_policy_survives_cross_profile_resolution(tmp_path, monkeypatch, modern):
    home = tmp_path / "custom-home"
    sibling = home / "profiles" / "work"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(sibling))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    core = tmp_path / "core"
    core.mkdir()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _fixtures._wheel(wheels, "fixturedep", "1.0")
    (core / "pyproject.toml").write_text(
        '[project]\nname="core"\nversion="1"\nrequires-python=">=3.11"\n'
        '[tool.uv]\npackage=false\nno-index=true\n'
        f'find-links=[{json.dumps(wheels.as_posix())}]\n', encoding="utf-8",
    )
    plugin = home / "plugins" / "member"
    external = sibling / "plugins" / "sidecar"
    plugin.mkdir(parents=True)
    external.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [member]\n", encoding="utf-8")
    (sibling / "config.yaml").write_text("memory:\n  provider: sidecar\n", encoding="utf-8")
    (external / "plugin.yaml").write_text(
        'name: sidecar\npython_runtime: external\npython_dependencies: ["impossible==99"]\n',
        encoding="utf-8",
    )
    (external / "pyproject.toml").write_text(
        '[project]\nname="sidecar"\nversion="1"\ndependencies=["impossible==99"]\n',
        encoding="utf-8",
    )
    specs = ['fixturedep>=1,<2', 'hermes-agent>=0.1,<1',
             'remote @ https://example.invalid/unreviewed.whl',
             'missing-other-python; python_version < "3.0"']
    (plugin / "plugin.yaml").write_text(
        'name: member\npython_dependencies: ' + json.dumps(["ignored==99"] if modern else specs) + '\n',
        encoding="utf-8",
    )
    if modern:
        (plugin / "pyproject.toml").write_text(
            '[project]\nname="member"\nversion="1"\nrequires-python=">=3.11"\n'
            f'dependencies={json.dumps(specs)}\n[tool.uv]\npackage=false\n', encoding="utf-8",
        )
    before = {p: p.read_bytes() for directory in (plugin, external) for p in directory.iterdir()}
    assert enabled_member_dirs() == [plugin]
    assert read_python_declaration(external).external
    declaration = read_python_declaration(plugin)
    assert declaration.requirements == tuple(specs)
    assert unsupported_requirements(declaration.requirements) == (specs[2],)
    uv = shutil.which("uv")
    assert uv is not None, "real resolver is required"
    environment = PythonEnvironment(
        uv=Path(uv), python=Path(sys.executable), destination=tmp_path / "env",
        cache=tmp_path / "cache", env=dict(os.environ), offline=True,
    )
    root = tmp_path / "snapshot"
    lock_and_sync(enabled_member_dirs(), [], root=root, source=core, seed_lock=None,
                  environment=environment)
    probe = subprocess.run(
        [str(environment.executable), "-I", "-c", "import fixturedep; print(fixturedep.__version__)"],
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert probe.stdout.strip() == "1.0"
    [relative] = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["uv"]["workspace"]["members"]
    generated = tomllib.loads((root / relative / "pyproject.toml").read_text())["project"]["dependencies"]
    assert generated == [specs[0], specs[3]], "target markers must reach uv, unsafe/self requirements must not"
    assert all(p.read_bytes() == body for p, body in before.items())


@pytest.mark.parametrize("modern", [False, True])
def test_invalid_requirement_refused_at_declaration_boundary(tmp_path, modern):
    if modern:
        path = tmp_path / "pyproject.toml"
        path.write_text('[project]\ndependencies=["not a requirement >="]\n', encoding="utf-8")
    else:
        path = tmp_path / "plugin.yaml"
        path.write_text('python_dependencies: ["not a requirement >="]\n', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        read_python_declaration(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(("body", "installs"), [
    ("dependencies = []\n", False),
    ('dependencies = ["fixturedep>=1,<2"]\n', True),
    ('dependencies = []\n[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n', True),
    ('dependencies = []\n[dependency-groups]\ndev = ["fixturedep"]\n', True),
    ('dependencies = []\n[project.optional-dependencies]\nextra = ["fixturedep"]\n', True),
], ids=["empty", "dependencies", "buildable", "dependency-groups", "extras"])
def test_only_a_pyproject_that_installs_something_needs_dependency_consent(tmp_path, body, installs):
    """``dependencies = []`` puts nothing in the environment; a buildable plugin, extras and dependency
    groups (``uv sync --all-packages`` installs a member's default groups) still do."""
    (tmp_path / "plugin.yaml").write_text("name: probe\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "probe"\nversion = "1"\n' + body, encoding="utf-8")
    assert read_python_declaration(tmp_path).installs_packages is installs
