# Automatic Model Routing — Implementation Status

## Scope
Complexity-based auto-escalation for normal tasks and multi-agent (MoA) presets.

## Verified Artifacts
- `hermes_cli/complexity_routing.py` — `model` strategy fully implemented.
- `tests/hermes_cli/test_complexity_routing.py` — 13 tests green.
- `model_switch(scope="turn")` — invoked by routing layer when complexity threshold met.

## Blockers Resolved This Session
- Path-import bug: tests were resolving complexity_routing from the wrong package tree. Added repo-path sanity assertion.
- `get_complexity_routing` missing: added default-safe block loader + unwrapped-block shortcut so `select_target` accepts already-resolved routing blocks from tests and future callers.

## Current Limitations
- MoA preset activation path not yet wired; `select_target` returns `strategy: moa` but no caller consumes it.
- No runtime telemetry for escalation events (attempted, fallback, blocked by cap).

## Follow-up
- Kanban: `t_3db6bbc5` — "Wire complexity_routing.select_target into dispatch + MoA preset hook"
- Kanban: `t_c0816dd0` — "Add runtime telemetry and session-cap enforcement for complexity_routing"
- Kanban: `t_2cae8399` — "Research: Hermes profiles vs model_switch for complexity routing guidance"

## Next Steps
1. Integrate `select_target` into main dispatch decision path (`gateway/run.py` or equivalent).
2. Implement MoA preset activation hook.
3. Add escalation event logging + session-cap enforcement.

## Profiles vs `model_switch` for complexity routing

### Developer guidance observed in repo/docs/skills
- Profile-based inbound routing is described in `docs/profile-routing.md` and is tied to gateway multiplexing (`gateway.multiplex_profiles: true`). It selects an entire Hermes profile based on message source (guild/channel/thread), then the entire session runs under that profile's config.
- The `delegation-routing` skill covers automatic model selection for both `delegate_task` subagents and kanban workers. It classifies goal text, estimates complexity 1-10, and routes to a provider/model. Auto-escalation from local to cloud when complexity > threshold is explicitly part of that design, but is marked broken in the kanban-worker skill notes (July 2026).
- The core repo docs/skills do NOT contain an explicit statement like "always use profiles for complexity routing" or "never use model_switch for this." The strongest convention is: per-source routing → profiles (`profile_routes` + multiplexing); per-task routing → delegation-routing skill (`select_routing`, `route_kanban_task`) or manual `model_switch`.

### Recommendation
Use profiles when the routing split matches Hermes' source-based multiplexing (server/channel/thread identity). Example: one Discord guild should always default to a cheaper/free-tier model, another to deepseek.
Use `model_switch`/`select_target` for goal-based complexity routing because the decision should depend on the task text and complexity, not on where the message came from. Profiles are the wrong granularity if you want the same chat to sometimes use deepseek, sometimes use free tier, depending on task difficulty.
Preferred composition: base profile provides kanban/delegation defaults; complexity router overrides the model inline via `model_switch(scope="turn")` when a threshold is crossed. This avoids creating a combinatorial explosion of profiles for every complexity band.

### Pros/cons

| Approach | Pros | Cons |
|---|---|---|
| Profiles | Full isolated state per route. Static and explicit. | Granularity is source-based, not goal-based. Requires multiplexing. |
| `model_switch`/`select_target` | Per-task, goal-driven. Fits complexity estimation. | Overrides are ephemeral unless tracked. Caps/telemetry must be custom. |

### Pitfalls / compatibility notes
- Profile routing is ignored unless `gateway.multiplex_profiles: true`.
- Kanban worker `model_override` is a per-task DB field; if you set it, dispatcher argv injection wins over runtime routing unless you remove `model_override`.
- `resolved_model` tracking in `task_runs.metadata` is only partially confirmed (plugin registered; live dispatcher invocation not fully verified). Do not treat metadata absence as proof of non-routing.
- Free-tier preference (`stepfun/step-3.7-flash:free`) already appears in fallback rows and kanban override data; it is a valid fallback target but not a speed/efficacy guarantee.
