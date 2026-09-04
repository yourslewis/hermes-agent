"""Per-model API-mode declaration and validation.

Background
----------
``agent.api_mode`` decides the wire protocol used to talk to a model:
``chat_completions`` sends OpenAI-shaped payloads (``/chat/completions``,
OpenAI ``tools`` schema) while ``anthropic_messages`` sends Anthropic-shaped
payloads (``/v1/messages``, ``input_schema`` tools).

Historically the mode was inferred in ``agent.agent_init`` from the *provider
name* and the *base-URL hostname* only.  That inference is wrong for any
endpoint that multiplexes several model families behind one URL (LiteLLM,
Copilot passthroughs, in-house gateways): a ``provider: custom`` entry
pointing at ``http://127.0.0.1:4040/v1`` matched no branch and silently fell
through to ``chat_completions``, so Anthropic models received OpenAI-format
tool schemas.  Measured effect on Claude models behind such a proxy: tool
invocation dropped from 12/12 to 1/12 on prompts that required a tool, with
the model emitting "I'll look into the repo..." as a *final* answer and no
``tool_calls`` field.  It looks exactly like hallucination, but it is a
transport-negotiation bug.

Contract
--------
This module makes the mode an explicit, per-model, *mandatory* declaration:

.. code-block:: yaml

    model:
      provider: custom
      base_url: http://127.0.0.1:4040/v1
      default: claude-opus-5
      api_mode_map:
        claude-opus-5: anthropic_messages
        gpt-5.6-sol:   chat_completions

Rules (deliberately strict — there is no safe default):

* ``api_mode_map`` must be present and non-empty.
* Every model actually used must have its own entry.  There is no
  provider-level fallback and no inheritance.
* Values must be one of :data:`VALID_API_MODES`.  Empty strings, ``None``
  and unknown values are rejected.
* Violations raise :class:`ApiModeConfigError` at startup, not on the first
  API call, and the message names the offending model and config path.

Setting the legacy scalar ``model.api_mode`` alongside ``api_mode_map`` is an
error: two sources of truth for the same decision is the ambiguity this
module exists to remove.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

__all__ = [
    "VALID_API_MODES",
    "ApiModeConfigError",
    "normalize_api_mode_map",
    "resolve_api_mode",
]


# Wire protocols Hermes can actually speak.  Keep in sync with the
# ``api_mode`` branch in ``agent/agent_init.py`` and the transport registry
# in ``agent/transports/__init__.py``.
VALID_API_MODES = (
    "chat_completions",
    "anthropic_messages",
    "codex_responses",
    "bedrock_converse",
    "codex_app_server",
)

_VALID_SET = frozenset(VALID_API_MODES)

# Shown verbatim in error messages so a user can copy a correct value out.
_VALID_LIST = ", ".join(VALID_API_MODES)


class ApiModeConfigError(ValueError):
    """Raised when ``model.api_mode_map`` is missing, malformed or incomplete.

    Deliberately fatal: a wrong ``api_mode`` degrades tool-calling silently
    rather than erroring, so guessing on the user's behalf trades a loud
    startup failure for a quiet, hard-to-diagnose behavioural regression.
    """


def _config_hint(config_path: Optional[str]) -> str:
    """Render a ' (in <path>)' suffix for error messages, or '' if unknown."""
    return f" (in {config_path})" if config_path else ""


def normalize_api_mode_map(
    raw: Any,
    *,
    config_path: Optional[str] = None,
    legacy_api_mode: Any = None,
) -> Dict[str, str]:
    """Validate and normalise a raw ``api_mode_map`` mapping.

    Args:
        raw: The value of ``model.api_mode_map`` as loaded from YAML.
        config_path: Path of the config file, used in error messages.
        legacy_api_mode: The value of the scalar ``model.api_mode``, if set.
            Passing a non-empty value here is an error — see module docstring.

    Returns:
        A new dict with stripped model ids mapped to validated mode strings.

    Raises:
        ApiModeConfigError: On any missing, empty, duplicate or invalid entry.
    """
    where = _config_hint(config_path)

    if isinstance(legacy_api_mode, str) and legacy_api_mode.strip():
        raise ApiModeConfigError(
            f"Both 'model.api_mode' and 'model.api_mode_map' are set{where}. "
            f"These are two sources of truth for the same decision. "
            f"Remove 'model.api_mode' and declare the mode per model in "
            f"'model.api_mode_map' instead."
        )

    if raw is None:
        raise ApiModeConfigError(
            f"'model.api_mode_map' is required but missing{where}. "
            f"Declare the API mode for every model you use, e.g.:\n"
            f"  model:\n"
            f"    api_mode_map:\n"
            f"      claude-opus-5: anthropic_messages\n"
            f"      gpt-5.6-sol: chat_completions\n"
            f"Valid modes: {_VALID_LIST}"
        )

    if not isinstance(raw, Mapping):
        raise ApiModeConfigError(
            f"'model.api_mode_map' must be a mapping of model id -> api mode{where}, "
            f"got {type(raw).__name__}. Valid modes: {_VALID_LIST}"
        )

    if not raw:
        raise ApiModeConfigError(
            f"'model.api_mode_map' is empty{where}. "
            f"Every model must declare an API mode explicitly; there is no "
            f"default. Valid modes: {_VALID_LIST}"
        )

    normalized: Dict[str, str] = {}
    for model_id, mode in raw.items():
        if not isinstance(model_id, str) or not model_id.strip():
            raise ApiModeConfigError(
                f"'model.api_mode_map' contains an empty or non-string model "
                f"id{where}: {model_id!r}"
            )
        key = model_id.strip()

        if mode is None:
            raise ApiModeConfigError(
                f"'model.api_mode_map[{key}]' is null{where}. "
                f"An explicit mode is required. Valid modes: {_VALID_LIST}"
            )
        if not isinstance(mode, str) or not mode.strip():
            raise ApiModeConfigError(
                f"'model.api_mode_map[{key}]' must be a non-empty string{where}, "
                f"got {mode!r}. Valid modes: {_VALID_LIST}"
            )

        value = mode.strip()
        if value not in _VALID_SET:
            raise ApiModeConfigError(
                f"'model.api_mode_map[{key}]' has unknown api mode {value!r}{where}. "
                f"Valid modes: {_VALID_LIST}"
            )

        if key in normalized and normalized[key] != value:
            raise ApiModeConfigError(
                f"'model.api_mode_map' declares conflicting modes for {key!r}{where}: "
                f"{normalized[key]!r} and {value!r}"
            )
        normalized[key] = value

    return normalized


def resolve_api_mode(
    model: Optional[str],
    api_mode_map: Mapping[str, str],
    *,
    config_path: Optional[str] = None,
) -> str:
    """Look up the declared API mode for ``model``.

    Args:
        model: The model id being resolved (e.g. ``claude-opus-5``).
        api_mode_map: A mapping already validated by
            :func:`normalize_api_mode_map`.
        config_path: Path of the config file, used in error messages.

    Returns:
        The declared api mode string.

    Raises:
        ApiModeConfigError: If ``model`` is empty or has no declared entry.
            Unknown models fail loudly rather than defaulting, so adding a
            model to the upstream gateway cannot silently regress tool
            calling for that model.
    """
    where = _config_hint(config_path)

    if not isinstance(model, str) or not model.strip():
        raise ApiModeConfigError(
            f"Cannot resolve an API mode without a model id{where}."
        )
    key = model.strip()

    if key not in api_mode_map:
        known = ", ".join(sorted(api_mode_map)) or "(none)"
        raise ApiModeConfigError(
            f"Model {key!r} has no entry in 'model.api_mode_map'{where}. "
            f"Every model must declare its API mode explicitly — there is no "
            f"default, because an incorrect mode silently degrades tool "
            f"calling instead of erroring.\n"
            f"Add:\n"
            f"  model:\n"
            f"    api_mode_map:\n"
            f"      {key}: <mode>\n"
            f"Valid modes: {_VALID_LIST}\n"
            f"Currently declared: {known}"
        )

    return api_mode_map[key]
