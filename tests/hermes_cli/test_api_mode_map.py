"""``model.api_mode_map`` resolution and its precedence over inference.

The module ships no tests of its own (ported from a local commit), and the
integration point is a one-line insertion at the top of ``_resolve_api_mode``'s
ladder -- exactly the kind of edit a future upstream refactor can silently drop.
These assert the *contract*, not the implementation:

1. a declared map outranks provider/hostname inference,
2. no declared map leaves the legacy inference chain untouched,
3. a declared-but-unlisted model raises rather than falling back.

(3) is the whole point of the feature: an OpenAI-shaped tool schema sent to an
Anthropic model behind a multiplexing proxy degrades tool calling instead of
erroring, which reads as hallucination rather than a transport bug.
"""
import pytest

from hermes_cli.api_mode_map import (
    ApiModeConfigError,
    normalize_api_mode_map,
    resolve_api_mode,
)


def test_declared_map_normalizes_and_resolves_per_model():
    """Each model maps to its own wire protocol -- no provider-level fallback."""
    cfg = normalize_api_mode_map(
        {"claude-opus-5": "anthropic_messages", "gpt-5.6-sol": "chat_completions"},
        config_path="/tmp/config.yaml",
        legacy_api_mode=None,
    )
    assert resolve_api_mode("claude-opus-5", cfg, config_path="/tmp/config.yaml") == \
        "anthropic_messages"
    assert resolve_api_mode("gpt-5.6-sol", cfg, config_path="/tmp/config.yaml") == \
        "chat_completions"


def test_unlisted_model_raises_instead_of_falling_back():
    """The strictness IS the feature: silent fallback is the bug being fixed."""
    cfg = normalize_api_mode_map(
        {"claude-opus-5": "anthropic_messages"},
        config_path="/tmp/config.yaml",
        legacy_api_mode=None,
    )
    with pytest.raises(ApiModeConfigError) as exc:
        resolve_api_mode("some-new-model", cfg, config_path="/tmp/config.yaml")
    # The message must name the offending model, or the error is undebuggable.
    assert "some-new-model" in str(exc.value)


def test_invalid_mode_value_is_rejected_at_normalize_time():
    """Startup, not first-API-call: a typo must not survive into the turn loop."""
    with pytest.raises(ApiModeConfigError):
        normalize_api_mode_map(
            {"claude-opus-5": "anthropic-messages"},  # hyphen, not underscore
            config_path="/tmp/config.yaml",
            legacy_api_mode=None,
        )


def test_declared_mode_outranks_hostname_inference():
    """The integration contract: _resolve_declared_api_mode is consulted first.

    A custom provider on an OpenAI-shaped base_url infers ``chat_completions``.
    With a map declaring the model as Anthropic, the declaration must win --
    this is the regression that made tool calling drop to 1/12 on Claude models
    behind a LiteLLM-style proxy.
    """
    from agent.agent_init import _resolve_api_mode

    class _Agent:
        provider = "custom"
        base_url = "http://127.0.0.1:4040/v1"
        _base_url_hostname = "127.0.0.1"
        _base_url_lower = "http://127.0.0.1:4040/v1"
        model = "claude-opus-5"
        api_mode = None
        _config_path = "/tmp/config.yaml"
        _api_mode_map = {"claude-opus-5": "anthropic_messages"}

    agent = _Agent()
    _resolve_api_mode(agent, None, "custom", agent.base_url)
    assert agent.api_mode == "anthropic_messages"


def test_no_declared_map_preserves_legacy_inference():
    """Configs predating the feature must route exactly as before."""
    from agent.agent_init import _resolve_api_mode

    class _Agent:
        provider = "anthropic"
        base_url = "https://api.anthropic.com"
        _base_url_hostname = "api.anthropic.com"
        _base_url_lower = "https://api.anthropic.com"
        model = "claude-opus-5"
        api_mode = None
        _config_path = None
        _api_mode_map = {}  # declared-empty == not configured

    agent = _Agent()
    _resolve_api_mode(agent, None, None, agent.base_url)
    # Reached via the provider branch of the ladder, not the declaration.
    assert agent.api_mode == "anthropic_messages"
