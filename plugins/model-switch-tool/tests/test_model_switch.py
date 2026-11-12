import json, os, sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)
for p in ["/home/michael/.hermes/installs/55dbd0577755667c/environments/de7a5629ca5649fcb179f5140591457e/workspace", "/home/michael/.hermes/hermes-agent"]:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from tool import SCHEMA, check_fn, handle_model_switch


def test_schema_shape():
    assert SCHEMA["name"] == "model_switch"
    assert "model" in SCHEMA["parameters"]["properties"]
    assert SCHEMA["parameters"]["required"] == ["model"]
    assert set(SCHEMA["parameters"]["properties"]["scope"]["enum"]) == {"turn", "session", "global"}


def test_check_fn():
    # check_fn depends on hermes_cli.model_switch importability in the test env;
    # at runtime the plugin is enabled, so just ensure it returns a bool.
    assert isinstance(check_fn(), bool)


def test_invalid_scope():
    out = handle_model_switch({"model": "foo", "scope": "bogus"})
    assert "invalid scope" in str(out).lower() or "error" in str(out).lower()


def test_empty_model_returns_current_state():
    out = handle_model_switch({"model": ""})
    assert "current_model" in str(out)
    assert "No model specified" in str(out)
