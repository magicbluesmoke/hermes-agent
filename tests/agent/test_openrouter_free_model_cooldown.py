"""An OpenRouter free-tier 429 benches only the free model it names.

OpenRouter meters ``:free`` models per model per day (``free-models-per-day``).
A worker on a free model that exhausts that bucket must still be able to fall
back to a paid model on the same key — the credential is healthy. Before this
fix the 429 marked the whole credential exhausted, and the fallback walk then
skipped every candidate with "credential pool is exhausted (every entry in
cooldown)", including the paid escape hatch (#free-models-per-day).
"""
import json

import pytest

FREE_MODEL = "nvidia/nemotron-3.5-lightning:free"
PAID_MODEL = "~deepseek/deepseek-flash-latest"
FREE_429 = {
    "message": (
        "Rate limit exceeded: free-models-per-day-high-balance. Add 10 credits "
        "to unlock 1000 free model requests per day."
    )
}


def _seed_pool(tmp_path, monkeypatch, provider="openrouter"):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    entries = [{
        "id": "cred-0", "label": "key-0", "auth_type": "api_key",
        "priority": 0, "source": "manual", "access_token": "sk-or-key",
    }]
    (hermes_home / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": {provider: entries}})
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    from agent.credential_pool import load_pool
    return load_pool(provider)


def test_free_model_429_keeps_credential_available_for_paid_models(tmp_path, monkeypatch):
    pool = _seed_pool(tmp_path, monkeypatch)
    assert pool.select(model=FREE_MODEL) is not None

    nxt = pool.mark_exhausted_and_rotate(
        status_code=429, error_context=FREE_429, api_key_hint="sk-or-key",
        failure_reason="rate_limit", model=FREE_MODEL,
    )
    # Sole credential: nothing to rotate to for the free model itself.
    assert nxt is None

    entry = pool.entries()[0]
    assert entry.last_status is None, "free-model 429 must not bench the credential"
    assert pool.select(model=FREE_MODEL) is None, "the free model is still cooling down"
    assert pool.select(model=PAID_MODEL) is not None, "paid fallback must stay usable"
    assert pool.has_available(model=PAID_MODEL) is True
    assert pool.next_available_at(model=PAID_MODEL) is None
    assert pool.next_available_at(model=FREE_MODEL) is not None


def test_paid_model_429_still_benches_the_credential(tmp_path, monkeypatch):
    """A 429 on a paid model is an account-level throttle: unchanged behaviour."""
    pool = _seed_pool(tmp_path, monkeypatch)
    pool.mark_exhausted_and_rotate(
        status_code=429,
        error_context={"message": "Rate limit exceeded"},
        api_key_hint="sk-or-key",
        failure_reason="rate_limit",
        model=PAID_MODEL,
    )
    entry = pool.entries()[0]
    assert entry.last_status == "exhausted"
    assert pool.has_available(model=PAID_MODEL) is False


def test_free_quota_marker_detected_without_model_suffix(tmp_path, monkeypatch):
    """Provider may omit the ``:free`` suffix (e.g. an alias); the body still names the bucket."""
    pool = _seed_pool(tmp_path, monkeypatch)
    pool.mark_exhausted_and_rotate(
        status_code=429, error_context=FREE_429, api_key_hint="sk-or-key",
        failure_reason="rate_limit", model="nvidia/nemotron-3.5-lightning",
    )
    assert pool.entries()[0].last_status is None
    assert pool.select(model=PAID_MODEL) is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
