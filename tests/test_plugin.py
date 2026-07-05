from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path

import pytest

from agent.plugins.context import PluginContext, PluginKVStore
from plugin import PluginUndo, UndoCommandModule, _find_last_passive_turn, _undo_last_turn
from session.manager import SessionManager


class _MemoryEngine:
    def __init__(self, *, fail_real_undo: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_real_undo = fail_real_undo

    def undo_by_message_sources(
        self,
        message_ids: list[str],
        *,
        dry_run: bool = False,
    ) -> dict[str, object]:
        self.calls.append({"message_ids": list(message_ids), "dry_run": dry_run})
        if self.fail_real_undo and not dry_run:
            raise RuntimeError("memory cleanup failed")
        return {
            "affected_ids": ["mem1"],
            "restored_ids": ["old1"],
            "rollback_source_ids": ["cli:1:0", "cli:1:1", "cli:1:2"],
        }


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.asyncio
async def test_undo_command_aborts_without_running_llm(tmp_path) -> None:
    plugin = PluginUndo()
    session_manager = SessionManager(tmp_path)
    session = session_manager.get_or_create("cli:1")
    session.add_message("user", '<system-reminder data-system-context-frame="true">内部</system-reminder>')
    session.add_message("user", "u0")
    session.add_message("assistant", "a0")
    session_manager.save(session)
    memory_engine = _MemoryEngine()
    plugin.context = PluginContext(
        event_bus=None,
        tool_registry=None,
        plugin_id="plugin_undo",
        plugin_dir=tmp_path,
        data_dir=tmp_path,
        kv_store=PluginKVStore(tmp_path / ".kv.json"),
        session_manager=session_manager,
        memory_engine=memory_engine,
    )
    module = UndoCommandModule(plugin)
    state = SimpleNamespace(
        session_key="cli:1",
        session=session,
        msg=SimpleNamespace(
            content="/undo",
            channel="cli",
            chat_id="1",
            timestamp=datetime.now(),
        ),
    )
    frame = SimpleNamespace(input=state, slots={"session:session": state.session})
    result = await module.run(frame)
    assert result.slots["session:ctx"].abort is True
    assert [call["dry_run"] for call in memory_engine.calls] == [True, False]


def test_undo_deletes_context_user_assistant_three_rows(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:1")
    session.add_message("user", '<system-reminder data-system-context-frame="true">内部</system-reminder>')
    session.add_message("user", "u0")
    session.add_message("assistant", "a0")
    session.add_message("user", '<system-reminder data-system-context-frame="true">内部</system-reminder>')
    session.add_message("user", "u1")
    session.add_message("assistant", "a1")
    session.last_consolidated = 6
    manager.save(session)
    target = _find_last_passive_turn(session.messages)
    assert target is not None
    delete_indices, _, _ = target
    message_ids = [str(session.messages[i]["id"]) for i in delete_indices]
    result = _run(_undo_last_turn(manager, "cli:1", expected_message_ids=message_ids))
    assert result is not None
    assert result.deleted_ids == ["cli:1:3", "cli:1:4", "cli:1:5"]


def test_undo_keeps_cursor_when_target_after_consolidated_prefix(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:1")
    for index in range(3):
        session.add_message("user", '<system-reminder data-system-context-frame="true">内部</system-reminder>')
        session.add_message("user", f"u{index}")
        session.add_message("assistant", f"a{index}")
    session.last_consolidated = 6
    manager.save(session)
    target = _find_last_passive_turn(session.messages)
    assert target is not None
    delete_indices, _, _ = target
    message_ids = [str(session.messages[i]["id"]) for i in delete_indices]
    result = _run(_undo_last_turn(manager, "cli:1", expected_message_ids=message_ids))
    assert result is not None
    assert manager.get_or_create("cli:1").last_consolidated == 6
