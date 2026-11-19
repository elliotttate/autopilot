"""Regression replay: every CONFIRMED verification is kept as a harness that plays again, with no agent.

When a run reports a claim CONFIRMED (agents/session.finish), the harness of the episode that showed it is saved under
the project's regressions folder with what that episode ended on:

    <regressions>/<project>/<run id>/
        harness.py          the episode's harness (the run's harness<n>.py)
        lib.py              the project's lib.py as the run left it (a snapshot: replay uses the live one by default)
        regression.json     the claim, the verdict, the episode's end (status state and evidence), Jev's picks in order

<regressions> is AUTOPILOT_REGRESSIONS (<it>/<project>/<run id>), else the project's memory folder
(memory/<project>/regressions/<run id>). Nothing goes into the game's own folder by itself: `autopilot codex --project
<source> --out <game>/Autopilot` exports them, with the codex and memory (autopilot/gamefolder.py), into
<game>/Autopilot/regressions/<run id>, which goes into source control with the game, so a QA machine or CI with no
Autopilot memory replays them on a build of the game (`autopilot regress --build <Game.exe> --regressions <that
folder>`, or the window `autopilot qa`). A replay of the project lists the game's folder's and the memory's.

A replay (`autopilot regress`, or replay() here) restarts the level the regression was played on and runs the saved
harness through the runtime as an episode did. Its decisions are Jev's, live, as the episode's were (`client`: the
harness is code, and what to do next is decided from what the game shows now: bots that move differently, a dodge,
an enemy elsewhere), with no agent; or, with no client (`autopilot regress --recorded`), ReplayJev's: the recorded
picks, in order, where they are offered, else the option the recording picked most: no model at all, for a game
that plays the same each time.

It passes when the episode ends done: the harness's own success condition, which is the claim (the gate opened when the
lever was pulled; five kills without a reload). Nothing else in its evidence is compared: rounds left, who holds
an objective at the end, the name an object spawned in play gets, how long it took all vary with every play of a
game with anything random in it, and the claim is none of them. What changed is listed with the result
(`differs`: booleans and strings that differ, with Blueprint object names by class, by_class; numbers that moved by
more than `tolerance`, relative, regression.json, default 0.25), so a change shows without failing the replay. A
regression's own `ignore` list leaves keys out of it. One that doesn't pass is played again (`retries`, default
2: up to 3 tries) and passes on the first try that does: a claim that something can happen holds when play shows it
once, and a game with bots doesn't always give the chance. With recorded decisions every try plays the same, so a
pass after a failed try is reported flaky (its harness reads the game too tightly). A replay's episode may last as
long as the recorded one was allowed to (regression.json's `max_seconds`), else 3x the recorded one. A stall (no
progress for the harness's stall_seconds) ends a run's episode so the agent revises the plan; a replay has no agent,
so it plays on through it to that limit (noted with the result), and ends early only on a frozen player: one that
didn't move and was sent no input since the last progress.

Only an episode that started from a restarted level is saved (one that went on from where an earlier episode left
play needs that state, which a replay can't rebuild): the latest such episode that ended done, else none.
"""

import json
import os
import re
import shutil
import sys
import time
import types

from . import levels as levelsmod
from . import runtime

FILE = "regression.json"
TOLERANCE = 0.25
RETRIES = 2  # times a replay that didn't pass is played again (the first try that passes passes)
LOOKAHEAD = 5  # recorded picks a replay may skip to find one offered now (timings differ between runs)
MIN_SECONDS = 20.0  # a replay's episode lasts at least this
LONGER = 3.0  # without the recorded episode's own limit, a replay's lasts up to this many times the recorded one


IN_GAME = ("Autopilot", "regressions")  # a game's regressions folder, inside its own project folder


def root():
    """Where regressions are kept: AUTOPILOT_REGRESSIONS, else None (each project's memory folder)."""
    return (os.environ.get("AUTOPILOT_REGRESSIONS") or "").strip() or None


def game_dir(project_root):
    """The game's project folder on this machine as its memory remembers it (project.json's `project`: the .uproject
    or the project's folder), or None when it isn't on this machine."""
    try:
        with open(os.path.join(project_root, "project.json"), encoding="utf-8") as f:
            path = (json.load(f) or {}).get("project")
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not path or not isinstance(path, str) or not os.path.exists(path):
        return None
    return path if os.path.isdir(path) else os.path.dirname(path)


def in_game(game):
    """The regressions folder inside a game's project folder."""
    return os.path.join(game, *IN_GAME)


def _memory(project, project_root):
    if project_root is None:
        from . import codex

        project_root = codex.project_dir(project)
    return project_root


def folder(project, project_root=None):
    """Where a project's regressions are saved: AUTOPILOT_REGRESSIONS/<project>, else its memory folder's
    regressions."""
    if root():
        return os.path.join(root(), project)
    return os.path.join(_memory(project, project_root), "regressions")


def folders(project, project_root=None):
    """Where a replay of the project looks, the folders that exist of: AUTOPILOT_REGRESSIONS/<project>; else the
    game's own folder's Autopilot/regressions (exported there, or a teammate's from source control) and the project's
    memory's."""
    if root():
        found = [os.path.join(root(), project)]
    else:
        project_root = _memory(project, project_root)
        game = game_dir(project_root)
        found = ([in_game(game)] if game else []) + [os.path.join(project_root, "regressions")]
    return [f for f in found if os.path.isdir(f)]


def results_dir():
    """A new folder for a replay's traces and results: regress/<time> in Autopilot's settings folder."""
    from . import bridge

    return os.path.join(os.path.dirname(bridge.brain_file()), "regress", time.strftime("%Y%m%d-%H%M%S"))


# -- reading an episode's trace --------------------------------------------------------------------------------------

def read_trace(path):
    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return events


def episode_of(events):
    """What a replay needs of one episode's trace: {"continued", "end": {state, note, evidence}, "picks", "extras",
    "t", "max_seconds"} (picks: the options Jev's answers chose, in order; extras: its other answers, by decision;
    max_seconds: how long the episode was allowed to play)."""
    picks, extras, end, start_t, continued, limit = [], [], None, None, False, None
    for e in events:
        kind = e.get("kind") or e.get("event")
        if kind == "start" and isinstance(e.get("max_seconds"), (int, float)):
            limit = e["max_seconds"]
        elif kind == "continued":
            continued = True
        elif kind == "decision" and e.get("chosen"):
            picks.append(e["chosen"])
            extras.append(e.get("extra") or {})
            if start_t is None:
                start_t = e.get("t")
        elif kind == "end":
            end = e
    status = (end or {}).get("status") or {}
    t = ((end or {}).get("progress") or {}).get("t")
    return {"continued": continued, "ended": end is not None, "reason": (end or {}).get("reason"),
            "end": {"state": status.get("state"), "note": status.get("note"), "evidence": status.get("evidence") or {}},
            "picks": picks, "extras": extras, "t": t, "max_seconds": limit}


def confirming_episode(run_dir):
    """(n, episode) of the run's latest episode that started from a restarted level and ended done, else None."""
    ns = []
    for name in os.listdir(run_dir):
        if name.startswith("trace") and name.endswith(".jsonl") and name[5:-6].isdigit():
            ns.append(int(name[5:-6]))
    for n in sorted(ns, reverse=True):
        if not os.path.exists(os.path.join(run_dir, f"harness{n}.py")):
            continue
        ep = episode_of(read_trace(os.path.join(run_dir, f"trace{n}.jsonl")))
        if ep["ended"] and not ep["continued"] and ep["end"]["state"] == "done":
            return n, ep
    return None


# -- saving --------------------------------------------------------------------------------------------------------

def level_of(game):
    """The level a run's episodes played on game, as a replay opens it, or None when it isn't known: the run's level
    (game.level: asked for, or restarted into), else the one the game played (cli.prepare's Level: playing, else the
    editor's open one). As the project's list of levels names it when it is listed; a PIE world's prefix dropped
    (/Game/Maps/UEDPIE_0_Arena -> /Game/Maps/Arena). A replay on a build needs it: a build starts in its default map
    (often a menu), not the level the editor had open."""
    info = getattr(game, "level_info", None)
    for found in (getattr(game, "level", None), getattr(info, "playing", None), getattr(info, "open", None)):
        found = str(found or "").strip()
        if not found:
            continue
        listed = [p for p in (getattr(info, "levels", None) or []) if levelsmod.same(p, found)]
        if listed:
            return listed[0]
        head, _, tail = found.replace("\\", "/").rpartition("/")
        tail = re.sub(r"^UEDPIE_\d+_", "", tail)
        return f"{head}/{tail}" if head else tail
    return None


def save(run_dir, report, project, project_root=None, dest=None, level=None):
    """Keep a CONFIRMED verification's harness as a regression; its folder, or None (not a confirmed claim, or no
    episode a replay can play: see the module's docstring). level: the level the episode played (level_of), kept as
    regression.json's `map` for the replay to open."""
    if str(report.get("outcome") or "").strip().upper() != "CONFIRMED":
        return None
    found = confirming_episode(run_dir)
    if not found:
        return None
    n, ep = found
    rid = str(report.get("run") or os.path.basename(os.path.normpath(run_dir)))
    dest = dest or os.path.join(folder(project, project_root), rid)
    os.makedirs(dest, exist_ok=True)
    shutil.copyfile(os.path.join(run_dir, f"harness{n}.py"), os.path.join(dest, "harness.py"))
    lib = os.path.join(project_root, "lib.py") if project_root else None
    if lib and os.path.exists(lib):
        shutil.copyfile(lib, os.path.join(dest, "lib.py"))
    spec = {
        "id": rid, "project": project, "prompt": report.get("prompt"), "outcome": "CONFIRMED",
        "summary": report.get("summary"), "evidence": report.get("evidence") or [],
        "saved": time.strftime("%Y-%m-%dT%H:%M:%S"), "episode": n,
        "expect": ep["end"], "picks": ep["picks"], "extras": ep["extras"], "played_seconds": ep["t"],
        "tolerance": TOLERANCE,
    }
    if level:
        spec["map"] = level
    if ep.get("max_seconds"):
        spec["max_seconds"] = ep["max_seconds"]
    with open(os.path.join(dest, FILE), "w", encoding="utf-8") as f:
        json.dump(runtime.jsonable(spec), f, indent=1)
    return dest


def save_from_session(session, report):
    """agents/session.finish's hook: the run's regression when it CONFIRMED a claim (never raises)."""
    try:
        return save(session.run_dir, report, session.project, project_root=session.root,
                    level=level_of(getattr(session, "game", None)))
    except Exception as e:
        try:
            session.note(f"couldn't save the regression: {type(e).__name__}: {e}")
        except Exception:
            pass
        return None


def listing(base):
    """The regression folders under base (a project's regressions folder, one regression's, or a tree of them)."""
    if os.path.exists(os.path.join(base, FILE)):
        return [base]
    found = []
    for here, _, files in os.walk(base):
        if FILE in files:
            found.append(here)
    return sorted(found)


def listing_all(bases):
    """The regression folders under each of bases, one per run id (a run's in an earlier base wins: the game's own
    folder over the memory's copy from before)."""
    found, ids = [], set()
    for base in bases:
        for reg in (listing(base) if os.path.isdir(base) else []):
            rid = os.path.basename(os.path.normpath(reg))
            if rid not in ids:
                ids.add(rid)
                found.append(reg)
    return found


def describe(reg):
    """What a list of regressions shows of one: {"id", "prompt", "summary", "saved", "dir"} (a folder whose
    regression.json doesn't read: its id and dir)."""
    try:
        spec = load(reg)
    except (OSError, ValueError):
        spec = {}
    return {"id": spec.get("id") or os.path.basename(os.path.normpath(reg)), "prompt": spec.get("prompt"),
            "summary": spec.get("summary"), "saved": spec.get("saved"), "dir": reg}


def load(reg):
    with open(os.path.join(reg, FILE), encoding="utf-8") as f:
        return json.load(f)


# -- replaying -----------------------------------------------------------------------------------------------------

class ReplayJev:
    """Jev's stand-in for a replay: the recorded picks in order (the next one offered within LOOKAHEAD), else the
    option offered that the recording picked most, else the first offered. Other questions get the recorded answers
    of the same decision, else neutral ones. No model is called."""

    decision_provider = "recorded"

    def __init__(self, picks, extras=None):
        self.picks = list(picks or [])
        self.extras = list(extras or [])
        self.at = 0
        self.calls = 0
        self.counts = {}
        for p in self.picks:
            self.counts[p] = self.counts.get(p, 0) + 1

    def pick(self, names):
        for i in range(self.at, min(len(self.picks), self.at + LOOKAHEAD)):
            if self.picks[i] in names:
                self.at = i + 1
                return self.picks[i], i
        ranked = sorted(names, key=lambda n: -self.counts.get(n, 0))
        return (ranked[0] if ranked else None), None

    def system_one(self, state, questions):
        self.calls += 1
        ns = types.SimpleNamespace
        names = list(getattr(questions.get("pick"), "criteria", None) or {})
        chosen, i = self.pick(names)
        recorded = self.extras[i] if i is not None and i < len(self.extras) else {}
        choices, nouls, scores = {}, {}, {}
        if chosen is not None:
            rest = (0.1 / (len(names) - 1)) if len(names) > 1 else 0.0
            choices["pick"] = ns(choice=chosen, probabilities={n: 0.9 if n == chosen else rest for n in names},
                                 confidence=0.9)
        for k, q in questions.items():
            if k == "pick":
                continue
            kind = type(q).__name__
            if kind == "Noul":
                v = recorded.get(k)
                nouls[k] = ns(noul=v if isinstance(v, (int, float)) else 0.5)
            elif kind == "Score":
                v = recorded.get(k)
                scores[k] = ns(score=v if isinstance(v, (int, float)) else 0.5)
            else:
                crit = list(getattr(q, "criteria", None) or {})
                v = recorded.get(k)
                probs = dict(v) if isinstance(v, dict) else {c: 1.0 / len(crit) for c in crit}
                if probs:
                    choices[k] = ns(choice=max(probs, key=probs.get), probabilities=probs, confidence=0.5)
        return ns(choices=choices, nouls=nouls, scores=scores, usage=None)


def incidental(key):
    """Whether an evidence key names when (`t`, `pulled_at_t`) or where (`player_at`) in the episode something
    happened: details of one run, which a replay's timing moves, so not compared."""
    return isinstance(key, str) and (key == "t" or key.endswith(("_t", "_at")))


INSTANCE = re.compile(r"\b(\w+?_C)_\d+\b")  # a Blueprint object's name: its class, then an instance number


def by_class(text):
    """Text with Blueprint object names as their classes (BP_Rifle_C_2147469854 -> BP_Rifle_C): an object the
    game creates as it runs is numbered as it is created, in the editor from a small counter and in a packaged build
    from another, so the same rifle has another name in every play; one placed in the level keeps its name
    (BP_Gate_A). Only Blueprint instances (`..._C_<n>`): Lvl_2 stays Lvl_2."""
    return INSTANCE.sub(r"\1", text) if isinstance(text, str) else text


def differences(expected, got, tolerance=TOLERANCE, ignore=(), at=""):
    """What changed in a replay's evidence from the recording's, as "key: then, now" lines: shown with the result,
    never a failure (the harness's done is the claim; the rest varies with play: rounds left, who holds an objective
    at the end). Booleans, strings (object names by class: by_class) and None when they differ; numbers when they
    moved by more than tolerance (relative, at least of 1); lists when their length changed; keys there then and
    missing now.
    When and where (incidental keys) and the keys in ignore (a regression's own "ignore") are left out."""
    out = []
    where = at or "value"
    if isinstance(expected, dict) and isinstance(got, dict):
        for k, v in expected.items():
            if incidental(k) or k in ignore:
                continue
            key = f"{at}.{k}" if at else str(k)
            if k not in got:
                out.append(f"{key}: missing now")
            else:
                out += differences(v, got[k], tolerance, ignore, key)
    elif isinstance(expected, list) and isinstance(got, list):
        if len(expected) != len(got):
            out.append(f"{at or 'list'}: {len(expected)} then, {len(got)} now")
    elif (isinstance(expected, (int, float)) and not isinstance(expected, bool)
          and isinstance(got, (int, float)) and not isinstance(got, bool)):
        if abs(got - expected) > tolerance * max(1.0, abs(expected)):
            out.append(f"{where}: {expected!r} then, {got!r} now")
    elif by_class(expected) != by_class(got) or type(expected) is not type(got):
        out.append(f"{where}: {by_class(expected)!r} then, {by_class(got)!r} now")
    return out


def _load_harness(reg, lib_dir):
    """The regression's harness, its `import lib` the one in lib_dir."""
    sys.modules.pop("lib", None)
    sys.path.insert(0, lib_dir)
    try:
        return runtime.load_harness(os.path.join(reg, "harness.py"))
    finally:
        try:
            sys.path.remove(lib_dir)
        except ValueError:
            pass


def start_level(harness, spec, level=None):
    """The level a replay restarts into: the harness's start_map, else the level the regression was played on (its
    `map`), else `level` (`regress --level`: for regressions saved before they kept it), else None (the level the game
    is on starts over)."""
    return getattr(harness, "start_map", None) or spec.get("map") or level or None


def episode_limit(spec, seconds=None):
    """How long a replay's episode may play: `seconds` (--seconds), else as long as the recorded episode was allowed
    (max_seconds), else LONGER times the recorded one; at least MIN_SECONDS."""
    if seconds:
        return float(seconds)
    if isinstance(spec.get("max_seconds"), (int, float)) and spec["max_seconds"] > 0:
        return max(MIN_SECONDS, float(spec["max_seconds"]))
    return max(MIN_SECONDS, LONGER * float(spec.get("played_seconds") or 0))


def replay(game, reg, project_root=None, frozen_lib=False, seconds=None, restart=True, trace=None, level=None,
           client=None):
    """Play regression reg (its folder) again on game: {"id", "passed", "why", "state", "evidence", "differs",
    "decisions", "seconds", "trace", "decided"}. client: Jev's client, deciding live (runtime.jev_client()); None:
    the recorded decisions (ReplayJev, no model). lib: the project's live lib.py (project_root) unless frozen_lib, or
    there is none: then the snapshot saved with it. trace: where the replay's trace goes (default: replay.jsonl in
    reg). level: the level to replay in when neither the harness nor the regression names one (start_level)."""
    spec = load(reg)
    started = time.time()
    out = {"id": spec.get("id") or os.path.basename(reg), "prompt": spec.get("prompt"), "passed": False, "why": "",
           "state": None, "evidence": None, "differs": [], "decisions": 0, "seconds": 0.0, "dir": reg,
           "decided": "live" if client is not None else "recorded"}
    lib_dir = reg
    if not frozen_lib and project_root and os.path.exists(os.path.join(project_root, "lib.py")):
        lib_dir = project_root
    try:
        harness = _load_harness(reg, lib_dir)
    except Exception as e:
        out["why"] = f"the harness doesn't load: {type(e).__name__}: {e}"
        out["seconds"] = round(time.time() - started, 2)
        return out
    expect = spec.get("expect") or {}
    limit = episode_limit(spec, seconds)
    trace = trace or os.path.join(reg, "replay.jsonl")
    os.makedirs(os.path.dirname(os.path.abspath(trace)), exist_ok=True)
    try:
        if restart:
            game.restart(map=start_level(harness, spec, level))
        game.pause(False)
        decider = client if client is not None else ReplayJev(spec.get("picks"), spec.get("extras"))
        # No agent revises a replay's plan, so a stall doesn't end it (only a frozen player does): it plays on to the
        # episode's time limit
        rt = runtime.Runtime(game, harness, trace, client=decider, max_seconds=limit, stall_ends=False)
        result = rt.run()
    except Exception as e:
        out["why"] = f"the replay failed: {type(e).__name__}: {e}"
        out["seconds"] = round(time.time() - started, 2)
        return out
    finally:
        try:
            game.release_all()
        except Exception:
            pass
    status = result.get("status") or {}
    out.update(state=status.get("state"), evidence=status.get("evidence"), decisions=result.get("decisions") or 0,
               seconds=round(time.time() - started, 2), trace=trace)
    tolerance, ignore = float(spec.get("tolerance", TOLERANCE)), set(spec.get("ignore") or ())
    out["differs"] = differences(expect.get("evidence") or {}, status.get("evidence") or {}, tolerance, ignore)
    out["stalls"] = result.get("stalls_passed") or []
    played_on = stalls_said(out["stalls"])
    if status.get("state") != expect.get("state"):
        reason = result.get("reason") or ""
        if reason == "stalled":
            reason = "stalled: the player stood still and the harness sent no input"
        out["why"] = (f"ended {status.get('state') or 'without a status'} ({reason}"
                      f"{': ' + status['note'] if status.get('note') else ''}), not {expect.get('state')}{played_on}")
        return out
    out["passed"] = True
    out["why"] = f"ended {status.get('state')}" + (f": {status['note']}" if status.get("note") else "") + played_on
    return out


def stalls_said(stalls):
    """The stalls a replay played on through, in words for its result ('' for none)."""
    if not stalls:
        return ""
    at = ", ".join(f"t={s.get('since_t')}-{s.get('t')} s" for s in stalls)
    return f"; played on through {len(stalls)} stall{'s' if len(stalls) > 1 else ''} (no progress at {at})"


def try_trace(trace, n):
    """Where try n of a replay's trace goes: trace itself for the first, <name>-try<n>.jsonl for the others."""
    if not trace or n <= 1:
        return trace
    stem, ext = os.path.splitext(trace)
    return f"{stem}-try{n}{ext}"


def replay_all(game, regs, project_root=None, frozen_lib=False, seconds=None, say=print, retries=RETRIES,
               out_dir=None, on_event=None, level=None, client=None):
    """replay() of each regression folder in regs, one after another (one game): their results. One that doesn't
    pass is played again, up to `retries` times, and passes on the first try that does (its "tries": how many it
    took; "earlier": why the tries before didn't). With live decisions (client) that is play varying: bots elsewhere,
    no chance this time. With recorded ones (no client) every try plays the same, so a pass after a failed try is
    FLAKY (its "flaky": its harness reads the game too tightly, or the game itself varies). out_dir: where the
    replays' traces go (<id>.jsonl, <id>-try<n>.jsonl; default: in each regression's folder). on_event: told
    {"event": "replaying", "id", "try"} as each try starts and {"event": "result", **result} as each regression ends.
    level: the level to replay those that don't name one in (start_level)."""
    results = []
    tell = on_event or (lambda event: None)
    for reg in regs:
        rid = describe(reg)["id"]
        trace = os.path.join(out_dir, f"{rid}.jsonl") if out_dir else os.path.join(reg, "replay.jsonl")
        earlier = []
        for n in range(1, max(0, retries) + 2):
            tell({"event": "replaying", "id": rid, "try": n, **({"why": earlier[-1]} if earlier else {})})
            r = replay(game, reg, project_root=project_root, frozen_lib=frozen_lib, seconds=seconds,
                       trace=try_trace(trace, n), level=level, client=client)
            if r["passed"]:
                break
            earlier.append(r["why"])
        r = dict(r, tries=n, earlier=earlier[:n - 1] if r["passed"] else earlier[:-1])
        if r["passed"] and earlier:
            r["why"] = f"passed on try {n}; before: {'; '.join(earlier)}"
            if client is None:
                r["flaky"] = True
        elif not r["passed"] and n > 1:
            r["why"] = f"{r['why']} (on each of {n} tries)" if len(set(earlier)) == 1 else \
                f"not passed in {n} tries: {'; '.join(earlier)}"
        results.append(r)
        tell({"event": "result", **r})
        if say:
            mark = "FLAKY" if r.get("flaky") else "ok   " if r["passed"] else "FAIL "
            said = f"  {mark} {r['id']}: {r['prompt'] or ''}  ({r['why']}, {r['seconds']:.0f}s)"
            if r.get("differs"):
                said += "\n         what changed (not compared): " + "; ".join(r["differs"])
            say(said)
    return results


def summary(results):
    return {"passed": sum(1 for r in results if r["passed"]), "total": len(results),
            "failed": sorted(r["id"] for r in results if not r["passed"]),
            "flaky": sorted(r["id"] for r in results if r.get("flaky")), "results": results}
