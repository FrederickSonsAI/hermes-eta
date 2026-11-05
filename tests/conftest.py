"""Load the plugin directory as a package named eta_plugin, without Hermes installed."""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    if "eta_plugin" in sys.modules:
        return sys.modules["eta_plugin"]
    spec = importlib.util.spec_from_file_location(
        "eta_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eta_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def pkg():
    return _load()


@pytest.fixture
def store(pkg):
    from eta_plugin.store import RunStore
    return RunStore(connect=lambda: sqlite3.connect(":memory:", check_same_thread=False))


class FakeCtx:
    def __init__(self):
        self.tools, self.hooks, self.commands, self.skills = {}, {}, {}, {}
        self.config = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        assert schema["name"] == name
        self.tools[name] = handler

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_command(self, name, handler, description="", args_hint="", **kw):
        self.commands[name] = handler

    def register_skill(self, name, path):
        assert Path(path).exists()
        self.skills[name] = path

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def dispatch_tool(self, name, args, **kw):
        return '{"error": "Unknown tool: %s"}' % name


@pytest.fixture
def fake_ctx():
    return FakeCtx()
