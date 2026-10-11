> 下文保留 `decision-v1` 的实现边界。新的 `task-loop-v1` 已使用完整多轮子 Campaign；
> 预算、自动组织验证与恢复说明见 [MULTIROUND_EVALUATION.md](MULTIROUND_EVALUATION.md)。

# Task Loop 与 Harness 改进循环（第一阶段）

## 范围与角色

第一阶段只验证组合突变优化的 Task Harness 改进，不以一次涨分、提示修改或
版本采用声称完整 RSI。基础模型权重不变，不开放 Controller 任意源码修改。

- Task Loop：目标/已有证据 → Planner → Executor → 受控查询 → Analyst → 下一轮或最终报告。
- Meta Loop：轨迹/方法历史 → Improver 自主分析 → 带证据的提案 → 候选 W/M → 独立验证 → Controller 发布/拒绝。

Planner、Executor、Analyst、Improver、Evaluator 是逻辑角色，可共用一个模型。
为保护历史日志和提供商缓存，内部 A/B/C/M/E 标识不重写；`roles.py` 和 `prompts`
命令提供逻辑名，终端时间线同时显示逻辑名和原标识。没有新增五套服务。

## 本次实现的边界

`Campaign.analyze_feedback()` 是独立、幂等的任务检查点。每次研究反馈、包括最后
一轮都完成分析；禁用/暂停 Improver 或已有待验证提案不会跳过分析。最终分析
暂停时，原始测量已保存，`status.task_loop.analysis_pending` 为真；`step`/`replay`
恢复同一检查点，不重复查询。完成后生成带版本、证据及成本的 `task_reports`。

新的 `capability-v1` 研究中，已知的 Improver 提供商暂停会暂停自动改进，Task
可继续运行；恢复按原 `retry-llm` 和 `methods resume` 流程处理。不确定的外部执行、
完整性冲突仍停止推进，不能为“不中断任务”而吞掉未知副作用。计算任务复用反馈
分析检查点，但本阶段不为计算结果伪造实测验收。

`Patch.capability` 包含 problem、expected_behavior、applicability、validation_plan
和可选 regression_checks，另保留 hypothesis、evidence_refs、changes、target 和
base_version。它们约束提案的可检查表达，不规定诊断顺序，不要求先发现重复失败。
低 fitness 不等于已证明某种缺陷。只有启用 `capability-v1` 的研究强制此契约；
旧补丁没有该字段时，序列化和补丁哈希不变。

提示词由公共权限/证据底线、当前角色规则、适用阶段规则、版本化方法指导和
参考技能组成；任务/I/O 契约、证据引用及工具目录沿用实际请求的类型化上下文。
实验板/任务规则不再注入 Improver/Evaluator；验收专属规则不再注入 Planner。
完整实际请求继续由原 LLM 收据与轨迹存储保存，不用删证据压缩上下文。

## 可执行版本和发布

沿用 `Workflow` / `MetaPolicy` 和内容寻址快照。提示、配置和方法程序均是版本
内容；保留的程序在后续 W/M 调用中通过沙箱实际执行。Controller、隐藏标签入口、
预算与当前验收协议不可写。暂不支持候选任意替换安装目录源码或依赖。

`task_harness_invoked` 记录实际 W/M、程序版本和提示包；守护进程校验事件与绑定
TaskView 一致。`status.harness_use` 只统计发布后的主运行调用，验证分支不算部署。
`rsi_evidence` 继续分开报告 Meta 自修改、后续调用与收益未确立的状态。

回退只切换未来调用的版本指针，不撤销测量、查询或成本。旧研究保留原快照；
升级后新建运行目录，不在原记录上静默迁移。

## 独立 H₀/H₁ 验收

新 `start`/默认 `init` 和 `configs/research.mutation.json` 使用
`workflow_validation=independent`。待验证 W 不再在下一批普通研究中自动采用：
手动 `step` 可继续使用原 W。自动 `replay` 在待验证提案的安全边界返回，保留
后续预算；提供 `--validation-cases validation-cases.json` 时则自动执行独立比较，
再继续 Task Loop。也可在该边界显式运行 `evaluate-workflow` 后恢复 replay。
历史 `online` 模式和 M 比较入口仍保留，但不把任务内双臂试验标为独立能力证据。

```bash
proteinrsi init --task task.json --out runs/mutation-dual \
  --protein-model none --research-config configs/research.mutation.json \
  --method-governance configs/methods.fixed_meta.json
# 沿用实际 LLM 提供商配置和数据准备流程。
proteinrsi replay --campaign runs/mutation-dual --dataset measurements.csv --agent llm
# 在有待验证 W、没有待提交批次且预留预算的安全边界运行：
proteinrsi evaluate-workflow --campaign runs/mutation-dual \
  --cases validation-cases.json --agent llm --promote
```

`methods.fixed_meta.json` 仅允许 W 修改，适用于第一阶段；默认治理仍支持 W/M。
`--protein-model none` 只关闭可选 PLM，不关闭 LLM。LLM/tool 总次数仍可设置为
`unlimited`，超时、重试和单次执行限制保持；独立比较案例须给出有限计算上限。

案例采用现有 `MetaCase` JSON 列表格式：case_id、group_id、split、task、initial、
labels、query_budget（history 为空）。完整 labels 文件仅由 Controller 读取；
Worker 只获得初始证据和任务契约。相关种子应使用同一 group_id，不冒充独立蛋白。
同一蛋白中，已被提案过程看到的标签必须列入 initial，不能重新宣称为保留证据。

每个 W 在独立子存储运行同一案例，起始证据、工具、查询与计算上限相同，不调用
Improver，不继承当前研究的经验或另一组的结果。所有初始标签披露与新查询都按
现有离线评估协议计入共同账本；不会给每个分支凭空发一笔新预算。

**当前 H 比较是一组从初始证据出发的候选决策案例，不是完整多轮研究评测。**
它适用于生成/排序/程序使用等方法改动。长期记忆、恢复或多轮规划的能力主张，
还需相应行为测试或更长回放，不能用此单步指标自动代替。

LLM 模式复用 E-plan → 冻结指标程序 → 受控结果 → E-verdict，另提供每组实际
成本。原显式数值 gate 仅按其冻结协议执行。`accepted` 不是通用能力证明。

对已接受的冻结版本使用新的 test 案例确认，不发布版本：

```bash
proteinrsi evaluate-workflow --campaign runs/mutation-dual \
  --patch-id p-<已接受补丁> --cases confirmation-cases.json --agent llm
```

选择与确认不允许复用相同案例起点，改名称/随机种子/预算不构成新的保留案例；确认报告不进入 Agent 的方法记忆，也不能
改变验收标准或重新选版本。软件只能检查声明的案例/证据身份，无法替代实验者
对真正独立测试集的管理。跨蛋白或通用能力必须另有对应证据。

## 接受、拒绝、恢复和回退

比较开始前冻结案例、版本、计划和发布意图。完成的子分支有持久结果，提供商
暂停后继续同一比较；完成后重复命令读取原报告，不重新抽一次结果。预算不足
在执行前拒绝。未知完成状态阻止版本发布和新验证；确认外部工作已停止后：

```bash
proteinrsi methods abandon-evaluation --campaign runs/mutation-dual \
  --evaluation-id harness-<id> --operator <name> --reason <reason> --acknowledge-uncertain
proteinrsi methods rollback --campaign runs/mutation-dual \
  --target workflow --version w-<先前活动版本> --operator <name> --reason <reason>
```

失败和拒绝经验仍可检索，带验证状态和适用范围；拒绝不自动等于科学假设被否定。
独立确认报告在 operator 命名空间保留，不回流至候选选择记忆。

## 验证与结论

先验证分析解耦、最后一轮分析、暂停恢复、提案拒绝、真实版本调用、验证接受/
拒绝、成本守恒、保留案例隔离及指针回退，再做有预算的小规模 GB1 实测回放。
脚本化模型与合成标签测试只能证明机制。本次未运行真实 LLM/GB1 效果实验。
完整 RSI 的后续阶段仍需 M₀/M₁ 同起点对照，证明新版 M 更会产生后续改进。
