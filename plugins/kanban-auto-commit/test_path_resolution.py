"""Regression: plugin _board_db_path must resolve under profile-scoped HERMES_HOME.

The dispatcher injects HERMES_KANBAN_DB into worker envs so plugins can
find the root kanban DB even when HERMES_HOME points at profiles/<name>.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Import the plugin module directly to test its path resolution logic.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import __init__ as pkg
import importlib
importlib.reload(pkg)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)


def test_root_hermes_home_resolves(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "kanban" / "boards" / "realm-forge").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    pkg.HERMES_HOME = root
    pkg.KANBAN_HOME = root / "kanban"
    got = pkg._board_db_path("realm-forge")
    assert got == root / "kanban" / "boards" / "realm-forge" / "kanban.db"


def test_profile_scoped_without_override_points_to_missing_dir(monkeypatch, tmp_path):
    profile = tmp_path / "profiles" / "kbw"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    pkg.HERMES_HOME = profile
    pkg.KANBAN_HOME = profile / "kanban"
    got = pkg._board_db_path("realm-forge")
    assert got == profile / "kanban" / "boards" / "realm-forge" / "kanban.db"
    assert not got.exists()  # does NOT fall back to root


def test_hermes_kanban_db_env_overrides_profile_scoped(monkeypatch, tmp_path):
    profile = tmp_path / "profiles" / "kbw"
    profile.mkdir(parents=True)
    root = tmp_path / "root"
    (root / "kanban" / "boards" / "realm-forge").mkdir(parents=True)
    override = root / "kanban" / "boards" / "realm-forge" / "kanban.db"
    override.touch()
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(override))
    pkg.HERMES_HOME = profile
    pkg.KANBAN_HOME = profile / "kanban"
    got = pkg._board_db_path("realm-forge")
    assert got == override
    assert got.exists()
