---
name: autopilot
description: Playtest the game open in the user's Unreal, Unity, Godot or Roblox Studio editor with Autopilot (its `autopilot` MCP server) - explore the game for bugs, test it (go over it and report every problem), verify a claim about how it works or looks, or reach a goal in play. Use when the user asks to playtest, test, verify, check or explore their game, or to use Autopilot.
---

# Playtesting with Autopilot

Autopilot plays the game open in the user's editor: a fast decision model makes the choices in play, inside a
harness you write for this game and this request. You research the game's code, try things in the live game, write
and revise the harness, and report what play showed. Its tools come from the `autopilot` MCP server.

1. The game's project must be open in Unreal, Unity, Godot or Roblox Studio with the Autopilot plugin, its Autopilot tab connected.
   When start_run says Autopilot's agent service isn't running, tell the user that and stop.
2. Call `start_run` with the request in the user's words and its kind. A request is one of four types:
   - an exploration ("Explore the crafting system in Lvl_Main"): `explore`, with `minutes` of play when the user
     gives them;
   - a test ("Go through the currently open level and tell me everything that looks wrong"): `explore` too, as it
     plays as an exploration;
   - a verification ("Verify that the tree shakes when you hit it with an axe"): `verify`;
   - a goal ("Kill the boss while keeping your health above 20% of its maximum value"): `goal`.
   Pass `time_limit_minutes` when the user gives a time limit.
3. Read the INSTRUCTIONS.md it names, all of it, with Autopilot's `read_file` (in parts: it is long). Follow it: it is
   how to work, what the kit is, and the rules a report must meet.
4. A call that answers "still going" (an episode, the game starting) isn't done: call `wait` until it gives the
   result.
5. To look at the game's picture, never call `frames` yourself: hand the run's id, the episode, the times in it
   (seconds from its start, as its run_harness summary's t= gives them) and open questions (nothing about the request
   or what you expect) to the `frame-describer` subagent, as INSTRUCTIONS.md says.
6. If start_run asks for the decision model's key, tell the user where it goes (the file or the Autopilot tab it
   names). Never ask them to paste a key into this conversation.
7. Name in `report`'s `episode` the episode that shows the result best: its recording is the one the user watches.
   Once `report` says "Reported", the run is over: tell the user its outcome in a few lines. The Autopilot tab in
   their editor shows the whole result, with the recording.

## Several requests at once

When the user gives several requests to go through (a list, a spreadsheet, a CSV of prompts), run them in batches:
Autopilot plays up to three games side by side in the editor (Unreal), one run in each.

- Give each request to a subagent of its own, in the background, with the request, its kind and these steps 2-7 (the
  subagent calls `start_run` itself and works its run to the report); start at most three at once and, as each one
  reports, start the next. Each start passes `parallel` set to the number you keep going (3, or fewer when there are
  fewer requests): the first start of a batch starts that many games side by side; a start while that many runs go is
  refused until one reports.
- Every run has its own id (start_run answers with it): pass it as `run` with every Autopilot call of that run, and
  to its frame describer and episode reviewer. A run's subagent that can't start subagents of its own looks at the
  picture with `describe_frames` (Autopilot's own describer) instead.
- Working several runs yourself instead, keep their games playing together: make each run's `run_harness` (or
  `probe`) call in one message; while several runs go, each answers "still going" within seconds and plays on, so
  every run's episode starts at once. Then call `wait` for each of them, in one message.
- Don't end your turn while a run's subagent is still going: wait for each one's result (a session run without a
  person, `claude -p` or `codex exec`, ends with your turn, and its subagents with it).
- When every run has reported, tell the user each request's outcome in one line, in their order (a table when they
  gave a table).

Use Autopilot's own file tools for the game's folder (paths relative to it), never your own, and don't change the
user's project or the game's files: the game is acted on only through play, with Autopilot's tools. The user can
watch the run in the Autopilot tab, stop it there, or steer it; what they type there reaches you with your next tool
result. A hint leaves the episode playing; a redirect ended the episode there and changes the request from then on.
After either, they likely want play to go on from where it is: prefer run_harness restart=False unless what they said
asks for a restart.

<!-- autopilot connect: Autopilot's skill -->
