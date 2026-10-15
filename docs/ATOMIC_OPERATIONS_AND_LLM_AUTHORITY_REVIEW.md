# ProteinRSI 原子操作与 LLM 权限核对

核对日期：2026-10-14。范围为当前本地代码及 GB1 前17轮流程审计，另参考第18轮失败步骤。本文是分析与设计建议，没有修改运行代码或恢复实验。当前本地存在尚未提交的修改，因此结论针对工作区，而不是仅针对远程 main。

结论：保留 LLM 自主规划、写代码、调用工具、修复程序及改进 W/M 的能力；把授权范围、可信数据注入、实验反馈写入和方法生效规则做成稳定接口。同一个 Agent 系统内部，模型输出、工具执行和可信状态管理承担不同职责。不能把科学自主性、操作权限、数据格式和执行可靠性混为一件事。

## 参考项目实际提供什么

| 项目与核对来源 | 实际机制 | 对 ProteinRSI 的启示 |
| --- | --- | --- |
| [LangGraph ToolNode 源码](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/tool_node.py) | 工具注册与调用分派；调用前注入 state/store/runtime；剔除模型伪造的受注入参数；按配置把部分错误返回成 ToolMessage。默认不会吞掉所有执行错误。 | 模型只填写自己能决定的参数；可信上下文由执行侧注入。图编排和参数校验本身不替应用决定数据库授权或提供代码沙箱。 |
| [Claude Agent SDK 权限回调示例](https://github.com/anthropics/claude-agent-sdk-python/blob/main/examples/tool_permission_callback.py)、[官方权限说明](https://code.claude.com/docs/en/agent-sdk/permissions) | 按调用应用权限规则、hooks和批准机制。allowed_tools 是预批准规则，不是独立的完整工具白名单；已经批准的调用可能不经过 can_use_tool。 | 必须区分工具可见、调用授权、是否需要人工批准。每次都必须执行的硬性检查不能只挂在询问批准的回调中。不要把示例中的命令字符串匹配当成完整沙箱。 |
| [smolagents CodeAgent 源码](https://github.com/huggingface/smolagents/blob/main/src/smolagents/agents.py)、[执行器说明](https://huggingface.co/docs/smolagents/reference/python_executors) | 将工具和变量交给执行器，模型以代码组合动作并利用执行反馈。官方明确说明 LocalPythonExecutor 不是安全沙箱，隔离不可信代码需要其他执行环境。 | 现场写程序、函数和循环是可保留的能力；限制的是执行环境能访问什么。失败后修改代码是正常任务执行行为，单次纠错不足以证明 RSI 提升。 |
| [OpenAI Agents SDK 上下文说明](https://github.com/openai/openai-agents-python/blob/main/docs/context.md)、[工具执行生命周期](https://github.com/openai/openai-agents-python/blob/main/.agents/references/tool-execution-lifecycle.md) | 共用应用策略，但区分工具暴露与具体参数/资源授权；在产生副作用前计划和检查调用；续跑保存已完成调用及批准状态。 | 一个授权策略供各入口使用；恢复不能重新执行已确认完成的实验。输入 guardrail、工具授权、批准以及科学验收承担不同职责。 |
| [OpenHands ToolDefinition/ToolExecutor 源码](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/tool/tool.py)、[确认策略源码](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/security/confirmation_policy.py) | 结构化 Action → Executor → Observation；支持不同确认策略，包括 NeverConfirm；执行器有独立的执行和中断接口。 | 自动运行可以采用预授权，不必每个操作询问用户；调用结果、暂停、中断仍应有明确契约。 |

这些机制没有形成一份所有框架都遵循的统一权限标准。下面是结合本项目需求提出的规范，不是声称上述项目已经实现了 ProteinRSI 的实验、方法验证或标签隔离。

## 当前代码已经做对的边界

1. `src/proteinrsi/tools.py:ToolGateway.call` 检查调用白名单、任务兼容性、外发授权、输入 Schema、部分受保护字段和 Task artifact 可见性。它不是只有提示词限制。
2. `src/proteinrsi/replay/broker.py:dispatch` 控制 worker 的 RPC、角色、可读写命名空间和审计事件。worker 不能直接写实验测量、预算或方法发布记录。
3. `src/proteinrsi/replay/controller.py:run_replay` 把完整历史标签保留在 Controller；守护回放使用独立进程、Landlock/seccomp和能力 RPC。明确选择 in-process 路径时，不具备相同的 OS 隔离保证。
4. `src/proteinrsi/research/code.py:execute_code` 与 `code_worker.py` 限制现场 Python 的文件、网络、进程及资源访问。代码取得已揭示 TaskView 和明确输入，不取得 oracle 或 campaign 数据库。生成代码及结果会持久记录。
5. `src/proteinrsi/dataflow/resources.py:ResourceStore` 检查内容身份、Schema和当前任务/证据/方法 scope；计算资源的 `measurement_authority` 为 false。
6. `src/proteinrsi/improvement.py:apply_patch` 与 `runtime.py:_stage_patch` 只接受 W/M 的允许字段；方法程序仍在同一 Python 沙箱运行。`governance.py:adopt_unvalidated` 只在研究事先配置 direct 策略时发布，并标为未验证。

因此，前17轮新增 agent:* 步骤不能据此认定为获得新系统权限。新建一个 Schema 或角色名称，也不等于获得文件、网络或实验权限。

## 当前最主要的不清晰之处

**工具选择与授权语义混在一起。** `Workflow.tool_names` 既描述方法选择，也被转换成 allowed 列表。修改 W 时目前验证工具是否已经配置、是否支持任务，但没有一个独立表达“本研究事先授权哪些工具”的统一对象。应明确配置工具集、研究授权工具集和当轮选择集的关系；这是一项设计规范建议，不等同于已经证实存在标签泄漏。

**同类 Agent 操作的执行方式不同。** `dataflow/integration.py:propose` 内部包含 Designer 的多次工具调用循环；`dataflow/protocol.py:register_agent_operations` 注册的自定义 agent:* 主要是一次结构化模型回答，工具须另外安排。模型容易把“增加一个有输入输出 Schema 的步骤”误认为“增加一个可执行工具”。目录应明确暴露每种操作的执行能力。

**已有数据的引用、展示与执行输入没有形成足够稳定的使用方式。** 后台 Python 已能取得完整已揭示上下文；模型看到的长数据可能是分页引用。第18轮新增 resolve_current_panel 却要求模型取回所有排除序列并重输 task_context/candidates。这是在制造额外的传输和输出要求，不是读取未知标签。应该在工具执行前解析获授权的引用、注入真实数据，模型返回选择、分析或代码，而不是重建已有对象。

**错误恢复仍常转成流程重写。** `integration.py:review` 与 `protocol.py:ProtocolExecutor.execute` 能让 Planner 修改尚未执行的后缀。这很适合科学重新规划，但也被用于返回变量、缺失字段、候选集合身份等局部接口问题。前17轮的多次修改属于这类纠错。必须区分模型参数/代码错误与系统注入、资源完整性、未知执行结果和提供方错误，不能要求模型修复所有错误。

**执行检查与科学正确性需要分开表述。** `adapter:accept_checked_candidates` 确认实际执行过工具、候选身份一致、指定检查名返回通过；这不证明生成的检查代码本身正确。固定位置、合法字母、去重和查询额度继续由可信任务校验执行；模型编写的分组、配对和模型分析检查保留来源，并作为可检查的计算结果。

## 建议的最小规范

原子操作定义为一个注册、可独立识别的执行接口；它拥有明确输入、输出、允许效果、数据范围和恢复规则。“原子”不要求把一个 Python 程序拆成几十次 LLM 调用，也不保证外部服务具有数据库事务语义。

同一项授权至少回答三个问题：允许做什么、允许对哪些数据做、执行结果会改变什么。提示词描述用途，代码执行授权。

| 操作范围 | LLM 可决定的内容 | 执行侧必须保证的边界 |
| --- | --- | --- |
| 已揭示证据查询 | 查哪些变体、轮次、背景，是否需要历史细节 | 只能查询当前分支合法揭示的证据；未测返回 unknown/无记录，不触发新实验 |
| 现场代码与分析 | 算法、代码、派生指标、候选构造、科学检查 | 只读授权输入和artifact；只能写工作产物；不读取隐藏标签、密钥或直接改变主研究状态 |
| 蛋白模型工具 | 选择工具、参数、调用时机，或完全不调用 | 使用已配置且预授权的接口；凭据、部署路径由程序绑定；网络外发遵守研究授权；输出是预测证据 |
| 候选提交及实验 | 自主设计新候选，决定研究批次 | 校验任务约束、序列身份、重复策略、每轮容量和剩余查询额度；幂等提交后才揭示或接收实验值 |
| 方法改进 | 改 W/M 的prompt、策略、工具使用、记忆方法及沙箱程序 | 候选版本不可变；按冻结的 direct/validate 策略发布，在安全轮次边界切换；不能自行更改授权、已有事实或本次验收规则 |

LLM 可修改当轮计划、生成新的计算程序、定义科学中间格式和组合获授权的工具。新复合操作的效果只能来自所调用的既有接口；新名称、Schema、prompt或代码不能自行获得实验、网络或持久状态写权限。若将来允许生成代码直接调用工具，应通过同一受控桥接执行，不能把 Controller、完整 Store 或任意网络客户端交给生成代码。

实验查询权限与历史查询权限应使用明显不同的接口和返回语义。用户事先授予“20轮、每轮24个新候选”的回放权限后，合法实验可自动执行，不需要每轮再问确认。计算调用次数沿用当前用户选择，不额外设总次数预算；单次计算的超时、取消和输出容量仍属于执行可靠性约束。

每次调用共用一份记录：调用身份、操作/实现版本、模型参数、注入输入的引用、研究/分支 scope、授权策略身份、执行状态、输出引用、错误类型。外部调用完成情况未知时先核对，不能因修改了计划 hash 就重复提交实验。已确认结果恢复时复用；新候选生成和新查询使用新的调用身份。

对于恢复，最小分类即可：

- 无匹配的已测记录：正常结果，继续决策。
- 模型参数、输出格式或现场代码错误：返回具体可修复错误，修改局部调用/程序，再运行。
- 系统注入失败、资源丢失或scope不一致：明确报运行时问题，不让模型杜撰数据或放松边界。
- 已确认未完成的提供方临时失败：按提供方恢复策略重试。
- 副作用完成情况未知：保存 pending/uncertain 状态并核对，避免重复实验。
- 明确越权：拒绝该调用，解释可用范围；不得通过改名或改代码绕过。

## 落到现有代码的精简改动

1. 在现有工具执行入口旁定义一个冻结的研究授权对象，与 W/M 的选工具策略分开。工具目录、RPC、方法程序和验证分支调用同一个授权判断。`ToolSpec.capability` 当前主要是描述字段，应由执行侧解释，并补充必要的效果/数据范围信息；不另建第二套工具平台。
2. 保留 `OperationRegistry` 做编排、`ToolGateway` 做真实工具执行；统一操作目录的表达，明确“纯结构化回答、可请求工具、沙箱程序、状态变更”及所需输入。普通与自定义 agent:* 使用一致的可解释规则，修复两种执行方式的隐含差异。
3. 由资源绑定解析器和执行上下文注入候选、已揭示观测、排除集合等真实数据。把稳定字段隐藏在模型可填写参数之外；LLM 使用引用并生成代码，后台传完整获授权对象。固定的基础序列校验复用现有 `validate_candidate` 和 plate 完成逻辑；科学检查仍允许现场编写。
4. 把局部参数/代码修复与研究计划修改分开记录、分开路由。保留成功前缀和执行回执，恢复只处理未完成部分。M 可以把经证实有效的纠错方法纳入可复用版本；一次修好自己刚写错的程序，不自动成为能力提升证据。

不必先迁移 LangGraph 才能建立这些边界。如果后续迁移，LangGraph适合接管状态推进、路由和checkpoint；实验隔离、工具授权、科学证据身份和 W/M 发布规则仍由 ProteinRSI 自己实现。

## GB1 的具体例子

Planner决定围绕FWAA生成24个新变体，LLM可以直接给出编辑，也可以编写Python程序。后台向程序注入候选引用、已揭示数据和去重集合；程序输出新候选及必要计算产物。实验提交接口检查39/40/41/54位约束、唯一性、是否已查询和当轮额度，随后按预授权完成回放并记录实测fitness。Analyst更新工作记忆；Improver可提出降低重复检查或改进候选生成能力的W/M修改，按本研究冻结的策略发布。

这里不要求每轮都用蛋白模型、都重新读取全部历史、都增加一个检查Agent或都修改方法，也不会让LLM再次输出已存在的409条观测及完整任务对象。

证据索引：[前17轮流程审计](../runs/gb1-direct-r20-q480-20261013-144321/workflow-audit-r1-r17.json)。成功路径中的修改与后来放弃的尝试已分别记录；第18轮的失败不计入前17轮统计。
