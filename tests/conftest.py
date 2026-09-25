"""Fixtures shared by the test modules."""

from __future__ import annotations

import pytest

from line_control.console.server import build_router
from line_control.line.supervisor import LineSupervisor

from tests.support import open_line


@pytest.fixture
def line(tmp_path) -> LineSupervisor:
    """Return an empty supervisor on a scratch data directory."""
    return open_line(tmp_path)


@pytest.fixture
def unit(line: LineSupervisor) -> str:
    """Register a unit and return its name."""
    line.register_unit("u1")
    return "u1"


@pytest.fixture
def router(line: LineSupervisor):
    """Return the console router bound to the scratch supervisor."""
    return build_router(line)
