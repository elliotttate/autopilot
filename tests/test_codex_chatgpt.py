"""The ChatGPT login path is optional and independent of API credentials."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autopilot import cli, codex_login
from autopilot.agents import codex, session


def test_login_turn_sends_only_explicit_tools_and_never_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-decision-key")
    monkeypatch.setattr(codex_login, "executable", lambda: "codex")
    calls = []

    class Process:
        returncode = 0

        def __init__(self, cmd, **kwargs):
            calls.append(cmd)
            assert "--ignore-user-config" in cmd and "--ephemeral" in cmd
            assert "OPENAI_API_KEY" not in kwargs["env"]
            assert "TYPESAFE_API_KEY" not in kwargs["env"]
            assert kwargs["cwd"] != str(tmp_path)
            assert json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text())["type"] == "object"
            assert Path(cmd[cmd.index("--image") + 1]).read_bytes() == b"fake-frame"
            self.final = Path(cmd[cmd.index("--output-last-message") + 1])

        def communicate(self, data, timeout):
            assert data == b"Which route?"
            self.final.write_text('{"route":1}')

        def poll(self):
            return self.returncode

    monkeypatch.setattr(codex_login.subprocess, "Popen", Process)
    text = codex_login.ask("Which route?", schema={"type": "object"}, images=[b"fake-frame"])
    assert json.loads(text)["route"] == 1 and len(calls) == 1
    assert not Path(calls[0][calls[0].index("--output-schema") + 1]).exists()


def test_access_gate_and_replay_use_the_selected_provider(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_AURA_URL", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("AUTOPILOT_DECISION_PROVIDER", "codex")
    monkeypatch.setattr(codex_login, "unavailable", lambda: None)
    monkeypatch.setattr(cli.runtime, "jev_client", lambda: "signed-in client")
    assert cli.decision_key_missing() is None
    with cli.replay_decisions(None, "project", True) as client:
        assert client == "signed-in client"
    monkeypatch.setattr(codex_login, "unavailable", lambda: "Sign in to Codex.")
    assert cli.decision_key_missing() == "Sign in to Codex."
    with pytest.raises(cli.NoDecisions, match="Sign in to Codex"):
        with cli.replay_decisions(None, "project", True):
            pass
    assert cli.describer_model(None, "anthropic") == ("codex", None)


def test_coding_turn_gets_only_this_runs_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(codex.engines, "of", lambda game: "unity")
    monkeypatch.setattr(codex, "system_text", lambda engine, run: f"Play {run}")
    calls = []

    def ask(message, **kwargs):
        config = kwargs["config"]
        assert config["developer_instructions"] == "Play runs/demo"
        assert config["mcp_servers.autopilot"]["args"][-1] == "run-tools.json"
        assert config["features.shell_tool"] is False
        assert kwargs["model"] is None and kwargs["cwd"] == tmp_path
        calls.append(message)
        return "Saw the shelf."

    monkeypatch.setattr(codex_login, "ask", ask)
    play = SimpleNamespace(game=object(), run_rel="runs/demo", budget=60, stop=lambda: False,
                           next_message=lambda: None, note=lambda message: None, said=lambda message: None)
    events = codex.Events(play, say=lambda message: None)
    assert codex.chatgpt_turns(play, "run-tools.json", tmp_path, "Inspect the aisle", events, None) is None
    assert calls == ["Inspect the aisle"] and events.last_text == "Saw the shelf."


def test_login_route_uses_cli_default_without_model_override(monkeypatch):
    for key in ("AUTOPILOT_CODEX_MODEL", "AUTOPILOT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    assert session.model("codex") == "Codex CLI default"
