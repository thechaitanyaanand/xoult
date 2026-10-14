from types import SimpleNamespace

import litellm
import pytest

from xoult import llm

MODELS = ["groq/a", "gemini/b", "xai/c"]


@pytest.fixture
def calls(monkeypatch):
    """Fake providers: groq/a is rate limited, xai/c has a bad key, gemini/b works."""
    made = []

    def completion(model, **kwargs):
        made.append(model)
        if model == "groq/a":
            raise litellm.RateLimitError("429", llm_provider="groq", model=model)
        if model == "xai/c":
            raise litellm.AuthenticationError("bad key", llm_provider="xai", model=model)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(llm, "_cooldown", {})
    return made


def test_falls_back_and_rests_rate_limited_model(calls, monkeypatch):
    assert llm.complete([], "t", models=MODELS) == "ok"
    assert calls == ["groq/a", "gemini/b"]

    # Within the cooldown the rate-limited model is skipped, not retried.
    calls.clear()
    assert llm.complete([], "t", models=MODELS) == "ok"
    assert calls == ["gemini/b"]

    # After a minute it gets another chance.
    calls.clear()
    now = llm.time.monotonic()
    monkeypatch.setattr(llm.time, "monotonic", lambda: now + llm.RATE_LIMITED_FOR + 1)
    llm.complete([], "t", models=MODELS)
    assert calls == ["groq/a", "gemini/b"]


def test_broken_model_rests_longer(calls, monkeypatch):
    with pytest.raises(litellm.AuthenticationError):
        llm.complete([], "t", models=["xai/c"])
    now = llm.time.monotonic()
    monkeypatch.setattr(llm.time, "monotonic", lambda: now + llm.RATE_LIMITED_FOR + 1)
    calls.clear()
    llm.complete([], "t", models=["xai/c", "gemini/b"])
    assert calls == ["gemini/b"]


def test_everything_resting_still_tries(calls):
    llm._cooldown.update({m: float("inf") for m in MODELS})
    assert llm.complete([], "t", models=MODELS) == "ok"
    assert calls == ["groq/a", "gemini/b"]
