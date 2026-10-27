"""Shared pytest fixtures.

Sets DOTENV_PATH=/dev/null so importing the server / ingest modules does not
require /opt/health/.env (which only exists on the production VPS and is
chmod 600). Provides a `tmp_data_dir` fixture that drops fixture CSVs into a
temp directory and points DATA_DIR at it for the duration of the test.
"""
import importlib
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Force the application's load_dotenv() call to no-op rather than try to read
# /opt/health/.env on the test runner.
os.environ["DOTENV_PATH"] = "/dev/null"

# Both services refuse to start with a token shorter than 32 characters.
TEST_TOKEN = "t" * 64


def _import_server(data_dir: Path):
    """Import (or reimport) mcp.server with DATA_DIR pointed at data_dir.

    The server reads DATA_DIR at module import, so we reload between tests
    that need a different directory. We also stub out FastMCP so importing
    does not bind ports or otherwise depend on the real mcp package's HTTP
    machinery — only the loader / tool functions are exercised in tests.
    """
    import types

    fake = types.ModuleType("mcp.server.fastmcp")

    class _StubMCP:
        def __init__(self, *a, **kw):
            self.settings = types.SimpleNamespace(
                transport_security=types.SimpleNamespace()
            )

        def tool(self):
            return lambda fn: fn

        def custom_route(self, path, methods, **kw):
            return lambda fn: fn

        def streamable_http_app(self):
            return lambda *a, **kw: None

    fake.FastMCP = _StubMCP
    parent = types.ModuleType("mcp.server")
    parent.fastmcp = fake
    root = types.ModuleType("mcp")
    root.server = parent
    sys.modules["mcp"] = root
    sys.modules["mcp.server"] = parent
    sys.modules["mcp.server.fastmcp"] = fake

    os.environ["HEALTH_TOKEN"] = TEST_TOKEN
    os.environ["DATA_DIR"] = str(data_dir)
    workouts_dir = data_dir.parent / "workouts"
    workouts_dir.mkdir(exist_ok=True)
    os.environ["WORKOUTS_DIR"] = str(workouts_dir)
    for name, folder in (
        ("SLEEP_DIR", "sleep"),
        ("ECG_DIR", "ecg"),
        ("HR_NOTIFICATIONS_DIR", "hr-notifications"),
        ("STATE_OF_MIND_DIR", "state-of-mind"),
        ("BUTLER_DIR", "butler"),
    ):
        (data_dir.parent / folder).mkdir(exist_ok=True)
        os.environ[name] = str(data_dir.parent / folder)
    # Tests that don't care about reports get a sibling reports/ dir for free.
    reports_dir = data_dir.parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    os.environ["REPORTS_DIR"] = str(reports_dir)

    sys.path.insert(0, str(REPO_ROOT / "mcp"))
    if "server" in sys.modules:
        return importlib.reload(sys.modules["server"])
    import server  # noqa: PLC0415
    return server


@pytest.fixture
def tmp_data_dir(tmp_path: Path):
    """Empty temp directory with DATA_DIR env var pointing at it.

    It is a child of ``tmp_path`` so the sibling reports/ and workouts/
    directories that ``_import_server`` creates are private to each test.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def server_module(tmp_data_dir: Path):
    """Importable server module with DATA_DIR set to a fresh tmp dir."""
    return _import_server(tmp_data_dir)


@pytest.fixture
def populated_data_dir(tmp_data_dir: Path):
    """Tmp data dir seeded with the clean and wrapped fixture CSVs."""
    shutil.copy(FIXTURES / "sample_clean.csv", tmp_data_dir / "health_clean.csv")
    shutil.copy(FIXTURES / "sample_wrapped.csv", tmp_data_dir / "health_wrapped.csv")
    return tmp_data_dir


@pytest.fixture
def reports_dir(tmp_path: Path):
    """Empty reports dir paired with a fresh server module pointed at it."""
    rd = tmp_path / "reports"
    rd.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    server = _import_server(data_dir)
    # _import_server set REPORTS_DIR to data_dir.parent/"reports"; that is rd.
    return rd, server
