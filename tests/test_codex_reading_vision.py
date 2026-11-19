"""Reading tools and recorded frames can use the same signed-in local Codex as play."""

import json

import pytest

from autopilot import codex_login, llm, vision


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_DECISION_PROVIDER", "codex")
    monkeypatch.setenv("AUTOPILOT_VISION_MODEL", "relay-only")
    monkeypatch.setenv("AUTOPILOT_HELPER_MODEL", "relay-only")
    monkeypatch.delenv("AUTOPILOT_CODEX_MODEL", raising=False)
    monkeypatch.delenv("AUTOPILOT_MODEL", raising=False)
    monkeypatch.setattr(codex_login, "unavailable", lambda: None)
    monkeypatch.setattr(vision.requests, "post", lambda *a, **k: pytest.fail("unexpected HTTP model call"))
    return monkeypatch


def test_frames_are_supplied_as_ordered_attachments_with_their_own_questions(local):
    calls = []

    def ask(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return "The character opens the door."

    local.setattr(codex_login, "ask", ask)
    route, error = vision.route("anthropic", agent_model="relay-only", model="another-relay-model")
    assert route[0] == "codex" and error is None
    frames = [{"jpeg": b"frame-a", "t": 12}, {"jpeg": b"frame-b", "t": 14}]
    parts = vision.frame_parts(frames, base=12) + [("text", "What happens to the door?")]
    answer, model, usage = vision.Wire(route, "codex").ask(parts, vision.SYSTEM)
    assert answer == "The character opens the door." and model == "Codex CLI default" and usage is None
    prompt, options = calls.pop()
    assert options["images"] == [b"frame-a", b"frame-b"] and options["model"] is None
    assert prompt.index("Frame 1: time 0.00 s") < prompt.index("[Attached image 1]") < prompt.index("Frame 2:")
    assert prompt.index("Frame 2: time 2.00 s") < prompt.index("[Attached image 2]") < prompt.index("What happens")


def test_reader_and_researcher_continue_using_the_same_local_model(local, tmp_path):
    local.setenv("AUTOPILOT_MODEL", "general-local-model")
    local.setenv("AUTOPILOT_CODEX_MODEL", "chosen-codex-model")
    model = llm.Model("google", agent_model="unused-provider-model")
    calls = []

    def ask(prompt, **kwargs):
        calls.append((prompt, kwargs))
        if kwargs["schema"] is None:
            return "The text describes an opening door."
        if len(calls) == 2:
            return json.dumps({"text": "", "calls": [{"name": "read_file", "arguments_json": '{"path":"town.cs"}'}]})
        return json.dumps({"text": "The door uses a proximity trigger.", "calls": []})

    local.setattr(codex_login, "ask", ask)
    reader = llm.Reader(model, tmp_path, tmp_path / "output")
    (tmp_path / "town.cs").write_text("OnTriggerEnter: open door", encoding="utf-8")
    assert reader.ask("What opens?", about="town.cs") == "The text describes an opening door."
    assert "OnTriggerEnter: open door" in calls[0][0]
    tools = [("read_file", "Read one game file", {"type": "object", "required": ["path"],
                                                 "properties": {"path": {"type": "string"}}})]
    messages = [{"role": "user", "content": "What controls the door?"}]
    first = model.turn(messages, tools=tools)
    call, = first["calls"]
    assert call["name"] == "read_file" and call["args"] == {"path": "town.cs"}
    messages += [first["message"], {"role": "tool", "id": call["id"], "content": "OnTriggerEnter: open door"}]
    second = model.turn(messages, tools=tools)
    assert second["text"] == "The door uses a proximity trigger." and second["calls"] == []
    assert call["id"] in calls[-1][0] and "OnTriggerEnter: open door" in calls[-1][0]
    assert all(kwargs["model"] == "chosen-codex-model" for _, kwargs in calls)
    assert model.tokens == {"chosen-codex-model": {"calls": 3}}
    reader.close()
    assert all(kwargs["stop"].is_set() for _, kwargs in calls)


def test_local_model_errors_propagate_without_relay_fallback(local):
    def refused(*args, **kwargs):
        raise RuntimeError("Codex usage limit reached")

    local.setattr(codex_login, "ask", refused)
    model = llm.Model("codex", agent_model="Codex CLI default")
    with pytest.raises(RuntimeError, match="Codex usage limit"):
        model.ask("What does this do?")
    with pytest.raises(RuntimeError, match="Codex usage limit"):
        vision.Wire(model.route, "codex").ask([("text", "What is visible?")], vision.SYSTEM)
    local.setattr(codex_login, "unavailable", lambda: "Sign in to the Codex CLI")
    assert vision.route("openai") == (None, "Sign in to the Codex CLI")
    assert llm.Model("anthropic").route is None


def test_unlisted_local_tool_call_is_rejected(local):
    local.setattr(codex_login, "ask", lambda *a, **kw: '{"text":"","calls":[{"name":"outside","arguments_json":"{}"}]}')
    model = llm.Model("codex")
    with pytest.raises(ValueError, match="invalid reading tool"):
        model.turn([{"role": "user", "content": "read"}], tools=[("read", "Read", {"type": "object"})])
