# 类型化数据交接与可选研究协议

本补丁基于 `0c6254674455c4c013cd9d56e631ac7bfa8d36e0`。它不改写、不恢复历史实验，也不改变查询、整板、缺测或标签访问规则。

## 已实现的两个层次

**默认设计路径的修复**已经接入 `DesignerAgent.propose()`：突变使用 `MutationEdit`，成功工具结果注册为候选集资源；B 通过 `candidate_refs` 采用结果，不再必须抄写整批序列。合法的旧 JSON 仍能使用；自创的 `position/from/to` 合并字段不被猜测转换，而是反馈结构化错误，最多修复两次。修复次数持久化，重启不重置额度，调用仍经过原 LLM 客户端计费。

**新的自然语言任务默认使用通用协议执行器**。`start` 保存 `ResearchConfig.protocol_mode="typed"`，`ResearchRunner` 调用 `dataflow.integration.run_campaign_protocol()`。`start --protocol-mode legacy` 可显式选择旧流程；底层 ResearchConfig 缺省仍为 legacy，以保持已保存配置与 init 的兼容性。已完成一板的旧任务不会被自动迁移。

```text
目标 / 任务约定
    → A 选择已有操作、输入引用及结果类型，生成 Protocol
    → 检查依赖、Schema版本、参数和最终输出契约
    → 执行 Agent / 原子工具 / 明确适配器
    → 验证并保存不可变资源
    → A 查看实际结果，保留或修订未执行后缀
    → 任务边界校验最终资源
    → 原 Campaign 处理反馈、预算、方法验收与下一轮
```

## 使用

只在已有配置完整的新任务上启用：

```bash
proteinrsi init --task /path/to/operator-task.json --out runs/typed-new \
  --research-config configs/research.protocol.json \
  --protein-config examples/esmc600m.json
```

计算任务不需要蛋白模型时，使用 `--protein-model none`，不要传 `--protein-config`。保留原有的模型环境配置和工作流工具白名单；本功能不安装模型、不扩大白名单。

自然语言 `start` 默认启用 typed，启动摘要显示实际协议模式。`--prepare-only` 只保存任务，不执行研究协议。澄清过程保留第一次选择的模式；旧澄清记录没有协议字段时保留 legacy。已有 init 入口仍可通过 `--research-config` 显式选择 typed，旧路径同样获得设计数据交接修复。

`configs/research.protocol.json` 使用 LLM 资源选择和有界协议规划。`enable_generated_code=true` 仅表示可配置原有沙箱代码功能；调用仍须同时满足工具注册、白名单和系统隔离条件。未请求任何蛋白工具的运行合法，不自动执行 ESMC/Ridge。

## 一份数据只保存一次，后续传ID

`ResourceStore` 位于 `dataflow/resources.py`，复用已有 `research_step_outputs` 存储命名空间和受限 worker RPC，不开放整个数据库。资源包含类型、Schema哈希、内容、来源、父资源和任务/证据/工作流作用域。

- `resource:<sha256>` 标识不可变的数据及来源；不是路径，不是可修改别名。
- `seq:<sha256>` 标识完整序列。类型化序列资源与现有排序接口的短 ID 在适配器边界转换。
- `artifact:<sha256>.<kind>` 继续标识原有结构或大型文件。结构资源必须引用已经登记的文件并显式表达链/实体映射。原生工具在读取时另做内容哈希和科学约束检查。
- 资源ID及声明的来源不构成真实测量证明；所有本层输出均无实验测量写入权限。

原生 Python/模型返回 `candidates` 后，设计交接层保存完整结果，给 B 的是候选集ID、数量及有限预览。B可请求新操作，也可返回 `candidate_refs` 和空工具请求。旧 runner 接收的仍是程序解析后的 Candidate 列表，实验接口不需要跟着改。

对第一次直接设计，允许完整序列或严格的突变编辑；资源机制不禁止LLM提出新序列，只避免重复转述已有数据。

## 显式任务预测的证据交接

C 只能附加本次实际取得的 `research_fit_predict` 工具产物。`Candidate.prediction_ref`
保存不可变的 `task_predictions/<sha256>` 引用；读取时校验内容哈希、完整任务/轮次/
证据/工作流/请求作用域和序列身份，任务边界还核对指标与单位。数值始终由可信工具
产物恢复，不接受模型在序列资源中填写预测数值。

`agent:rank` 发布带证据的候选集，而不是只对原始候选集排序。现有
`protein.sequence_set/v1` 数据 Schema 保持不变；资源的不可变元数据
`prediction_refs` 保存 `seq:<sha256>` 到预测产物的映射。序列/排序父资源的显式
绑定把映射传递到最终候选资源，随后 A-selection 和实验批次保留准确预测。
冲突引用、篡改数值和其他作用域的引用会被拒绝；未附加预测的候选继续为空值。

批次中的数值和引用在测量之前冻结。反馈误差读取冻结数值，按原批次证据和工作流
校验证据，而不是使用反馈后的新拟合值。预测仍为未校准代理证据，没有测量写入权限。
旧批次、旧序列资源不会被回填或修改；新的预测工具与排序适配器使用新的实现版本，
避免复用旧的丢失证据的步骤收据。

## 任务不同，最终资源可以不同

`dataflow/tasks.py` 定义任务边界，而不是让通用执行器根据蛋白模型名字分支：

| 当前任务适配器 | 必要结果 |
|---|---|
| 变体设计 / Binder 设计 | `candidates: protein.sequence_set/v1`；可附结构、评价等其他结果 |
| 给定序列排序 | `ranking: protein.ranking/v1`，必须完整保持输入候选身份，无须先设计或设置母本 |
| 固定输入亲和力预测 | `estimates: analysis.estimates/v1`，不伪造新序列；没有有效数值证据时应返回空值和限制 |

定量预测必须引用同对象、同性质、同单位、同数值及同方法的实际工具结果；这只证明结果交接一致，不证明预测器经过校准。模型代理分数不能因此变成实测。

计算迭代在 `computational.py` 中保存 `protocol_results` 并传入下一轮历史，亲和力等不返回新序列的任务不会丢掉结果。实验任务仍必须经过原控制器，没有在协议里增加可绕过预算的“测量工具”。

为兼容现有启动入口，根 `TaskSpec` 和 `TaskKind` 本次保留；不是已经支持任意新任务名称。新增任务仍需注册输入/最终结果适配器和必要的领域校验。该扩展不要求修改通用协议执行器。

## 协议如何连接输入输出

每个步骤有 `operation`、`arguments`、`bindings` 和可选 `outputs`。`operation` 是注册表中的能力，不限制为五个业务动作；A/B/C是角色能力而非强制的数据形状。

```json
{
  "source": "step:backbone.result",
  "schema_ref": "tool.result/实际工具名/实际版本哈希",
  "pointer": "/backbone_ref",
  "delivery": "value"
}
```

这表示把**实际完成的上游结果**的 `backbone_ref` 传给下游参数，LLM不再手工复制路径。该片段仅说明连接语法；真实 Schema ID 来自运行时工具目录，不能照抄占位符。

`delivery="ref"` 把整个资源的引用交给可读取资源的适配器，`delivery="value"` 由程序解析内容。使用有限的 JSON Pointer，不执行表达式、不隐式转换单位、不猜链、不从相近字段名推断含义。

每个原生工具的输入/输出类型直接由现有 `ToolSpec` 注册产生。协议不能重定义 `result` 类型；额外的命名输出视图也必须通过其声明类型的运行时验证。不兼容的数据需要明确适配器，例如 `adapter:normalize_candidates`、`adapter:sequence_arguments`。

LLM可以在 `custom.*` 命名空间定义有界的中间JSON Schema，并创建 A/B/C 执行的自定义步骤。已有版本不能被重新定义；远端Schema、递归Schema、动态引用与自定义正则验证器被拒绝。不接受模型提交的验证器Python代码。

预检只验证名义类型/版本、引用、依赖、必填字段与可用操作；它**不是任意 JSON Schema 子类型证明，也不是科学有效性证明**。实际值、科学约束、文件完整性继续在执行边界验证。

## 修复和恢复

- 设计格式：同一步的有界 LLM 修复；不会修改母本、固定残基或测量记录。
- 协议格式/连接：无效 JSON Schema 统一转为结构化 ContractError，A-plan 有界修复后才能运行；A-review 只能改未执行步骤。
- 执行期输入/输出映射错误：反馈给 A-review 修复未完成后缀；成功工具收据保留。修复次数跨恢复累计，不能通过重启重置。完成状态不明的调用仍暂停，不自动重发。
- 同一轮的整板补齐请求具有独立资源作用域，不会重复返回上一面板的缓存候选。
- 操作成功后，原始结果先形成独立收据；下游字段映射有误不会删除收据。修复映射后复用结果，不重新运行GPU工具。
- 失败/不确定完成状态保留且阻塞；不把网络或进程故障冒称格式错误，不盲目重提交。
- 每一步和最终结果验证失败均记录阻塞状态。LLM和工具费用仍由既有客户端/网关管理。
- 跨任务、证据或工作流作用域的引用拒绝直接读取；需要显式经过任务边界导入，不能把测试标签当作资源传递。

## 目前没有实现的范围

支持顺序执行的有向无环数据流，以及有界的未执行后缀修订；跨轮反馈继续由Campaign处理。没有任意嵌套循环、并行GPU调度或模型自动安装。条件决策由A根据已产生资源修订后缀表达，尚无独立的通用条件分支语言。

M的已有W/M提示与策略验收保留，可影响后续协议规划；**尚未开放任意协议拓扑/Schema/代码的持久M补丁验收**。最终任务指标、约束、预算和实验接口不可由本协议重写。不要把本次数据流升级表述为已证明更强的科学RSI。

真实模型、GPU 和湿实验仍需单独验证；本次部署已验证目标机隔离中的人工回放与计算循环。测试结果及来源界限见 [DATAFLOW_VALIDATION](DATAFLOW_VALIDATION.md)。实际复用说明见 [DATAFLOW_REUSE](DATAFLOW_REUSE.md)。

## Final adoption in typed design dialogs

`agent:propose` uses `typed-designer-v3-final-selection`. Its published sequence
resource consists only of candidates resolved from B's final response with no
further tool calls. Earlier valid tool outputs and provisional B responses are
retained as evidence, not automatically appended to the submitted panel. Select
every desired `candidate_ref` explicitly; multiple refs retain requested order
and deduplicate identities. An empty final selection is a contract failure, not
a request to fall back to an earlier panel. Within-request format-repair retention
remains governed by its existing explicit `repair_state` contract. Legacy
cumulative-pool runners are outside this typed-operation change.

The implementation version prevents a fresh corrected operation from reusing an
old proposal-step receipt. It is **not an in-place migration** for existing draft
plates, pending batches, protocol journals or review decisions. Those stores may
already contain materialized candidates independently of the operation cache.
Do not delete caches, refund measured wells, rewrite history, or silently replay
an affected draft under changed semantics. Use a genuinely fresh planning scope
or an explicitly reviewed transition at a clean round boundary, preserving old
source identities, receipts, observations and all budget charges. Frozen metric
plans retain their exact-runtime checks and must not be migrated implicitly.
