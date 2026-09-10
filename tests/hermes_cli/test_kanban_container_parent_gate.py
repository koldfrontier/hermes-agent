"""Tests for the container-aware kanban parent gate (RFC 2026-09-06).

Deadlock being fixed: ``claim_task`` refuses to claim a task while ANY direct
parent is not ``done``/``archived``. Container epics are parked (``blocked``)
by design and only complete at the end, so parent-linked stories were
rejected forever (``claim_rejected parents_not_done``, demoted ready->todo on
every master pass) — queue starvation until a human/monitor completed or
unlinked the epic (EP-SB t_e486b074; same class as t_a4f421b0 / t_5b39cef4).

With ``kanban.container_parent_gate`` enabled (default **false** — strict
backward compatibility), a parent that is ``blocked`` as an epic container
(``block_kind='container'``, or a legacy ``epic container`` marker in a
comment body / ``blocked``-event reason) counts as satisfied for the parent
gate, so linked stories can be promoted, claimed, and completed while the
epic stays parked.

Covered here:

* ``block_task(kind='container')`` parks in ``blocked`` (NOT ``todo`` like a
  dependency) and records the typed kind.
* ``_parents_satisfied`` matrix: strict default vs flag-on, typed vs legacy
  marker, and the no-over-permission negative cases.
* End-to-end claim / promote / complete paths (the original starvation loop).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"container_parent_gate": True}},
    )


def _make_ready(conn, tid: str) -> None:
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))


def _block_container_parent(
    conn, reason: str = "Epic container — stories below carry the work."
) -> str:
    """Create an epic container: parked in ``blocked`` with a container kind."""
    parent = kb.create_task(conn, title="epic container", assignee="worker")
    _make_ready(conn, parent)
    assert kb.block_task(conn, parent, reason=reason, kind="container") is True
    return parent


def _linked_child(conn, parent_id: str, title: str = "story") -> str:
    child = kb.create_task(conn, title=title, assignee="worker")
    kb.link_tasks(conn, parent_id=parent_id, child_id=child)
    return child


# ---------------------------------------------------------------------------
# block --kind container routing
# ---------------------------------------------------------------------------


def test_block_container_kind_parks_in_blocked(kanban_home: Path) -> None:
    """A container block is a generic human block: stays ``blocked``, typed."""
    with connect_closing() as conn:
        parent = _block_container_parent(conn, reason="parked")
        task = kb.get_task(conn, parent)
        assert task.status == "blocked"
        assert task.block_kind == "container"
        events = [e for e in kb.list_events(conn, parent) if e.kind == "blocked"]
        assert events, "expected a 'blocked' event"
        assert (events[-1].payload or {}).get("kind") == "container"


def test_block_container_kind_not_dependency_routed(kanban_home: Path) -> None:
    """Container must NOT go to ``todo`` like a dependency block does."""
    with connect_closing() as conn:
        parent = kb.create_task(conn, title="epic", assignee="worker")
        _make_ready(conn, parent)
        kb.block_task(conn, parent, reason="parked", kind="container")
        assert kb.get_task(conn, parent).status == "blocked"


# ---------------------------------------------------------------------------
# _parents_satisfied — strict default (flag off) must be unchanged
# ---------------------------------------------------------------------------


def test_parents_satisfied_strict_default(kanban_home: Path) -> None:
    """Flag off: only done/archived parents satisfy — container is ignored."""
    with connect_closing() as conn:
        # No parents -> satisfied
        lone = kb.create_task(conn, title="lone", assignee="worker")
        assert kb._parents_satisfied(conn, lone) is True

        # Done parent -> satisfied
        done_parent = kb.create_task(conn, title="done", assignee="worker")
        _make_ready(conn, done_parent)
        kb.claim_task(conn, done_parent, claimer="worker")
        kb.complete_task(conn, done_parent, result="ok")
        done_child = _linked_child(conn, done_parent)
        assert kb._parents_satisfied(conn, done_child) is True

        # Running parent -> NOT satisfied
        run_parent = kb.create_task(conn, title="run", assignee="worker")
        _make_ready(conn, run_parent)
        kb.claim_task(conn, run_parent, claimer="worker")
        run_child = _linked_child(conn, run_parent)
        assert kb._parents_satisfied(conn, run_child) is False

        # Blocked container parent (typed) -> NOT satisfied without the flag
        epic = _block_container_parent(conn)
        child = _linked_child(conn, epic)
        assert kb._parents_satisfied(conn, child) is False

        # Blocked container parent (legacy comment marker) -> NOT satisfied
        legacy = kb.create_task(conn, title="legacy epic", assignee="worker")
        kb.add_comment(
            conn, legacy, "user", "Epic container — stories below carry the work."
        )
        _make_ready(conn, legacy)
        assert kb.block_task(conn, legacy, reason="parked") is True
        legacy_child = _linked_child(conn, legacy)
        assert kb._parents_satisfied(conn, legacy_child) is False


# ---------------------------------------------------------------------------
# _parents_satisfied — container-aware (flag on)
# ---------------------------------------------------------------------------


def test_parents_satisfied_container_kind_flag_on(kanban_home: Path, gate_on) -> None:
    with connect_closing() as conn:
        epic = _block_container_parent(conn)
        child = _linked_child(conn, epic)
        assert kb._parents_satisfied(conn, child) is True
        # Running parents still gate even with the flag on.
        run_parent = kb.create_task(conn, title="run", assignee="worker")
        _make_ready(conn, run_parent)
        kb.claim_task(conn, run_parent, claimer="worker")
        run_child = _linked_child(conn, run_parent)
        assert kb._parents_satisfied(conn, run_child) is False


def test_parents_satisfied_legacy_comment_marker_flag_on(
    kanban_home: Path, gate_on
) -> None:
    """A pre-typed block with an 'epic container' comment marker qualifies."""
    with connect_closing() as conn:
        legacy = kb.create_task(conn, title="legacy epic", assignee="worker")
        kb.add_comment(
            conn, legacy, "user", "Epic container — stories below carry the work."
        )
        _make_ready(conn, legacy)
        assert kb.block_task(conn, legacy, reason="parked") is True
        assert kb.get_task(conn, legacy).block_kind is None  # legacy: un-typed
        child = _linked_child(conn, legacy)
        assert kb._parents_satisfied(conn, child) is True


def test_parents_satisfied_legacy_event_reason_flag_on(
    kanban_home: Path, gate_on
) -> None:
    """A legacy blocked-event reason matching 'epic container' qualifies."""
    with connect_closing() as conn:
        legacy = kb.create_task(conn, title="legacy epic", assignee="worker")
        _make_ready(conn, legacy)
        assert (
            kb.block_task(
                conn, legacy, reason="Epic container — stories below carry the work."
            )
            is True
        )
        assert kb.get_task(conn, legacy).block_kind is None
        child = _linked_child(conn, legacy)
        assert kb._parents_satisfied(conn, child) is True


def test_parents_satisfied_unmarked_blocked_still_gates_flag_on(
    kanban_home: Path, gate_on
) -> None:
    """Flag on must NOT over-permit: a plain blocked parent still gates."""
    with connect_closing() as conn:
        blocked = kb.create_task(
            conn, title="blocked for another reason", assignee="worker"
        )
        _make_ready(conn, blocked)
        assert kb.block_task(conn, blocked, reason="waiting on infra") is True
        child = _linked_child(conn, blocked)
        assert kb._parents_satisfied(conn, child) is False


# ---------------------------------------------------------------------------
# claim_task — the original starvation path
# ---------------------------------------------------------------------------


def test_claim_task_child_of_container_parent_flag_on_claims(
    kanban_home: Path, gate_on
) -> None:
    """With the flag on, a story under a parked container epic gets claimed."""
    with connect_closing() as conn:
        epic = _block_container_parent(conn)
        child = _linked_child(conn, epic)
        _make_ready(conn, child)
        claimed = kb.claim_task(conn, child, claimer="worker")
        assert claimed is not None
        assert kb.get_task(conn, child).status == "running"


def test_claim_task_child_of_container_parent_flag_off_demotes(
    kanban_home: Path,
) -> None:
    """Default (strict) behaviour: claim is rejected and demoted to todo."""
    with connect_closing() as conn:
        epic = _block_container_parent(conn)
        child = _linked_child(conn, epic)
        _make_ready(conn, child)
        assert kb.claim_task(conn, child, claimer="worker") is None
        assert kb.get_task(conn, child).status == "todo"
        rejected = [
            e
            for e in kb.list_events(conn, child)
            if e.kind == "claim_rejected"
            and (e.payload or {}).get("reason") == "parents_not_done"
        ]
        assert rejected, "expected a claim_rejected parents_not_done event"


# ---------------------------------------------------------------------------
# promote / recompute_ready — the demoted-todo starvation leg
# ---------------------------------------------------------------------------


def test_recompute_promotes_child_of_container_parent_flag_on(
    kanban_home: Path, gate_on
) -> None:
    """The todo -> ready promotion leg must be container-aware too, or the
    child can never reach the dispatcher even when the flag is on."""
    with connect_closing() as conn:
        epic = _block_container_parent(conn)
        child = _linked_child(conn, epic)
        # Fresh child created in todo, never claimed — the classic state.
        assert kb.get_task(conn, child).status == "todo"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_recompute_leaves_child_todo_flag_off(kanban_home: Path) -> None:
    """Default: a child under a parked container stays todo (the deadlock)."""
    with connect_closing() as conn:
        epic = _block_container_parent(conn)
        child = _linked_child(conn, epic)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"


# ---------------------------------------------------------------------------
# complete_task / unblock re-gate share the same predicate
# ---------------------------------------------------------------------------


def test_complete_child_of_container_parent_flag_on(kanban_home: Path, gate_on) -> None:
    """Children can complete while the container epic stays parked."""
    with connect_closing() as conn:
        epic = _block_container_parent(conn)
        child = _linked_child(conn, epic)
        _make_ready(conn, child)
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.complete_task(conn, child, result="story done") is True
        assert kb.get_task(conn, child).status == "done"
        assert kb.get_task(conn, epic).status == "blocked"


def test_unblock_child_of_container_parent_flag_on_lands_ready(
    kanban_home: Path, gate_on
) -> None:
    """The unblock/reopen re-gate honours the container predicate."""
    with connect_closing() as conn:
        epic = _block_container_parent(conn)
        child = _linked_child(conn, epic)
        _make_ready(conn, child)
        kb.claim_task(conn, child, claimer="worker")
        assert kb.block_task(conn, child, reason="paused", kind="needs_input") is True
        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "ready"
