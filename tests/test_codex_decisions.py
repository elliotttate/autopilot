import json
import threading
import time
from types import SimpleNamespace

import pytest
from test_autopilot import FakeLink
from typesafe_sdk import Choice, Image, Noul, Score

from autopilot import codex_decisions, codex_login, regress, runtime, sandbox, telemetry, watch
from autopilot.kit import Harness, Option, Status
from autopilot.kit.game import Game
from autopilot.vision import Frames


def questions():
    return {"pick": Choice(criteria={"left": "clear aisle", "forward": "solid shelf"}),
            "stuck": Noul(instructions="Blocked ahead?"), "motion": Score(criteria=["still", "walking", "running"])}


def test_codex_factory_uses_session_login_and_answers_typed_questions(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_DECISION_PROVIDER", "codex")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    seen = {}

    def ask(prompt, **kwargs):
        seen.update(kwargs)
        assert '"solid shelf"' in prompt
        return '{"pick":{"left":0.95,"forward":0.05},"stuck":0.9,"motion":1.5}'

    monkeypatch.setattr(codex_login, "ask", ask)
    client = telemetry.CountingJev(runtime.jev_client(), telemetry.Usage())
    assert client.decision_provider == "codex"
    r = client.system_one({"wall": "shelf"}, questions(), timeout=30, retry=runtime.RetryPolicy())
    assert r.choices["pick"].choice == "left" and r.choices["pick"].confidence == 0.95
    assert r.nouls["stuck"].noul == 0.9 and r.scores["motion"].score == 1.5
    assert r.raw["model"] == "codex" and seen["timeout"] == 30
    assert set(seen["schema"]["required"]) == set(questions())
    assert not seen["schema"]["properties"]["pick"]["additionalProperties"]
    client.close()
    assert seen["stop"]()


@pytest.mark.parametrize("changed", [
    '{"pick":{"teleport":1},"stuck":0.9,"motion":1}',
    '{"pick":{"left":0.8,"forward":0.1},"stuck":0.9,"motion":1}',
    '{"pick":{"left":1,"forward":0},"stuck":2,"motion":1}',
    '{"pick":{"left":1,"forward":0},"stuck":true,"motion":1}',
    '{"pick":{"left":1,"forward":0},"stuck":0.2,"motion":3}',
    '{"pick":{"left":1,"forward":0},"stuck":0.2,"motion":NaN}',
    '{"pick":{"left":1,"forward":0},"stuck":0.2,"motion":0,"shell":"anything"}',
    '{"pick":{"left":1,"forward":0},"stuck":0.2}',
    '{"pick":{"left":1,"forward":0},"stuck":0.2,"stuck":0,"motion":1}',
])
def test_invalid_or_unoffered_answers_are_rejected(monkeypatch, changed):
    monkeypatch.setattr(codex_login, "ask", lambda *a, **kw: changed)
    with pytest.raises(ValueError):
        codex_decisions.CodexDecisions().system_one({}, questions())


def test_watcher_attaches_actual_frames_and_reports_codex(monkeypatch):
    frames = Frames()
    now = time.time()
    frames.add(b"first-jpeg", 1.0, now - 1)
    frames.add(b"next-jpeg", 2.0, now)

    def ask(prompt, *, images, **kwargs):
        assert images == [b"first-jpeg", b"next-jpeg", b"rubric-jpeg"]
        data = json.loads(prompt.split("\n", 1)[1])
        assert data["state"]["frames"][1] == {"game_time": 2, "picture": {"attached_image": 2}}
        assert data["questions"]["gate"]["instructions"] == {"attached_image": 3}
        return '{"gate":0.8}'

    monkeypatch.setattr(codex_login, "ask", ask)
    looker = watch.Looker(frames, jev=codex_decisions.CodexDecisions())
    answer = looker.look(now - 1, now, {"gate": Noul(instructions=Image(b"rubric-jpeg"))}, rate=5)
    assert answer["by"] == "codex" and answer["gate"] == 0.8 and looker.calls["codex"] == 1


class Shelf(Harness):
    goal = "Take the open aisle"

    def __init__(self):
        self.picked = None

    def options(self, obs):
        return [Option(name, name, lambda game, obs, n=name: setattr(self, "picked", n))
                for name in ("left", "forward")]

    def status(self, obs):
        return Status("passed" if self.picked == "left" else "running")


def test_live_runtime_selects_only_an_offered_option_and_recorded_replay_stays_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_DECISION_PROVIDER", "codex")
    calls = []

    def ask(prompt, **kwargs):
        calls.append(prompt)
        return '{"pick":{"left":1,"forward":0}}'

    monkeypatch.setattr(codex_login, "ask", ask)
    for i, client in enumerate((runtime.jev_client(), regress.ReplayJev(["left"]))):
        rt = runtime.Runtime(Game(FakeLink()), Shelf(), str(tmp_path / f"trace{i}.jsonl"), client=client, max_seconds=2)
        assert rt.run()["status"]["state"] == "passed"
        assert rt.trace.events[0]["decision_provider"] == ("codex" if i == 0 else "recorded")
    assert len(calls) == 1


def test_slow_answer_drops_when_offered_options_changed_and_stops_at_episode_end(tmp_path, monkeypatch):
    started, ended = threading.Event(), threading.Event()

    def ask(prompt, *, stop, **kwargs):
        started.set()
        while not stop():
            time.sleep(0.005)
        ended.set()
        raise InterruptedError()

    monkeypatch.setattr(codex_login, "ask", ask)
    h = Shelf()
    rt = runtime.Runtime(Game(FakeLink()), h, str(tmp_path / "t.jsonl"), client=codex_decisions.CodexDecisions(),
                         max_seconds=0.45)
    rt.apply({"left": 0.95, "forward": 0.05}, {}, 5, h.options({})[1:], {}, 0, {"state": {}})
    assert rt.trace.events[-1]["action"] == "dropped" and h.picked is None
    rt.run()
    assert started.is_set() and ended.wait(1) and h.picked is None


def test_pending_codex_decision_outlives_jev_deadline_and_keeps_the_original_frame(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    seen = {}

    def ask(prompt, *, images, **kwargs):
        started.set()
        assert release.wait(2)
        seen.update(images=images, payload=json.loads(prompt.split("\n", 1)[1]))
        return '{"pick":{"left":1,"forward":0}}'

    monkeypatch.setattr(codex_login, "ask", ask)
    rt = runtime.Runtime(Game(FakeLink()), Shelf(), str(tmp_path / "t.jsonl"), client=codex_decisions.CodexDecisions())
    pixels = bytearray(b"old")
    obs = {"at": [1, 2], "view": Image(pixels)}
    try:
        rt.ask(obs, rt.h.options(obs))
        assert started.wait(1)
        pixels[:] = b"new"
        obs["at"][0] = 99
        future, _, *args = rt.pending[0]
        rt.pending[0] = (future, time.monotonic() - 8, *args)
        assert rt.answers() == [] and len(rt.pending) == 1
        release.set()
        future.result(timeout=2)
        assert rt.answers()[0][0] == {"left": 1, "forward": 0}
        assert seen["images"] == [b"old"] and seen["payload"]["state"]["situation"]["at"] == [1, 2]
    finally:
        release.set()
        rt.pool.shutdown()
        rt.trace.close()


def test_sandbox_forwards_the_same_observation_images_and_cancellation(monkeypatch):
    seen = {}
    parent = sandbox.Sandbox.__new__(sandbox.Sandbox)
    parent.client = codex_decisions.CodexDecisions()
    parent.codex = True
    parent.decision_stops = {1: threading.Event()}
    parent._write = lambda reply: seen.update(reply=reply)

    def ask_model(prompt, **kwargs):
        assert kwargs["images"] == [b"snapshot"] and not kwargs["stop"]()
        parent._cancel_decisions()
        assert kwargs["stop"]()
        return '{"visible":0.9}'

    def ask_host(msg, timeout, stop):
        assert timeout > runtime.JEV_TIMEOUT * 2.5 and stop is None
        parent._jev({**msg, "rid": 1})
        return seen["reply"]

    monkeypatch.setattr(codex_login, "ask", ask_model)
    client = sandbox.RemoteJev(SimpleNamespace(_ask=ask_host), codex=True)
    result = client.system_one({"view": Image(b"snapshot")}, {"visible": Noul(instructions="Gate?")})
    assert result.nouls["visible"].noul == 0.9 and not parent.decision_stops
