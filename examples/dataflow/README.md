# 数据流示例

`ranking.protocol.json` 是可验证的协议模型示例：用户候选资源→C排序→A审查→排序资源，没有B设计节点，也没有必须调用的蛋白模型。

该文件可通过 `Protocol.model_validate_json(...)` 加载，再交给 `run_campaign_protocol(team, view, config, protocol=...)`。它是Python接口示例，不是一个不存在的CLI子命令。正常启用 `configs/research.protocol.json` 时由A生成协议，无须手写该文件。

必须使用真实任务及其已登记输入；本例没有附任何蛋白实验标签。结构工具的版本Schema来自实际注册表，所以不在静态例子中编造 `tool.result` 哈希。
