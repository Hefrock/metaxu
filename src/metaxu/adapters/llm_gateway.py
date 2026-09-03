"""LLM API gateway adapter: assurance from the raw model call.

A gateway sits at the third interception boundary from
``docs/adr/0002-adapter-strategy.md`` — in front of the model API itself,
seeing exactly what the MCP proxy structurally cannot: the prompt, the
model's own answer text, and the *intent* of every tool call the model
proposes. Pairing a gateway partial with an MCP-proxy partial (same
``interaction_id``, see ``examples/llm_gateway/``) and merging them is the
most concrete demonstration of the composition thesis: neither observer
alone sees a whole interaction, and the merge is a re-evaluation over
their union, not a concatenation of their verdicts.

First provider: Anthropic's Messages API (``POST /v1/messages``), scoped
per ADR 0002 to keep per-provider maintenance bounded — read the ADR
before adding a second provider rather than growing this module ad hoc.

Unlike the other adapters there is no persistent per-call session object
to open and close: the Messages API is stateless, so :func:`record_exchange`
takes one ``(request, response)`` pair — the dict passed to
``client.messages.create(**request)`` and the ``Message`` (or an
equivalent mapping) it returned — and builds one complete artifact from
it. Because ``request["messages"]`` carries the *entire* prior turn
history in a stateless API, walking it recovers the full tool-call trace
— both the intent *and*, when the caller echoed a ``tool_result`` back on
a later turn, the result content — for the conversation so far, not just
the latest turn. That result content is exactly what the caller's own
code produced, though: unlike the MCP proxy, the gateway never
independently retrieves anything, so it carries no source-system
identity, hash, or retrieval timestamp for it. That provenance layer is
what merging in an MCP-proxy partial adds.

Call :func:`record_exchange` after the *final* response in a tool-use
loop (``stop_reason`` other than ``"tool_use"``) for one artifact
covering the whole exchange. An intermediate call is still a valid
partial — it will have no answer yet, exactly like an MCP-proxy partial,
until the loop reaches its last turn.

Stdlib-only: this module has no dependency on the ``anthropic`` package.
``request`` is a plain dict; ``response`` is duck-typed (either the SDK's
``Message`` object — attribute access — or an equivalent mapping).
"""

from __future__ import annotations

from typing import Any

from ..artifact import AssuranceArtifact
from ..events import utcnow
from ..policy import PolicyEngine
from ..safety import SafetyEngine
from ..session import AssuranceSession

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Duck-typed field access: SDK object attribute, or dict/mapping key."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text_of(content: Any) -> str:
    """Join the text blocks of a Messages API ``content`` value.

    ``content`` is either a plain string (the simple message form) or a
    list of content blocks; non-text blocks are ignored here.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        _get(block, "text", "") or "" for block in content if _get(block, "type") == "text"
    )


def _tool_result_content(block: Any) -> Any:
    """The content of a ``tool_result`` block, summarized like a tool result."""
    content = _get(block, "content")
    if isinstance(content, list):
        return _text_of(content) or content
    return content


def _reconstruct_tool_history(messages: list[Any]) -> list[dict[str, Any]]:
    """Tool calls (intent + result, when known) from message history, in
    the order the model issued them.

    An assistant ``tool_use`` block is the intent; a later user
    ``tool_result`` block (matched by ``tool_use_id``) is the outcome the
    caller echoed back. A call with no matching result yet is still
    pending as far as this history shows.
    """
    intents: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    results: dict[str, dict[str, Any]] = {}

    for message in messages or []:
        role = _get(message, "role")
        content = _get(message, "content")
        for block in content if isinstance(content, list) else []:
            btype = _get(block, "type")
            if role == "assistant" and btype == "tool_use":
                tool_id = _get(block, "id")
                if not tool_id:
                    continue
                if tool_id not in intents:
                    order.append(tool_id)
                intents[tool_id] = {
                    "name": _get(block, "name", "unknown"),
                    "arguments": _get(block, "input") or {},
                }
            elif role == "user" and btype == "tool_result":
                tool_id = _get(block, "tool_use_id")
                if not tool_id:
                    continue
                results[tool_id] = {
                    "content": _tool_result_content(block),
                    "is_error": bool(_get(block, "is_error", False)),
                }

    calls = []
    for tool_id in order:
        intent = intents[tool_id]
        outcome = results.get(tool_id)
        calls.append(
            {
                "name": intent["name"],
                "arguments": intent["arguments"],
                "result": outcome["content"] if outcome else None,
                "error": (
                    f"tool error: {outcome['content']}"
                    if outcome and outcome["is_error"]
                    else None
                ),
            }
        )
    return calls


def record_exchange(
    request: dict[str, Any],
    response: Any,
    policy_engine: PolicyEngine | None = None,
    safety_engine: SafetyEngine | None = None,
    interaction_id: str | None = None,
    prompt_version: str | None = None,
    tag_map: dict[str, list[str]] | None = None,
    provider: str = "anthropic",
) -> AssuranceArtifact:
    """Build one assurance artifact from a Messages API request/response.

    ``tag_map`` maps tool names to policy tags — the same convention the
    MCP proxy and CDS Hooks adapters use, so institutional policies match
    a gateway partial's tool calls the same way they match every other
    observer's.
    """
    tag_map = tag_map or {}
    messages = request.get("messages") or []

    # The most recent user-role message is often a tool_result envelope
    # with no text of its own (a tool-use loop's follow-up turn) — keep
    # walking back to the actual question rather than stopping there.
    question = "(no user message)"
    for message in reversed(messages):
        if _get(message, "role") != "user":
            continue
        text = _text_of(_get(message, "content"))
        if text:
            question = text
            break

    session = AssuranceSession(
        question=question,
        policy_engine=policy_engine,
        safety_engine=safety_engine,
        interaction_id=interaction_id,
        observer=f"llm-gateway:{provider}",
        metadata={
            "dev.metaxu/observer": f"llm-gateway:{provider}",
            "dev.metaxu/started_at": utcnow(),
        },
    )
    session.set_model(
        _get(response, "model") or request.get("model") or "unknown",
        prompt_version=prompt_version,
    )
    if request.get("system"):
        session.metadata["dev.metaxu/system_prompt_present"] = True

    # Tool trace reconstructed from the whole stateless history: intent
    # from an earlier assistant turn, result from a later tool_result the
    # caller echoed back — the one thing a single-turn view couldn't show.
    for call in _reconstruct_tool_history(messages):
        session.record_tool_call(
            name=call["name"],
            arguments=call["arguments"],
            result=call["result"],
            error=call["error"],
            tags=tag_map.get(call["name"], []),
        )

    # This turn's response: new tool-call intents (no result yet — the
    # caller hasn't executed them) and/or the answer.
    answer_parts: list[str] = []
    for block in _get(response, "content") or []:
        btype = _get(block, "type")
        if btype == "text":
            text = _get(block, "text", "") or ""
            if text:
                answer_parts.append(text)
        elif btype == "tool_use":
            name = _get(block, "name", "unknown")
            session.record_tool_call(
                name=name,
                arguments=_get(block, "input") or {},
                tags=tag_map.get(name, []),
            )
        # thinking / redacted_thinking blocks: internal reasoning, not
        # recorded as structured artifact content.

    stop_reason = _get(response, "stop_reason")
    if stop_reason == "refusal":
        details = _get(response, "stop_details")
        session.record_note(
            "model declined to respond",
            tags=["refusal"],
            data={
                "category": _get(details, "category") if details else None,
                "explanation": _get(details, "explanation") if details else None,
            },
        )
    elif answer_parts:
        session.set_answer("".join(answer_parts))
    # else: a mid-loop turn (stop_reason == "tool_use") or an empty final
    # turn — no answer recorded. check_missing_answer flags it if this is
    # the artifact's final state, exactly as intended.

    usage = _get(response, "usage")
    if usage is not None:
        token_usage = {
            field: value
            for field in _USAGE_FIELDS
            if (value := _get(usage, field)) is not None
        }
        if token_usage:
            session.reproducibility["token_usage"] = token_usage
    if stop_reason:
        session.reproducibility["stop_reason"] = stop_reason

    return session.finalize()
