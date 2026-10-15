# 科研执行接口与恢复

本次修改在已有 TaskSpec、OperationRegistry、ResourceStore 和持久化执行器内完成。
没有增加第二套工作流框架。LLM 仍负责提出科研问题、定义中间 Schema、编写沙箱代码、
选择可选工具和修改方法；控制端负责资源身份、权限、实验预算与方法采用。

## 数据交接

```text
冻结任务与可见证据 → A 定义步骤与输入输出
→ 校验输入绑定 → 执行 Agent / 工具 / 代码
→ 保存原始返回 → 草稿适配与语义校验 → 登记资源 → 下游引用
→ 终态任务校验 → 实验控制器提交与揭示 → 下一轮
```

- `SequenceProposal` 表示模型可撰写的内容：候选、突变编辑、已提供的候选资源引用。
  `Design` 在其上增加内置设计器的可选工具请求和重新规划信号。
- 内置设计器及自定义 `protein.sequence_set/v1` Agent 都复用 `resolve_design` 的任务约束。
  内置及自定义操作均可返回工具请求，在原步骤内接收结果，再交付科学输出。
- `materialize_sequence_proposal` 根据任务母本应用编辑，或接受新序列/输入引用；
  `ResourceStore.sequences` 计算候选 ID。没有 GB1 位点或母本常量。
- 其他自定义科学输出继续使用声明的 Schema。已有工具原生结果和显式适配器继续使用。
- `ResourceStore.validate` 统一 JSON Schema 与已注册模型的语义检查，失败返回 `ContractError`。
  排序还检查所引用序列资源的作用域和候选成员资格；结构仍须引用已登记文件。
- 资源合法不等于科学有效，不代替面板配对检查、实验实测反馈或 E 的方法验收。

## 错误和回执

| 情况 | 行为 |
| --- | --- |
| 模型回复格式或领域契约不合法 | 保存原始回复及字段错误，限定次数修复同一输入；自定义 Agent 和 M 共用此设施 |
| 合法回复，包括 M 不修改 | 接受并缓存，不因为结论不理想再抽样 |
| 工具/步骤已返回，发布或输出校验失败 | 保存 `returned` 回执及原始返回；恢复时复用返回，只重做校验/登记 |
| 步骤只留下 `started` | 完成状态未知，禁止自动重复执行，需核对 |
| Python或工具参数明确报错 | Executor保留失败attempt，局部修复原调用；成功后才登记步骤输出 |
| 科学检查返回passed=false | 保留真实计算结果，修改候选或科学计划，不伪造成代码修复 |
| 权限不足、输入scope不一致、预算不足 | 保留失败，不能用格式重试扩大权限、数据范围或预算 |
| E 裁决 | 保留既有冻结证据、仅修格式、有效裁决不重抽机制 |

原始模型尝试在 `research_step_outputs` 的 `/response/<attempt>` 记录中；
工具原始返回在 `tool_jobs.raw_result`；协议回执在 `research_step_outputs` 的
`protocol-step:*` 中。现有轨迹导出包含这些命名空间。
`returned` 表示执行返回已持久化，不表示输出已经被认可。已有的历史失败记录不会被改写。
计算返回到原始回执落盘之间仍存在无法原子覆盖外部工具的崩溃窗口；这类情况必须核对，
系统不承诺外部副作用的 exactly-once。

## 上下文与状态

`evidence-v1` 的内部策略修订号为 4，参与请求身份并写入上下文记录。
大型命名目录提供有大小上限的名称列表，支持 `read(ref, name)`；字典目录按精确键名读取，仍支持分页。
成功读取的页面保留到模型显式 `release`；容量不足拒绝新页面，不静默移出旧页面。
单页/完整上下文大小守卫仍有效。按名称读取操作返回精简执行契约及完整规格引用。
释放页面不删除原证据，也不允许读取隐藏标签或其他决策的引用。
超大目录仍需分页；这降低重复读取，不保证模型一定以最少调用完成任务。

`report`、JSON 轨迹和 HTML 轨迹分别显示科研 `status` 和 `execution.state`。
后者依据审计事件识别阻塞；`in_progress_or_interrupted` 不声称进程仍存活。

## 验证与使用边界

回归用例覆盖自定义 GB1 草稿（假 ID、越界编辑、正确编辑）、binder 草稿、
未知候选排序、登记中断后恢复、M 格式修复和不修改缓存、工具返回复用、
按名称读取及跨决策访问拒绝、100 轮证据上下文大小和执行状态。
测试使用本地脚本化模型与工具，不代表蛋白设计质量或真实提供方可用性。

新接口使用独立小规模真实 API 回放验证。旧研究冻结了执行代码和提示词，应使用原版本复现；
验证新接口应新建研究目录，不能把新代码静默混入旧实验。

2026-10-11 最终验证：`PYTHONPATH=src python -m pytest -q`，962 passed、5 skipped，
耗时 517.06 秒；`python -m ruff check .` 和 `git diff --check` 通过。
跳过项为当前控制器环境未安装的 torch、gepa、langgraph、mcp，以及未启用的真实 ESMC600M 测试。


## 规划与修复的职责

- A 在 `Step.question` 写一次科研意图；仅显式登记了 `question_argument` 的适配器自动接收。
  原生工具参数仍遵守工具契约，旧协议中的显式参数继续有效。
- Binding 可以只写 `source`；执行器依据输入资源或上游端口推导类型。
  显式 `schema_ref` 仍作为断言检查；自定义输出视图仍须声明目标类型。
- A-plan / A-review 首次提交草稿；格式或预检失败后只返回局部 JSON 编辑。
  控制器保存每次原始回复、修复前草稿、累积错误，原子应用编辑后重新校验完整协议。
  修复不会改写已完成步骤、突破资源作用域或增加实验权限。
- M 决定是否提出方法修改，E 按冻结计划验收。合法的“不修改”和“不采用”都不是格式错误。


## 提供方消息边界

Responses 的 assistant 消息保留在 `llm_attempts.provider_messages`，包括原始 phase。
解析器排除显式 `commentary`，只接受一个完整的最终消息（兼容单个未标 phase 的旧消息）。
多个可能答案、拒绝、不完整消息仍明确失败；不从散文提取 JSON，也不重新抽样有效裁决。
消息内的说明加 JSON 若没有协议边界，仍属于无效返回。已停止研究不改写、不跨源码版本续跑。
协议依据：[OpenAI Responses phase](https://developers.openai.com/api/docs/guides/reasoning)。


设计器交接显式提供 `design_dialogue.tool_rounds_remaining`。每个设计步骤的最后一轮
为 finalize：停止提供可调用目录，回复 Schema 限制 `tool_calls=[]`，运行时再次校验；
只能完成候选或通过 `replan_reason` 交回 A。局部步长和全局 LLM/工具总预算是不同概念。
此兼容步长仍用于旧研究；新研究的授权动作循环按实际完成推进，不设计算调用总次数。

## 后台输入与共享执行（2026-10-14）

`execution.py` 共用内置设计器及自定义Agent的动作/结果循环，并承担局部参数、代码纠错。
`permissions.py` 在研究初始化时冻结工具授权，验证分支继承同一份授权；W/M只选择方法。
Executor纠错使用当前配置模型的 `B-repair` 角色，不增加新的Agent或模型依赖。

协议可以写 `bindings["resource_refs.panel"]={"source":"step:design.result","delivery":"ref"}`。
下游Python直接读取 `inputs["panel"]["items"]`；完整已揭示TaskView仍在 `context`。
后台检查资源及Schema身份和当前证据scope，禁止同名literal覆盖，不要求LLM读完再抄回。
新协议不需要数据搬运Agent。现场修复、当轮计划调整、W/M提案和方法发布分别记录，TXT轨迹
标明原步骤失败与局部修复，既有成功结果和实验提交身份保持可复用。

旧研究不添加授权、不改写快照。源码变更后应新建研究；未完成旧研究仍需使用原始执行快照。


M 的 `patch_constraints` 来自控制器冻结的 GovernancePolicy；沙箱仅可读取这份配置。
提案前置校验与控制器暂存共用 `GovernancePolicy.validate_patch`。超限字段、错误版本等
契约错误携带原提案进入有界修复；合法提案不会因科研结论不理想而重抽。
拒绝记录保留原因，新增原因文本不改变原有“同轮同类失败只计一次”的幂等身份。

完整真实验证与限制见 [接口整理验证报告](INTERFACE_REFINEMENT_VALIDATION.md)。
