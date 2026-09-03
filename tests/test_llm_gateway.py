"""Tests for the LLM API gateway adapter (metaxu.adapters.llm_gateway)."""

from metaxu import PolicyEngine
from metaxu.adapters.llm_gateway import record_exchange


def simple_request(text="Can we start warfarin?", extra_messages=None):
    return {
        "model": "claude-opus-5",
        "system": "You are a careful clinical assistant.",
        "messages": (extra_messages or []) + [{"role": "user", "content": text}],
    }


def text_response(text, model="claude-opus-5", stop_reason="end_turn", usage=None):
    return {
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "usage": usage or {"input_tokens": 120, "output_tokens": 40},
    }


def test_records_question_model_and_answer():
    artifact = record_exchange(simple_request(), text_response("Warfarin appears appropriate."))
    assert artifact.question == "Can we start warfarin?"
    assert artifact.answer == "Warfarin appears appropriate."
    assert artifact.reproducibility["model"] == "claude-opus-5"
    assert artifact.correlation["observer"] == "llm-gateway:anthropic"
    assert artifact.correlation["role"] == "partial"


def test_system_prompt_presence_recorded_not_content():
    artifact = record_exchange(simple_request(), text_response("OK."))
    assert artifact.metadata["dev.metaxu/system_prompt_present"] is True
    assert "dev.metaxu/system_prompt" not in artifact.metadata  # no raw text captured


def test_token_usage_and_stop_reason_captured():
    response = text_response(
        "OK.",
        usage={
            "input_tokens": 500,
            "output_tokens": 80,
            "cache_read_input_tokens": 400,
        },
    )
    artifact = record_exchange(simple_request(), response)
    usage = artifact.reproducibility["token_usage"]
    assert usage == {"input_tokens": 500, "output_tokens": 80, "cache_read_input_tokens": 400}
    assert artifact.reproducibility["stop_reason"] == "end_turn"


def test_new_tool_use_intent_recorded_with_no_result():
    response = {
        "model": "claude-opus-5",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "get_labs",
                "input": {"patient_id": "pat-001"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    artifact = record_exchange(simple_request(), response, tag_map={"get_labs": ["platelet_count"]})
    [call] = artifact.tool_trace
    assert call["name"] == "get_labs"
    assert call["payload"]["arguments"] == {"patient_id": "pat-001"}
    assert call["payload"]["result_summary"] is None
    assert call["tags"] == ["platelet_count"]
    # Mid-loop turn: no answer yet.
    assert artifact.answer is None


def test_tool_history_reconstructed_with_intent_and_result():
    """The defining capability: request['messages'] carries the whole prior
    turn history in a stateless API, so a later call can recover both the
    tool-call intent (earlier assistant turn) and its result (the
    tool_result the caller echoed back) — not just the latest turn."""
    history = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_labs",
                    "input": {"patient_id": "pat-001"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "Platelets 232 10*3/uL",
                }
            ],
        },
    ]
    request = simple_request(extra_messages=history)
    response = text_response("Anticoagulation appears appropriate.")
    artifact = record_exchange(request, response, tag_map={"get_labs": ["platelet_count"]})

    [call] = artifact.tool_trace
    assert call["name"] == "get_labs"
    assert call["payload"]["result_summary"] == "Platelets 232 10*3/uL"
    assert call["payload"]["error"] is None
    assert artifact.answer == "Anticoagulation appears appropriate."


def test_tool_result_error_recorded():
    history = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "get_labs", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "connection refused",
                    "is_error": True,
                }
            ],
        },
    ]
    request = simple_request(extra_messages=history)
    artifact = record_exchange(request, text_response("Retrying."))
    [call] = artifact.tool_trace
    assert "connection refused" in call["payload"]["error"]


def test_refusal_recorded_as_note_and_no_answer():
    response = {
        "model": "claude-opus-5",
        "content": [],
        "stop_reason": "refusal",
        "stop_details": {"category": "bio", "explanation": "policy"},
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    artifact = record_exchange(simple_request(), response)
    assert artifact.answer is None
    notes = [e for e in artifact.events if e.type.value == "note"]
    [refusal_note] = notes
    assert refusal_note.payload["data"] == {"category": "bio", "explanation": "policy"}
    critical_or_warning = {f["check"] for f in artifact.safety_checks}
    assert "missing_answer" in critical_or_warning


def test_multiple_text_blocks_joined():
    response = {
        "model": "claude-opus-5",
        "content": [
            {"type": "text", "text": "Part one. "},
            {"type": "text", "text": "Part two."},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    artifact = record_exchange(simple_request(), response)
    assert artifact.answer == "Part one. Part two."


def test_thinking_blocks_not_recorded_as_content():
    response = {
        "model": "claude-opus-5",
        "content": [
            {"type": "thinking", "thinking": "internal reasoning that must not leak"},
            {"type": "text", "text": "Final answer."},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    artifact = record_exchange(simple_request(), response)
    assert artifact.answer == "Final answer."
    assert "internal reasoning" not in str(artifact.to_dict())


def test_question_extraction_skips_tool_result_only_user_turns():
    """Regression: the most recent user-role message in a tool-use loop is
    often a tool_result envelope with no text of its own. Extraction must
    keep walking back to the real question instead of stopping there and
    falling back to '(no user message)'."""
    request = {
        "model": "claude-opus-5",
        "messages": [
            {"role": "user", "content": "Can we start warfarin?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "get_labs", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "232"}
                ],
            },
        ],
    }
    artifact = record_exchange(request, text_response("Warfarin appears appropriate."))
    assert artifact.question == "Can we start warfarin?"


def test_no_user_message_falls_back():
    request = {"model": "claude-opus-5", "messages": []}
    artifact = record_exchange(request, text_response("OK."))
    assert artifact.question == "(no user message)"


def test_interaction_id_and_policy_engine_wired_through():
    engine = PolicyEngine.from_document(
        {
            "policies": [
                {
                    "name": "grounding",
                    "trigger": {"always": True},
                    "requires": ["patient_record_access"],
                }
            ]
        }
    )
    artifact = record_exchange(
        simple_request(),
        text_response("OK."),
        policy_engine=engine,
        interaction_id="ixn-fixed",
    )
    assert artifact.correlation["interaction_id"] == "ixn-fixed"
    [result] = artifact.policy_checks
    assert result["passed"] is False
    assert result["missing"] == ["patient_record_access"]


def test_response_as_sdk_like_object_via_attribute_access():
    """Duck-typed access must also work with attribute-style objects, not
    just plain dicts — the SDK's Message/ContentBlock objects, not just
    the wire-format dicts these tests otherwise use."""

    class Block:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.type = kw["type"]

    class Usage:
        input_tokens = 5
        output_tokens = 5
        cache_read_input_tokens = None
        cache_creation_input_tokens = None

    class Response:
        model = "claude-opus-5"
        content = [Block(type="text", text="Attribute-style answer.")]
        stop_reason = "end_turn"
        usage = Usage()
        stop_details = None

    artifact = record_exchange(simple_request(), Response())
    assert artifact.answer == "Attribute-style answer."
    assert artifact.reproducibility["token_usage"] == {"input_tokens": 5, "output_tokens": 5}
