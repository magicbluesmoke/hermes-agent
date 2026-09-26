"""Tests for kanban-complexity-routing assignee validation."""

import sys
from unittest import mock

# Plugin lives under profiles dir; put it on path
sys.path.insert(0, r"C:\Users\Michael Anselmi\AppData\Local\hermes\plugins\kanban-complexity-routing\src")


def test_known_assignees_allowlist():
    """When kanban.valid_assignees is set, it is the ONLY source."""
    from handler import _load_known_assignees

    fake_cfg = {
        "kanban": {
            "valid_assignees": ["kanban-worker", "default", "local-only", "moa"],
        }
    }
    with mock.patch("handler._load_config", return_value=fake_cfg):
        result = _load_known_assignees()
    assert result == {"kanban-worker", "default", "local-only", "moa"}


def test_known_assignees_fallback():
    """Without allowlist, only kanban-worker + default are accepted."""
    from handler import _load_known_assignees

    with mock.patch("handler._load_config", return_value={"kanban": {"default_assignee": "kanban-worker"}}):
        result = _load_known_assignees()
    assert result == {"kanban-worker", "default"}


def test_known_assignees_json_string():
    """Config tool stores YAML lists as JSON strings in some paths."""
    from handler import _load_known_assignees

    fake_cfg = {"kanban": {"valid_assignees": '["kanban-worker","default"]'}}
    with mock.patch("handler._load_config", return_value=fake_cfg):
        result = _load_known_assignees()
    assert result == {"kanban-worker", "default"}


if __name__ == "__main__":
    test_known_assignees_allowlist()
    print("PASS: allowlist")
    test_known_assignees_fallback()
    print("PASS: fallback")
    test_known_assignees_json_string()
    print("PASS: json string")
    print("All tests passed")
