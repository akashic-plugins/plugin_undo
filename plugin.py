from __future__ import annotations

from agent.plugin_composition import (
    COMMANDS,
    INTERACTION_UNDO,
    CommandDefinition,
    CommandInvocation,
    CommandResult,
)

api_version = 3
name = "plugin_undo"
version = "2.0.0"
inject = (COMMANDS, INTERACTION_UNDO)


async def apply(ctx, config) -> None:
    """注册只调用 Core destructive owner 的 `/undo` 命令。"""

    _ = config
    undo = ctx.require(INTERACTION_UNDO)

    async def handle(invocation: CommandInvocation) -> CommandResult:
        result = await undo.undo_latest(invocation.session_key)
        if result is None:
            return CommandResult(kind="success", text="没有可撤销的上一轮对话。")
        if result.reconciliation_pending:
            return CommandResult(
                kind="error",
                text=(
                    "上一轮对话已撤销，但派生记忆仍在等待 Core 重试收敛。"
                    f"\n删除消息：{len(result.message_ids)} 条"
                ),
            )
        return CommandResult(
            kind="success",
            text=(
                "已撤销上一轮对话。"
                f"\n删除消息：{len(result.message_ids)} 条"
            ),
        )

    await ctx.require(COMMANDS).register(
        ctx,
        CommandDefinition(
            name="undo",
            description="撤销上一轮对话",
            handler=handle,
        ),
    )
