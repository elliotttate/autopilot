"""Regression replay (autopilot/regress.py) on tests/simharness.py's tiny game: a CONFIRMED verification's
harness is kept under regressions/ and plays again with no agent; a change to lib.py that breaks it shows."""

import json
import os
import types

import pytest
import simharness as sim

from autopilot import cli, regress


@pytest.fixture(autouse=True)
def results_here(tmp_path, monkeypatch):
    """A replay's results and traces go to a folder of the test's, never this machine's settings folder."""
    monkeypatch.setattr(regress, "results_dir", lambda: str(tmp_path / "results"))


@pytest.fixture
def memory(tmp_path):
    mem = str(tmp_path / "memory" / "Tiny")
    sim.write_memory(mem, lib=sim.LIB.replace("< 10", "< 60"))
    return mem


def confirmed(tmp_path, memory, which="coin", rid="run-1"):
    run_dir = str(tmp_path / "runs" / rid)
    result = sim.play(run_dir, which, memory=memory)
    assert result["status"]["state"] == "done", result
    return run_dir, {"outcome": "CONFIRMED", "run": rid, "prompt": "Verify that walking to the coin brings the "
                     "player next to it", "summary": "it does", "evidence": ["28 cm from the coin"]}


def test_a_confirmed_claim_is_kept_and_replays_without_a_model(tmp_path, memory):
    run_dir, report = confirmed(tmp_path, memory)
    reg = regress.save(run_dir, report, "Tiny", project_root=memory)
    assert reg == os.path.join(memory, "regressions", "run-1")
    assert sorted(os.listdir(reg)) == ["harness.py", "lib.py", "regression.json"]
    spec = regress.load(reg)
    assert spec["expect"]["state"] == "done" and spec["expect"]["evidence"]["near_coin"] is True
    assert spec["picks"] and set(spec["picks"]) <= {"go_to_coin", "look_around"} and spec["episode"] == 1
    assert regress.listing(os.path.join(memory, "regressions")) == [reg]

    game, world, _ = sim.connect()
    try:
        r = regress.replay(game, reg, project_root=memory)
    finally:
        world.close()
    assert r["passed"], r
    assert r["why"].startswith("ended done") and r["evidence"]["near_coin"] is True and r["decisions"] >= 1
    assert os.path.exists(os.path.join(reg, "replay.jsonl"))
    # (no language model, no Jev: the replay's decisions are the recording's)
    assert all(c[0] != "jev" for c in world.calls)


def test_a_lib_change_that_breaks_it_fails_the_replay(tmp_path, memory):
    run_dir, report = confirmed(tmp_path, memory)
    reg = regress.save(run_dir, report, "Tiny", project_root=memory)
    sim.write_memory(memory)  # (lib.arrived back to `< 10`: the player never "arrives" 28 cm away)
    sim.fresh_imports(memory)
    game, world, _ = sim.connect()
    try:
        r = regress.replay(game, reg, project_root=memory, seconds=2)
        assert not r["passed"] and r["why"].startswith("ended running"), r
        # the snapshot saved with it still passes: the change is in lib.py
        r = regress.replay(game, reg, project_root=memory, frozen_lib=True)
        assert r["passed"], r
    finally:
        world.close()


def test_only_confirmed_claims_from_a_fresh_level_are_kept(tmp_path, memory):
    run_dir, report = confirmed(tmp_path, memory)
    assert regress.save(run_dir, dict(report, outcome="DENIED"), "Tiny", project_root=memory) is None
    assert regress.save(run_dir, dict(report, outcome="achieved"), "Tiny", project_root=memory) is None
    # an episode that went on from where play was can't be replayed from a restart
    with open(os.path.join(run_dir, "trace1.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "continued", "t": 0}) + "\n")
    assert regress.save(run_dir, report, "Tiny", project_root=memory) is None
    # an episode that didn't end done isn't one either
    other = str(tmp_path / "runs" / "run-2")
    sim.write_memory(memory)
    sim.play(other, "coin", memory=memory, max_seconds=1.5)
    assert regress.save(other, dict(report, run="run-2"), "Tiny", project_root=memory) is None


def test_the_session_keeps_its_confirmed_claim(tmp_path, memory, monkeypatch):
    run_dir, report = confirmed(tmp_path, memory)
    notes = []
    session = types.SimpleNamespace(run_dir=run_dir, project="Tiny", root=memory, note=notes.append)
    kept = regress.save_from_session(session, report)
    assert kept and os.path.exists(os.path.join(kept, "regression.json"))
    # AUTOPILOT_REGRESSIONS moves them all
    monkeypatch.setenv("AUTOPILOT_REGRESSIONS", str(tmp_path / "regs"))
    kept = regress.save_from_session(session, report)
    assert kept == str(tmp_path / "regs" / "Tiny" / "run-1")
    # (never raises)
    broken = types.SimpleNamespace(run_dir=str(tmp_path / "nowhere"), project="Tiny", root=memory,
                                   note=notes.append)
    assert regress.save_from_session(broken, report) is None and "couldn't save the regression" in notes[-1]


def test_session_finish_saves_the_regression(tmp_path, memory, monkeypatch):
    from autopilot.agents import session as sessionmod

    run_dir, report = confirmed(tmp_path, memory)
    seen = []
    monkeypatch.setattr(regress, "save_from_session", lambda s, r: seen.append(r["outcome"]) or "kept")
    fake = types.SimpleNamespace(
        report=dict(report), run_dir=run_dir, run_id="run-1", project="Tiny", prompt=report["prompt"],
        episodes=1, tool_seconds=0.0, game=types.SimpleNamespace(writes=[]), alerts=[], researcher=None,
        reader=None, findings=[], index_was_running=False, root=memory, out_of_time=lambda: False,
        indexer=lambda: None, note=lambda text: None, reported_at_time_up=False)
    out = sessionmod.finish(fake, 0.0, None, "")
    assert out["outcome"] == "CONFIRMED" and seen == ["CONFIRMED"]
    fake.report = dict(report, outcome="DENIED")
    sessionmod.finish(fake, 0.0, None, "")
    assert seen == ["CONFIRMED"]


def test_regress_decides_live_with_the_decision_model_by_default(tmp_path, memory, monkeypatch, capsys):
    """By default a replay's decisions are Jev's, live, as the episode's were (the harness is code; what to do next
    comes from what the game shows now), with no agent: on the user's own TypeSafe key, or inside a run with Aura.
    With neither it says how to get one (or --recorded) and replays nothing."""
    run_dir, report = confirmed(tmp_path, memory)
    regress.save(run_dir, report, "Tiny", project_root=memory)
    monkeypatch.setattr(cli.codexmod, "project_dir", lambda project: memory)
    monkeypatch.setattr(cli.aura, "enabled", lambda: False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    args = cli.parser().parse_args(["regress", "--events", "--out", str(tmp_path / "out")])
    assert cli.regress_on(None, "Tiny", args) == 1
    said = capsys.readouterr().out
    assert "needs Autopilot's decision model" in said and "--recorded" in said
    assert any(json.loads(line).get("event") == "error" for line in said.splitlines() if line.startswith("{"))

    # with a key: Jev's client decides (a stand-in here, deciding as the recording did)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    spec = regress.load(os.path.join(memory, "regressions", "run-1"))
    clients = []
    monkeypatch.setattr(cli.runtime, "jev_client", lambda: clients.append(regress.ReplayJev(spec["picks"])) or
                        clients[-1])
    game, world, _ = sim.connect()
    try:
        assert cli.regress_on(game, "Tiny", args) == 0
    finally:
        world.close()
    out = capsys.readouterr().out
    assert len(clients) == 1 and clients[0].calls >= 1 and "the decision model deciding in play" in out
    result = [json.loads(line) for line in out.splitlines() if line.startswith('{"event": "result"')][0]
    assert result["decided"] == "live" and result["passed"] and result["tries"] == 1

    # through Aura: the client is made inside the run (its relay carries the key), and a refusal is told
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setattr(cli.aura, "enabled", lambda: True)
    inside = []

    class Run:
        def __enter__(self):
            inside.append("in")

        def __exit__(self, *exc):
            inside.append("out")

    monkeypatch.setattr(cli, "start_aura_run", lambda link, kind, project, **kw: inside.append(kind) or Run())
    with cli.replay_decisions(None, "Tiny", True) as client:
        assert client is clients[-1] and inside == ["verify", "in"]
    assert inside[-1] == "out"

    def refused(*a, **kw):
        raise cli.aura.Refused(401, "Sign in to Aura first: run `autopilot login`, then start the run again.")

    monkeypatch.setattr(cli, "start_aura_run", refused)
    with pytest.raises(cli.NoDecisions, match="autopilot login"):
        with cli.replay_decisions(None, "Tiny", True):
            pass
    with cli.replay_decisions(None, "Tiny", False) as client:
        assert client is None  # (--recorded: no model)


def test_a_replay_may_play_as_long_as_the_recorded_episode_was_allowed(tmp_path, memory):
    """A replay's episode lasts up to what the recorded one was allowed (kept as max_seconds), else 3x the recorded
    one: a lucky recording that was quick doesn't make a replay's limit tight."""
    run_dir, report = confirmed(tmp_path, memory)
    spec = regress.load(regress.save(run_dir, report, "Tiny", project_root=memory))
    assert spec["max_seconds"] == 3.0  # (sim.play's)
    assert regress.episode_limit({"max_seconds": 150, "played_seconds": 37}) == 150
    assert regress.episode_limit({"played_seconds": 37}) == 111  # (saved before max_seconds was kept)
    assert regress.episode_limit({"played_seconds": 2}) == regress.MIN_SECONDS
    assert regress.episode_limit({"max_seconds": 150}, seconds=40) == 40


def test_a_replay_plays_on_through_stalls_and_says_so(tmp_path, memory, monkeypatch):
    """A stall ends a run's episode so the agent revises the plan; a replay has no agent, so it plays on (the runtime's
    stall_ends=False) to the episode's time limit, and its result tells the stalls it played through."""
    run_dir, report = confirmed(tmp_path, memory)
    reg = regress.save(run_dir, report, "Tiny", project_root=memory)
    asked = {}
    stalls = [{"kind": "no progress", "since_t": 96.43, "t": 156.44, "seconds": 60.0}]

    class Played:
        def __init__(self, game, harness, trace, **kw):
            asked.update(kw)

        def run(self):
            return {"reason": "status done", "status": {"state": "done", "note": "all three held", "evidence": {}},
                    "decisions": 3, "stalls_passed": stalls}

    monkeypatch.setattr(regress.runtime, "Runtime", Played)
    game = types.SimpleNamespace(restart=lambda map=None: None, pause=lambda paused: None, release_all=lambda: None)
    r = regress.replay(game, reg, project_root=memory)
    assert asked["stall_ends"] is False
    assert r["passed"] and r["stalls"] == stalls
    assert r["why"] == "ended done: all three held; played on through 1 stall (no progress at t=96.43-156.44 s)"


def test_a_regression_keeps_its_level_and_a_replay_opens_it(tmp_path, memory):
    """A build starts in its default map (often a menu), not the level the editor had open: a regression keeps the
    level its episode played and a replay opens it; one saved before that is replayed in --level."""
    level = types.SimpleNamespace(levels=["/Game/Maps/Arena", "/Game/Maps/Menu"], open="/Game/Maps/Menu",
                                  playing="/Game/Maps/UEDPIE_0_Arena.Arena")
    # the run's level, else the one the game played (as the list names it), else the editor's open one
    assert regress.level_of(types.SimpleNamespace(level="/Game/Maps/Menu", level_info=level)) == "/Game/Maps/Menu"
    assert regress.level_of(types.SimpleNamespace(level=None, level_info=level)) == "/Game/Maps/Arena"
    unlisted = types.SimpleNamespace(levels=[], open="", playing="/Game/Maps/UEDPIE_0_Arena")
    assert regress.level_of(types.SimpleNamespace(level=None, level_info=unlisted)) == "/Game/Maps/Arena"
    assert regress.level_of(types.SimpleNamespace(level=None, level_info=None)) is None
    assert regress.level_of(None) is None

    run_dir, report = confirmed(tmp_path, memory)
    session = types.SimpleNamespace(run_dir=run_dir, project="Tiny", root=memory, note=lambda text: None,
                                    game=types.SimpleNamespace(level=None, level_info=level))
    reg = regress.save_from_session(session, report)
    assert regress.load(reg)["map"] == "/Game/Maps/Arena"

    game, world, _ = sim.connect()
    opened = []
    restart = game.restart
    game.restart = lambda map=None, **kw: opened.append(map) or restart(**kw)
    try:
        assert regress.replay(game, reg, project_root=memory)["passed"]
        old = regress.save(run_dir, report, "Tiny", project_root=memory, dest=str(tmp_path / "old"))
        assert "map" not in regress.load(old)
        regress.replay_all(game, [old], project_root=memory, say=None, level="/Game/Maps/Other")
        regress.replay(game, old, project_root=memory)
    finally:
        world.close()
    assert opened == ["/Game/Maps/Arena", "/Game/Maps/Other", None]
    # the harness's own start_map wins over both
    assert regress.start_level(types.SimpleNamespace(start_map="/Game/S"), {"map": "/Game/M"}, "/Game/L") == "/Game/S"
    assert cli.parser().parse_args(["regress", "--level", "/Game/Maps/Arena"]).level == "/Game/Maps/Arena"


def test_what_changed_is_listed_never_compared():
    """A replay passes on the harness's done (the claim); what else its evidence shows is listed when it changed
    (booleans, strings, None; numbers beyond the tolerance, relative, at least of 1; lists whose length changed; keys
    gone), never a failure."""
    d = regress.differences
    assert d({"a": True, "n": 10, "s": "x", "m": 0}, {"a": True, "n": 11, "s": "x", "m": 0.2, "extra": 1}) == []
    assert d({"a": True}, {"a": False}) == ["a: True then, False now"]
    assert d({"s": "Held by A"}, {"s": "Neutral"}) == ["s: 'Held by A' then, 'Neutral' now"]
    assert d({"a": True}, {"a": 1}) == ["a: True then, 1 now"]  # (a boolean is a boolean)
    assert d({"k": None}, {"k": 3}) == ["k: None then, 3 now"] and d({"k": True}, {}) == ["k: missing now"]
    assert d({"ammo": 8, "r": {"used": 22, "ok": True}, "log": [1, 2]},
             {"ammo": 20, "r": {"used": 10, "ok": True}, "log": [1]}) == [
        "ammo: 8 then, 20 now", "r.used: 22 then, 10 now", "log: 2 then, 1 now"]


def test_objects_spawned_in_play_are_told_by_their_class():
    """An object the game spawns as it runs is numbered as it is created, from another counter in a packaged build
    than in the editor (BP_Rifle_C_2 then, BP_Rifle_C_2147469854 now): the same rifle. What changed shows Blueprint
    object names by class; other names that end in a number (a level, a placed object) stay as they are."""
    assert regress.by_class("BP_Rifle_C_2147469854") == "BP_Rifle_C"
    assert regress.by_class("/Game/Maps/Arena.Arena:PersistentLevel.BP_Rifle_C_2") == \
        "/Game/Maps/Arena.Arena:PersistentLevel.BP_Rifle_C"
    assert regress.by_class("Lvl_2") == "Lvl_2" and regress.by_class("BP_Gate_A") == "BP_Gate_A"
    assert regress.by_class(3) == 3
    assert regress.differences({"weapon": "BP_Rifle_C_2"}, {"weapon": "BP_Rifle_C_2147469854"}) == []
    assert regress.differences({"weapon": "BP_Rifle_C_2"}, {"weapon": "BP_Pistol_C_7"}) == [
        "weapon: 'BP_Rifle_C' then, 'BP_Pistol_C' now"]
    assert regress.differences({"level": "Lvl_2"}, {"level": "Lvl_3"}) == ["level: 'Lvl_2' then, 'Lvl_3' now"]


def test_replay_jev_follows_the_recording():
    from typesafe_sdk import Choice, Noul

    jev = regress.ReplayJev(["b", "a", "a"], [{"danger": 0.9}, {}, {}])
    q = {"pick": Choice(instructions="x", criteria={"a": "A", "b": "B"}),
         "danger": Noul(instructions="in danger?", criteria={"true": "yes"})}
    r = jev.system_one({}, q)
    assert r.choices["pick"].choice == "b" and r.nouls["danger"].noul == 0.9
    assert jev.system_one({}, {"pick": Choice(instructions="x", criteria={"a": "A", "c": "C"})}).choices[
        "pick"].choice == "a"
    # past the recording: what it picked most
    jev.system_one({}, {"pick": Choice(instructions="x", criteria={"a": "A"})})
    r = jev.system_one({}, {"pick": Choice(instructions="x", criteria={"c": "C", "a": "A"})})
    assert r.choices["pick"].choice == "a"


def test_cli_regress_replays_the_project_regressions(tmp_path, memory, monkeypatch, capsys):
    run_dir, report = confirmed(tmp_path, memory)
    reg = regress.save(run_dir, report, "Tiny", project_root=memory)
    monkeypatch.setattr(cli.codexmod, "project_dir", lambda project: memory)
    args = cli.parser().parse_args(["regress", "--recorded", "--json", str(tmp_path / "out.json")])
    assert args.cmd == "regress" and not args.no_launch
    game, world, _ = sim.connect()
    try:
        assert cli.regress_on(game, "Tiny", args) == 0
    finally:
        world.close()
    out = json.loads((tmp_path / "out.json").read_text())
    assert out["passed"] == out["total"] == 1 and out["failed"] == []
    assert "1/1 passed" in capsys.readouterr().out
    # evidence that differs from the recording's is listed, not a failure: the harness's done is the claim
    spec = regress.load(reg)
    spec["expect"]["evidence"]["near_coin"] = False
    with open(os.path.join(reg, regress.FILE), "w", encoding="utf-8") as f:
        json.dump(spec, f)
    game, world, _ = sim.connect()
    try:
        assert cli.regress_on(game, "Tiny", args) == 0
    finally:
        world.close()
    result = json.loads((tmp_path / "out.json").read_text())["results"][0]
    assert result["passed"] and "near_coin: False then, True now" in result["differs"]
    # a game change that keeps the claim from being shown (the harness never reaches done) fails the command
    sim.write_memory(memory)  # (lib.arrived back to `< 10`: the player never "arrives" 28 cm away)
    sim.fresh_imports(memory)
    args = cli.parser().parse_args(["regress", "--recorded", "--retries", "0", "--seconds", "2", "--json",
                                    str(tmp_path / "out.json")])
    game, world, _ = sim.connect()
    try:
        assert cli.regress_on(game, "Tiny", args) == 1
    finally:
        world.close()
    assert json.loads((tmp_path / "out.json").read_text())["failed"] == ["run-1"]
    # none at all: nothing fails
    args = cli.parser().parse_args(["regress", "--regressions", str(tmp_path / "none")])
    assert cli.regress_on(None, "Tiny", args) == 0


def test_a_project_without_a_headless_game_waits_for_its_plugin(tmp_path, monkeypatch):
    """--project on an engine with no headless game (Roblox Studio's) says so and waits for the plugin, as --no-launch
    does, instead of failing; one that has one is started."""
    from autopilot import engines

    class Studio(engines.get("godot").__class__):
        name = "studio"

        def launch_command(self, project, url, log, map=None, exe=None, headless=True):
            raise NotImplementedError("Studio has no headless game")

    said, started = [], []
    monkeypatch.setattr(engines, "find_project", lambda path: (Studio(), str(tmp_path)))
    args = cli.parser().parse_args(["regress", "--project", str(tmp_path)])
    assert cli.launch_headless(args, "ws://127.0.0.1:9/v1/bridge", say=said.append) is False
    assert "Studio has no headless game. Waiting for the plugin instead" in said[0]
    assert not hasattr(args, "game_process")

    class Headless(Studio):
        def launch_command(self, project, url, log, map=None, exe=None, headless=True):
            return ["game", "--connect", url]

    monkeypatch.setattr(engines, "find_project", lambda path: (Headless(), str(tmp_path)))
    monkeypatch.setattr(cli.threading, "Thread", lambda target, daemon: types.SimpleNamespace(
        start=lambda: started.append(target)))
    assert cli.launch_headless(args, "ws://127.0.0.1:9/v1/bridge", say=said.append) is True and len(started) == 1


def test_the_replays_hold_the_link_as_a_run_does(tmp_path, monkeypatch):
    """While regress replays (it drives the game, as a run does), another game's plugin is refused: cmd_regress holds
    the link as cmd_play does (cli.run_holds, bridge.serve_local's state "run")."""
    held = []
    state = {}
    link = types.SimpleNamespace(hello={"project": "Tiny", "engine": "unreal"}, _state=state)
    monkeypatch.setattr(cli, "Game", lambda link, codex=None: "game")
    monkeypatch.setattr(cli.codexmod.Codex, "for_project", staticmethod(lambda project: None))
    monkeypatch.setattr(cli, "regress_on", lambda game, project, args, **kw: held.append(dict(state)) or 0)
    assert cli.cmd_regress(link, cli.parser().parse_args(["regress", "--no-launch"])) == 0
    assert held == [{"run": {"project": "Tiny", "engine": "unreal"}}] and state == {}  # (held during, freed after)


def test_when_and_where_are_not_compared():
    """A replay that does what the recording did passes though its times and places moved a little (the agent's
    harnesses record when a lever was pulled, each HUD step's time, where the player ended): keys `t`, `..._t` and
    `..._at` aren't listed as changed, nor a regression's own "ignore"; what happened, how much and how long are
    listed when they changed."""
    then = {"opened": True, "pulled_at_t": 3.44, "player_at": [100, 2, 50], "seconds_open": 4.0, "mode": "open",
            "hud_steps": [{"t": 4.15, "from": 0, "to": 1}, {"t": 6.0, "from": 1, "to": 2}], "seed": "a7"}
    now = {"opened": True, "pulled_at_t": 4.93, "player_at": [130, 2, 41], "seconds_open": 4.2, "mode": "open",
           "hud_steps": [{"t": 2.85, "from": 0, "to": 1}, {"t": 5.1, "from": 1, "to": 2}], "seed": "b9"}
    assert regress.differences(then, now, ignore={"seed"}) == []
    assert regress.differences(then, now) == ["seed: 'a7' then, 'b9' now"]
    assert regress.differences(then, dict(now, seconds_open=9.0), ignore={"seed"}) == [
        "seconds_open: 4.0 then, 9.0 now"]
    assert regress.incidental("t") and regress.incidental("pulled_at_t") and regress.incidental("player_at")
    assert not regress.incidental("total") and not regress.incidental("attack")
    assert not regress.incidental("seconds_open") and not regress.incidental(3)


def test_a_replay_that_fails_is_tried_again_and_passes_on_the_first_try_that_does(monkeypatch):
    """A replay that doesn't pass is played again, up to 3 tries by default, and passes on the first try that does
    (a claim that something can happen holds when play shows it once; a game with bots doesn't always give the
    chance). With live decisions that's play varying; with recorded ones every try plays the same, so a pass after a
    failed try is FLAKY. One that fails every time fails. --retries 0: one try."""
    played, traces = [], []

    def run(outcomes, **kw):
        def replay(game, reg, **k):
            played.append(reg)
            traces.append(k.get("trace"))
            ok = outcomes[reg].pop(0) if len(outcomes[reg]) > 1 else outcomes[reg][0]
            return {"id": reg, "prompt": reg, "passed": ok, "why": "ended done" if ok else f"{reg} broke",
                    "seconds": 1.0, "differs": ["ammo_now: 8 then, 20 now"] if ok else []}

        monkeypatch.setattr(regress, "replay", replay)
        return regress.replay_all(None, list(outcomes), **kw)

    said = []
    results = run({"a": [False, False, True], "b": [False], "c": [True]}, say=said.append, client="jev",
                  out_dir="out")
    assert played == ["a", "a", "a", "b", "b", "b", "c"]
    assert traces[:3] == [os.path.join("out", "a.jsonl"), os.path.join("out", "a-try2.jsonl"),
                          os.path.join("out", "a-try3.jsonl")]
    s = regress.summary(results)
    assert s["passed"] == 2 and s["failed"] == ["b"] and s["flaky"] == []  # (live: not flaky)
    assert results[0]["tries"] == 3 and results[0]["earlier"] == ["a broke", "a broke"]
    assert results[0]["why"] == "passed on try 3; before: a broke; a broke" and said[0].startswith("  ok    a")
    assert "what changed (not compared): ammo_now: 8 then, 20 now" in said[0]
    assert results[1]["why"] == "b broke (on each of 3 tries)" and results[1]["tries"] == 3
    # recorded decisions: a pass after a failed try is flaky
    played.clear()
    results = run({"a": [False, True]}, say=None)
    assert results[0]["flaky"] and regress.summary(results)["flaky"] == ["a"] and played == ["a", "a"]
    played.clear()
    run({"a": [False, True]}, say=None, retries=0)
    assert played == ["a"]
    assert cli.parser().parse_args(["regress", "--retries", "0"]).retries == 0
    assert cli.parser().parse_args(["regress"]).retries is None  # (regress.RETRIES: 2)


def test_when_and_where_things_happened_are_not_compared():
    """When and where in the episode something happened differ from run to run (a replay's picks land a little earlier
    or later): the lever's pull at 3.44 s, replayed at 4.93 s, with the player a few studs elsewhere, is the same pull.
    What happened is still listed when it changed."""
    recorded = {"gate_open_attr": True, "pulled_at_t": 3.44, "player_at": [1192.0, 8.0, 122.0],
                "hud_steps": [{"t": 4.15, "hud_to": 1}, {"t": 11.7, "hud_to": 2}]}
    replayed = {"gate_open_attr": True, "pulled_at_t": 4.93, "player_at": [1188.0, -0.0, 122.0],
                "hud_steps": [{"t": 2.85, "hud_to": 1}, {"t": 7.62, "hud_to": 2}]}
    assert regress.differences(recorded, replayed) == []
    assert regress.differences(recorded, dict(replayed, gate_open_attr=False)) == [
        "gate_open_attr: True then, False now"]
    assert regress.differences(recorded, {k: v for k, v in replayed.items() if k != "pulled_at_t"}) == []
    # (a duration varies with play: listed when it moved, never a failure; the harness's done is the claim)
    assert regress.differences({"crack_to_gone_s": 0.4}, {"crack_to_gone_s": 0.6}) == []
    assert regress.differences({"crack_to_gone_s": 4.0}, {"crack_to_gone_s": 7.0}) == [
        "crack_to_gone_s: 4.0 then, 7.0 now"]
    assert regress.incidental("t") and regress.incidental("pulled_at_t") and regress.incidental("player_at")
    assert not any(regress.incidental(k) for k in ("seconds", "text", "attempts", "gate_open_attr", 3))


# -- regressions in the game's own folder, replays on a build of it ---------------------------------------------------

def remember_game(memory, path):
    with open(os.path.join(memory, "project.json"), "w", encoding="utf-8") as f:
        json.dump({"project": str(path)}, f)


def test_regressions_are_saved_in_the_memory_and_a_replay_lists_the_game_folders_too(tmp_path, memory):
    """A CONFIRMED claim's regression is saved in the memory, game on this machine or not: nothing goes into the
    game's own folder by itself (an export copies them there: test_gamefolder). A replay of the project lists the
    game's Autopilot/regressions (exported, or a teammate's from source control) and the memory's, one run's once,
    the game's copy first."""
    game = tmp_path / "Game"
    game.mkdir()
    (game / "Game.uproject").write_text("{}")
    remember_game(memory, game / "Game.uproject")
    run_dir, report = confirmed(tmp_path, memory)
    reg = regress.save(run_dir, report, "Tiny", project_root=memory)
    assert reg == os.path.join(memory, "regressions", "run-1")
    assert regress.folder("Tiny", memory) == os.path.join(memory, "regressions")
    assert not (game / "Autopilot").exists()
    assert regress.folders("Tiny", memory) == [os.path.join(memory, "regressions")]

    exported = os.path.join(str(game), "Autopilot", "regressions", "run-1")
    regress.save(run_dir, report, "Tiny", project_root=memory, dest=exported)
    teammate = os.path.join(str(game), "Autopilot", "regressions", "run-9")
    regress.save(run_dir, report, "Tiny", project_root=memory, dest=teammate)
    assert regress.folders("Tiny", memory) == [os.path.join(str(game), "Autopilot", "regressions"),
                                               os.path.join(memory, "regressions")]
    assert regress.listing_all(regress.folders("Tiny", memory)) == [exported, teammate]
    # a game no longer there: the memory's alone
    remember_game(memory, tmp_path / "gone" / "Gone.uproject")
    assert regress.folders("Tiny", memory) == [os.path.join(memory, "regressions")]


def test_regress_replays_the_ones_asked_and_tells_each_step(tmp_path, memory, capsys):
    """--only replays those regressions alone; --events prints each step as a line of JSON (what the QA window
    follows); the traces and results.json go to --out, not into the regressions folder (in the game's source)."""
    for rid in ("run-1", "run-2"):
        run_dir, report = confirmed(tmp_path, memory, rid=rid)
        regress.save(run_dir, report, "Tiny", project_root=memory)
    regs = os.path.join(memory, "regressions")
    out = tmp_path / "out"
    args = cli.parser().parse_args(["regress", "--recorded", "--regressions", regs, "--only", "run-2,nope",
                                    "--events", "--out", str(out)])
    game, world, _ = sim.connect()
    try:
        assert cli.regress_on(game, "Tiny", args) == 0
    finally:
        world.close()
    lines = capsys.readouterr().out.splitlines()
    events = [json.loads(line) for line in lines if line.startswith("{")]
    assert [e["event"] for e in events] == ["regressions", "replaying", "result", "summary"]
    assert [i["id"] for i in events[0]["items"]] == ["run-2"] and events[0]["out"] == str(out)
    assert events[0]["items"][0]["prompt"].startswith("Verify that walking to the coin")
    assert events[2]["id"] == "run-2" and events[2]["passed"] is True
    assert events[3]["passed"] == events[3]["total"] == 1 and "results" not in events[3]
    assert "No regression nope there" in lines
    assert json.loads((out / "results.json").read_text())["results"][0]["id"] == "run-2"
    assert (out / "run-2.jsonl").exists() and not os.path.exists(os.path.join(regs, "run-2", "replay.jsonl"))
    # without --events: no JSON lines; without --out: a new results folder
    args = cli.parser().parse_args(["regress", "--recorded", "--regressions", regs, "--only", "run-1"])
    game, world, _ = sim.connect()
    try:
        assert cli.regress_on(game, "Tiny", args) == 0
    finally:
        world.close()
    assert not [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert (tmp_path / "results" / "results.json").exists() and (tmp_path / "results" / "run-1.jsonl").exists()


def make_builds(tmp_path):
    """Built games as each engine lays them out (tiny files: only the layout, and Godot's pack magic, are read)."""
    def touch(path, data=b"MZ"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    ue = tmp_path / "ue" / "Windows"
    (ue / "Engine").mkdir(parents=True)
    unreal = touch(ue / "MyGame.exe")
    unreal_inner = touch(ue / "MyGame" / "Binaries" / "Win64" / "MyGame-Win64-Development.exe")
    unity = touch(tmp_path / "unity" / "MyGame.exe")
    (tmp_path / "unity" / "MyGame_Data").mkdir()
    godot = touch(tmp_path / "godot" / "MyGame.exe")
    touch(tmp_path / "godot" / "MyGame.pck")
    embedded = touch(tmp_path / "godot2" / "MyGame.exe", b"MZ" + bytes(64) + b"GDPC")
    other = touch(tmp_path / "other" / "tool.exe")
    editor = touch(tmp_path / "ed" / "UnrealEditor.exe")
    (tmp_path / "ed" / "Engine").mkdir()
    (tmp_path / "ed" / "UnrealEditor").mkdir()
    app = tmp_path / "mac" / "MyGame.app"
    touch(app / "Contents" / "MacOS" / "MyGame")
    (app / "Contents" / "UE").mkdir(parents=True)
    import plistlib

    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "MyGame"}))
    return dict(unreal=unreal, unreal_inner=unreal_inner, unity=unity, godot=godot, embedded=embedded, other=other,
                editor=editor, app=str(app))


def test_builds_are_told_apart(tmp_path):
    """--build takes an Unreal packaged game, a Unity player or a Godot export (or a macOS app of one), and tells which
    from the files beside it; anything else (an editor, another program, nothing) is none."""
    from autopilot import engines

    b = make_builds(tmp_path)
    found = {k: getattr(engines.find_build(v)[0], "name", None) for k, v in b.items()}
    assert found == {"unreal": "unreal", "unreal_inner": "unreal", "unity": "unity", "godot": "godot",
                     "embedded": "godot", "other": None, "editor": None, "app": "unreal"}
    assert engines.find_build(b["app"])[1] == os.path.join(b["app"], "Contents", "MacOS", "MyGame")
    assert engines.find_build(str(tmp_path / "missing.exe")) == (None, None)


def test_a_build_starts_headless_or_in_a_window(tmp_path):
    """Each engine starts a build without its project: headless by default, in a window to watch (--watch)."""
    from autopilot import engines

    b = make_builds(tmp_path)
    url, log = "ws://127.0.0.1:9/v1/bridge", "x.log"
    ue, unity, godot = engines.get("unreal"), engines.get("unity"), engines.get("godot")
    cmd = ue.launch_command(None, url, log, exe=b["unreal"])
    assert cmd[0] == b["unreal"] and "-nullrhi" in cmd and f"-PlaytestUrl={url}" in cmd and "-game" not in cmd
    cmd = ue.launch_command(None, url, log, exe=b["unreal"], headless=False)
    assert "-nullrhi" not in cmd and "-windowed" in cmd
    cmd = unity.launch_command(None, url, log, exe=b["unity"])
    assert cmd[:3] == [b["unity"], "-batchmode", "-nographics"] and f"-PlaytestUrl={url}" in cmd
    cmd = unity.launch_command(None, url, log, exe=b["unity"], headless=False)
    assert "-batchmode" not in cmd and "-nographics" not in cmd and cmd[1:3] == ["-screen-fullscreen", "0"]
    cmd = godot.launch_command(None, url, log, exe=b["godot"])
    assert cmd[:2] == [b["godot"], "--headless"] and "--path" not in cmd and f"-PlaytestUrl={url}" in cmd
    assert godot.launch_command(None, url, log, exe=b["godot"], headless=False)[1] == "--windowed"


def test_regress_on_a_build(tmp_path, monkeypatch, capsys):
    """`autopilot regress --build <exe>` starts the build (no project needed) told to connect here; one that isn't a
    build is refused; a build that ends before it connects (or never does: a Shipping build carries no plugin) is
    reported with its log, and the command fails."""
    b = make_builds(tmp_path)
    monkeypatch.setattr(cli, "regress_logs", lambda: str(tmp_path / "logs"))
    started = []
    monkeypatch.setattr(cli, "start_game", lambda args, cmd: started.append(cmd))
    args = cli.parser().parse_args(["regress", "--build", b["unity"], "--watch", "--events"])
    assert cli.launch_build(args, "ws://127.0.0.1:9/v1/bridge", say=lambda s: None) is True
    assert started[0][0] == b["unity"] and "-batchmode" not in started[0]
    assert args.game_log.startswith(str(tmp_path / "logs"))
    launching = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert launching["event"] == "launching" and launching["engine"] == "unity" and launching["watch"] is True
    with pytest.raises(SystemExit, match="isn't a game build Autopilot knows"):
        cli.launch_build(cli.parser().parse_args(["regress", "--build", b["other"]]), "ws://x")

    args.game_process = types.SimpleNamespace(poll=lambda: 3, pid=1, kill=lambda: None)
    monkeypatch.setattr(cli.processes, "kill", lambda proc: None)
    assert cli.game_ended(args)
    assert cli.game_never_connected(args) == 1
    out = capsys.readouterr().out
    assert ("The game ended (exit code 3) before it connected. A build connects only when it carries the Autopilot "
            "plugin: a Development build") in out and args.game_log in out
    assert json.loads(out.splitlines()[-1])["event"] == "error"


def test_waiting_for_a_plugin_gives_up_when_asked():
    """serve_local's give_up (the game it waits for has ended) stops the wait at once, not at its time limit."""
    import time

    from autopilot import bridge

    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        bridge.serve_local(port=0, wait=60, give_up=lambda: time.monotonic() - t0 > 0.5, http_port=None)
    assert time.monotonic() - t0 < 5
