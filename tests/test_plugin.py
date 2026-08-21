from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.plugin_composition import (
    COMMANDS,
    INTERACTION_UNDO,
    CommandExecution,
    CommandRegistry,
    InteractionUndoResult,
    InteractionUndoService,
    PluginCommands,
)
from agent.plugin_composition.context import CompositionRoot, PluginRuntime
from agent.plugins.manager import PluginManager
from bus.event_bus import EventBus
from plugin import apply
from session.manager import SessionManager


class _DefaultMemory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def describe(self) -> SimpleNamespace:
        return SimpleNamespace(name="default")

    def undo_by_message_sources(
        self,
        message_ids: list[str],
        *,
        dry_run: bool = False,
    ) -> dict[str, object]:
        assert dry_run is False
        self.calls.append(tuple(message_ids))
        return {"affected_ids": [], "restored_ids": []}


async def _registry(
    result: InteractionUndoResult | None,
) -> tuple[CompositionRoot, CommandRegistry]:
    root = CompositionRoot("undo-test")
    commands = PluginCommands()
    _ = await root.context.provide(COMMANDS, commands)

    async def undo_latest(_session_key: str) -> InteractionUndoResult | None:
        return result

    _ = await root.context.provide(
        INTERACTION_UNDO,
        InteractionUndoService(undo_latest),
    )
    runtime = PluginRuntime(
        plugin_id="plugin_undo",
        plugin_dir=Path("/plugin"),
        data_dir=Path("/data"),
        workspace=Path("/workspace"),
        config=None,
    )
    _ = await root.mount(
        lambda ctx: apply(ctx, None),
        name="plugin_undo",
        runtime=runtime,
    )
    return root, commands.freeze()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "kind", "text"),
    (
        (None, "success", "没有可撤销"),
        (
            InteractionUndoResult(
                "turn:1",
                "cli:1",
                ("m1", "m2"),
                "/backup.db",
                False,
                3,
                1,
            ),
            "success",
            "删除消息：2 条",
        ),
        (
            InteractionUndoResult(
                "turn:1",
                "cli:1",
                ("m1", "m2"),
                "/backup.db",
                True,
                3,
                1,
            ),
            "error",
            "等待 Core 重试",
        ),
    ),
)
async def test_command_projects_core_result_without_private_state(
    result: InteractionUndoResult | None,
    kind: str,
    text: str,
) -> None:
    root, registry = await _registry(result)

    execution = await registry.execute(
        "/undo",
        session_key="cli:1",
        channel="cli",
        chat_id="1",
        sender="user",
    )

    assert isinstance(execution, CommandExecution)
    assert execution.result.kind == kind
    assert text in execution.result.text
    await root.dispose()
    assert root.receipt().effects == ()


def _seed_interaction(manager: SessionManager) -> tuple[str, ...]:
    now = datetime.now(UTC).isoformat()
    rows = manager.control_store.persist_session(
        "cli:undo",
        created_at=now,
        updated_at=now,
        metadata={},
        messages=[
            {
                "role": "user",
                "content": "question",
                "timestamp": now,
                "extra": {
                    "control_turn_id": "turn:undo",
                    "turn_input_ordinal": 0,
                },
            },
            {
                "role": "assistant",
                "content": "answer",
                "timestamp": now,
                "extra": {
                    "control_turn_id": "turn:undo",
                    "turn_terminal": True,
                    "turn_input_count": 1,
                },
            },
        ],
    )
    return tuple(str(row["id"]) for row in rows)


@pytest.mark.asyncio
async def test_real_manager_candidate_is_inert_and_formal_command_deletes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    sessions = SessionManager(workspace)
    message_ids = _seed_interaction(sessions)
    memory = _DefaultMemory()
    source = Path(__file__).resolve().parents[1]
    plugin_dir = tmp_path / "plugins" / "plugin_undo"
    plugin_dir.mkdir(parents=True)
    shutil.copy2(source / "plugin.py", plugin_dir / "plugin.py")
    shutil.copy2(source / "akashic.plugin.toml", plugin_dir / "akashic.plugin.toml")
    manager = PluginManager(
        plugin_dirs=[tmp_path / "plugins"],
        event_bus=EventBus(),
        workspace=workspace,
        session_manager=sessions,
        memory_engine=memory,
        installed_cache_root=tmp_path / "cache",
    )
    try:
        await manager.load_all()
        stable = manager.current_snapshot
        assert stable is not None and stable.command_registry is not None

        plugin_path = plugin_dir / "plugin.py"
        plugin_path.write_text(
            plugin_path.read_text(encoding="utf-8").replace(
                'version = "2.0.0"',
                'version = "2.0.1"',
            ),
            encoding="utf-8",
        )
        manifest_path = plugin_dir / "akashic.plugin.toml"
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                'version = "2.0.0"',
                'version = "2.0.1"',
            ),
            encoding="utf-8",
        )
        candidate = await manager.prepare_candidate("plugin_undo")
        assert candidate is not None
        sessions.invalidate("cli:undo")
        assert tuple(
            str(row["id"]) for row in sessions.get_existing("cli:undo").messages
        ) == message_ids
        assert memory.calls == []

        published = await manager.publish_prepared("plugin_undo")
        assert published["publication_state"] == "committed"
        current = manager.current_snapshot
        assert current is not None and current.command_registry is not None
        execution = await current.command_registry.execute(
            "/undo",
            session_key="cli:undo",
            channel="cli",
            chat_id="undo",
            sender="user",
        )
        assert execution is not None
        assert execution.result.kind == "success"
        assert execution.result.text.startswith(
            "已撤销上一轮对话。"
            "\n删除消息：2 条"
            "\n压缩游标：0 → 0"
            "\n恢复备份："
        )
        backup_path = Path(execution.result.text.rsplit("：", 1)[1])
        assert backup_path.is_file()
        assert memory.calls == [message_ids]
        assert sessions.get_existing("cli:undo").messages == []
        root = current.composition_root
        assert root is not None
        await manager.terminate_all()
        assert root.receipt().effects == ()
        assert root.receipt().services == ()
    finally:
        sessions.close()
