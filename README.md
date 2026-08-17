# Plugin Undo

Plugin Undo 提供 `/undo`，撤销当前 Session 最后一个完整的 completed interaction。

插件是 pure Plugin API v3，只声明 `COMMANDS` 与 `INTERACTION_UNDO`。SessionDB backup、
interaction transcript/embedding 删除、Default Memory durable reconciliation 和 Akasha rebuild
都由 Core `189c25a3e011c90cc8106fdda0b57c8c0ae71730` 拥有；插件不接触 SessionManager、SQL、
memory engine 或正式 workspace。

候选 generation 只验证拓扑与 command catalog，不能调用 destructive owner。正式 `/undo` 若
Session 删除已提交但 Default Memory 尚待收敛，会明确返回 error 结果，Core 在当前进程 retry 或
进程重启时重放 pending receipt。
