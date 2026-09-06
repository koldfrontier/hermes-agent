# RFC: Container-aware claim gate — blocked container epics must not gate their stories

- **Status:** Accepted — implemented in this change-set (see `kanban.container_parent_gate` + `block --kind container`; tests in `tests/hermes_cli/test_kanban_container_parent_gate.py`)
- **Date:** 2026-09-06
- **Component:** Hermes Agent kanban (`hermes_cli/kanban_db.py`)
- **Discovered by:** lottorun-kanban-monitor during EP-SB deadlock; tracked as GAP `t_37b05cfb` on the `lottorun` board
- **Owner:** hermes-agent repo owner (peeranha board) — this RFC is the handoff artifact; tracked as a follow-up card on peeranha

---

## Summary

`claim_task` refuses to claim a task while ANY direct parent is not `done`/`archived`.
Container epics (created to hold decomposed stories, then immediately `block`ed with
"Epic container — stories below carry the work.") can never be `done` while their
stories run, and are `blocked` by design — so **every child is rejected forever**
(`claim_rejected parents_not_done`, demoted `ready -> todo` on each master pass).
This starves the queue until a human/monitor manually completes or unlinks the epic.

## Evidence

- EP-SB (`t_e486b074`): created `ready`, claimed, worker blocked it as a container.
  All 10 stories `SB-00..SB-09` were parent-linked.
- Every story dispatch showed `claim_rejected {reason: parents_not_done}` and was
  demoted to `todo` every tick (00:37 window, events on `t_3684c59f` / `t_fbaa908d`).
- Workaround applied by monitor: `hermes kanban complete t_e486b074` — gate then
  passed and children released. The one-off fix did not remove the foot-gun.
- Same class of deadlock has recurred before: `t_a4f421b0`, `t_5b39cef4`, EP-0001.

## Root cause

Central enforcement point — `_parents_satisfied()` (`hermes_cli/kanban_db.py:4617-4625`):

```sql
SELECT 1 FROM task_links l
JOIN tasks p ON p.id = l.parent_id
WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1
```

Called from `claim_task` (line 4772, rejection path 4652-4668), `promote`
(5409, 5446) and `dispatch` (6538). The check is **intentional** — the comment at
4651 cites RCA `kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md`:
a racy writer must never promote a task with undone parents. Real dependencies
(parent story → child story) must keep gating. The bug is the model: **blocked is
not a failure state for a container epic**, yet the gate treats `blocked` the same
as `running`/`todo`.

## Options considered

| Option | Description | Verdict |
|--------|-------------|---------|
| (a) Container-aware claim gate | Treat a parent `blocked` **as a container** (typed `block_kind='container'`, legacy fallback: comment/reason matches `/epic container/i`) as satisfied for its children | **Recommended** — narrow, schema-ready, reversible, per-board opt-in |
| (b) Teach implementers to `complete` container epics | Prompt-level: worker completes the epic instead of blocking | Done as mitigation; still leaves the gate hostile to any linked-epic board |
| (c) Auto-complete recompute for blocked epics with children | Monitor recompute auto-completes blocked epics | Works but mutates state outside the gate; loses the container marker (epic becomes `done` while stories run) |

Keep (b)+(c) as operational mitigations; ship (a) as the durable fix.

## Recommended design (a)

1. **New block kind `container`.** Extend `hermes kanban block --kind`
   (`{capability,dependency,needs_input,transient}` → add `container`) and the
   `block_task` kind validation. Schema already persists `tasks.block_kind` (TEXT,
   column exists; no migration needed). Container blocks behave like a generic block
   for the task itself (stays `blocked`, not dispatched), but are ignored by the
   parent gate.

2. **Gate change, config-gated.** New config key `kanban.container_parent_gate`
   (bool, **default `false`** — current strict behaviour; zero impact on in-flight
   claims until enabled). When `true`, `_parents_satisfied()` regards a parent as
   satisfied if it is `blocked` AND it is marked as a container. One-function change
   covers all call sites (claim/promote/dispatch):

   ```python
   def _parents_satisfied(conn, task_id):
       parent_ok = "p.status IN ('done','archived')"
       if cfg.kanban.container_parent_gate:
           parent_ok = ("(p.status IN ('done','archived') "
                        "OR (p.status = 'blocked' AND p.block_kind = 'container'))")
       return conn.execute(
           "SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
           f"WHERE l.child_id = ? AND NOT {parent_ok} LIMIT 1",
           (task_id,),
       ).fetchone() is None
   ```

   Legacy fallback (parent was blocked before `--kind container` existed, e.g.
   EP-SB `block_kind = NULL`): also treat as container-satisfied if any
   `task_comments.body` / `task_events` `blocked` payload contains
   `/epic container/i`. Element of escaping: the legacy fallback should only
   apply when `container_parent_gate` is enabled, to keep the flag single-purpose.

3. **Routing behaviour.** `dependency`-kind blocks already defer to parents and
   auto-unblock (kanban_db.py:4531); the new `container` kind is NOT auto-unblocked
   (it is the epic's intended steady state) and does not count against
   unblock-loop recurrence counters any differently than a generic block.

4. **Tests** (hermes `test_kanban_db` style):
   - `_parents_satisfied`: parent `done` → True; no parents → True; parent `running`
     → False; parent `blocked` + `block_kind='container'` → True (flag on) / False
     (flag off); parent `blocked` + legacy comment marker → True (flag on).
   - `claim_task` integration: child of blocked container epic gets claimed with
     flag on; is demoted to `todo` with `claim_rejected` event with flag off.
   - `promote` path (5409/5446) and `dispatch` (6538) share `_parents_satisfied`,
     so no separate tests required beyond a smoke assertion.

5. **Docs/migration.** Update this RFC → ADR on acceptance; document the flag in
   `hermes config` help; keep `docs/` pattern guidance aligned with
   `kanban-workflows` skill (epic ≠ parent).

## Rollout

- Release with flag **default false** (strict backward compatibility; cannot break
  a live board mid-flight).
- After release (non-urgent): enable per board —
  `hermes config set kanban.container_parent_gate true` (+ gateway restart) — or
  keep off entirely for boards that already follow the corrected no-link pattern.
- Board monitors: keep the no-link pattern as belt-and-suspenders; the gate change
  is defense-in-depth for legacy/directly-linked decompositions.

## Immediate mitigation (already applied, this change-set)

- **3 cron prompts corrected** (do NOT `hermes kanban link <epic> <story>`; comment
  story IDs on the epic; intra-story links only for ordering):
  `lottorun-kanban-autopilot` (a6d277a2cbdb), `lottorun-kanban-monitor`
  (4add269546ba), `gomew-kanban-recovery-monitor` (8dfb2ae46e7d) in
  `~/.hermes/cron/jobs.json` (backup: `jobs.json.bak-parentgate`).
- **Skill updated** `kanban-workflows`: Epic Decomposition Pattern corrected +
  recovery runbook in `references/epic-decomposition-pattern.md`.
- **Recovery runbook** (if a board is already deadlocked):
  `hermes kanban unlink <epic> <story>` per story → WAL checkpoint →
  `hermes kanban promote <story> --force`; or complete the container epic.

## Open questions

1. Should the default be `true` after a soak period instead of `false`? The
   predicate is narrow (blocked + container marker), so `true` is plausibly safe;
   conservative default chosen to honor "never touch in-flight claims".
2. Is a `container` kind the right surface, or should `dependency`-typed epics
   suffice? Container is semantically distinct (a scaffold, not an awaiting dep).
