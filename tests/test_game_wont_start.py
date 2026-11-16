"""A game that refuses to start (the plugin answers the session start 412: Unreal's blueprints with compile errors)
ends the run at once, and the result tells the user why (cli.GameWontStart)."""

import types

import pytest

from autopilot import cli, codex
from autopilot.bridge import BridgeError

BROKEN = ("The game wasn't started: this blueprint has compile errors, so the game wouldn't play as it says.\n"
          "- BP_Door (/Game/BP_Door.BP_Door)\n    The 'Open' node's Target must have a connection\n"
          "Fix it in the editor (open it and press Compile to see where) and run again.")


class Editor:
    """An editor whose game won't start (412), with its levels; what it was asked and sent kept."""

    def __init__(self, listed=None):
        self.hello = {"project": "Harbor Game", "host": "editor", "share": False}
        self.listed, self.asked, self.sent = listed, [], []

    def request(self, op, args=None, timeout=None):
        args = args or {}
        self.asked.append((op, dict(args)))
        if op == "levels":
            if self.listed is None:
                raise BridgeError(400, "unknown op levels")
            return {"levels": self.listed, "open": self.listed[0], "playing": ""}
        if op == "session" and args.get("action") in ("start", "restart"):
            raise BridgeError(412, BROKEN)
        return {"running": False}

    def status(self, text):
        self.sent.append({"type": "status", "text": text})

    def send(self, frame):
        self.sent.append(frame)

    def post(self, op, args=None):
        self.asked.append((op, dict(args or {})))


@pytest.fixture
def no_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "MEMORY", str(tmp_path / "mem"))
    monkeypatch.setattr(codex, "build", lambda project, path=None, **_: codex.Codex(str(tmp_path)))


def test_prepare_raises_when_the_game_refuses_to_start(no_codex):
    with pytest.raises(cli.GameWontStart) as e:
        cli.prepare(Editor(), None)
    assert str(e.value) == BROKEN
    # (the level asked for too: not played in the editor's own level instead)
    editor = Editor(["/Game/Maps/Arena", "/Game/Maps/Harbor"])
    with pytest.raises(cli.GameWontStart):
        cli.prepare(editor, None, level="Harbor")
    assert [a for op, a in editor.asked if op == "session" and a.get("action") != "status"] == [
        {"action": "start", "map": "/Game/Maps/Harbor"}]


def test_another_refusal_still_plays_the_editors_own_level(no_codex):
    """A level that can't play (404) isn't a game that won't start: the editor's own plays, as before."""
    class NoSuchLevel(Editor):
        def request(self, op, args=None, timeout=None):
            if op == "session" and (args or {}).get("map"):
                self.asked.append((op, dict(args)))
                raise BridgeError(404, "No level named Harbor")
            if op == "session" and (args or {}).get("action") == "start":
                self.asked.append((op, dict(args)))
                return {"running": True}
            return super().request(op, args, timeout)

    assert cli.refused(BridgeError(404, "No level")) is None and cli.refused(ValueError("x")) is None
    _, game = cli.prepare(NoSuchLevel(["/Game/Maps/Arena", "/Game/Maps/Harbor"]), None, level="Harbor")
    assert game.level is None and game.level_info.failed == "No level named Harbor"


def test_the_run_ends_with_why_as_its_result_and_an_alert(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTOPILOT_AURA_URL", raising=False)
    monkeypatch.setattr(cli.codexmod, "MEMORY", str(tmp_path))
    monkeypatch.setattr(cli.agents, "select", lambda name=None: types.SimpleNamespace(
        implement=lambda *a, **k: pytest.fail("a run played a game that wouldn't start")))

    def prepare(link, *a, **k):
        raise cli.GameWontStart(BROKEN)

    monkeypatch.setattr(cli, "prepare", prepare)
    link = Editor()
    assert cli.run_prompt(link, "The door opens with E", kind="verify") == cli.EXIT["INCONCLUSIVE"]
    result = link.sent[-1]["result"]
    assert result["verdict"] == "INCONCLUSIVE" and result["reason"] == BROKEN and result["alerts"] == [BROKEN]
    link = Editor()
    cli.run_prompt(link, "Explore the game for bugs", kind="explore", play_minutes=5)
    result = link.sent[-1]["result"]
    assert result["reached"] is False and result["notes"] == [BROKEN] and result["alerts"] == [BROKEN]
