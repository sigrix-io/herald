"""The rules that keep Herald general, as sweeps of its own source.

Each sweep also checks that it found what it exists to look at: a sweep that finds nothing reads
the same as a tree with nothing to find.
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import pkgutil
import sys
import tomllib
from pathlib import Path

import herald
import herald.channels
from herald import Channel

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "herald"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _modules() -> list[Path]:
    modules = sorted(SOURCE.rglob("*.py"))
    assert len(modules) >= 9, "the sweep found Herald's modules"
    return modules


def test_the_code_names_no_deployment_of_its_own() -> None:
    """No address, account or default of the company that wrote it: Herald is anyone's."""
    found = [path.relative_to(ROOT) for path in _modules() if "sigrix" in path.read_text(encoding="utf-8").lower()]
    assert found == []


def test_herald_imports_nothing_outside_the_standard_library() -> None:
    imported: set[str] = set()
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    assert {"json", "herald"} <= imported, "the sweep read the imports"
    assert sorted(name for name in imported if name != "herald" and name not in sys.stdlib_module_names) == []


def test_the_package_declares_no_dependencies() -> None:
    assert PYPROJECT["project"]["dependencies"] == []
    assert importlib.metadata.requires("herald-post") is not None, "the installed package's requirements were read"
    assert [req for req in importlib.metadata.requires("herald-post") if "extra ==" not in req] == []


def test_the_version_is_one_number() -> None:
    assert herald.__version__ == PYPROJECT["project"]["version"] == importlib.metadata.version("herald-post")


def test_the_changelog_has_an_entry_for_the_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"\n## {herald.__version__} " in changelog


def test_every_built_in_channel_registers_through_the_entry_point_group() -> None:
    """The built-in channels are plugins like anyone's, so each is in pyproject.toml's group."""
    declared = PYPROJECT["project"]["entry-points"]["herald.channels"]
    built_in: dict[str, str] = {}
    for module in pkgutil.iter_modules(herald.channels.__path__, "herald.channels."):
        loaded = importlib.import_module(module.name)
        for value in vars(loaded).values():
            if isinstance(value, type) and issubclass(value, Channel) and value.__module__ == module.name:
                built_in[value.name] = f"{module.name}:{value.__name__}"
    assert "stdout" in built_in, "the sweep found the built-in channels"
    assert declared == built_in
