# ProteinRSI

**有限实验预算下的蛋白科研团队：内环推进设计／预测，外环验证工作流修改，再对改进器本身进行独立验收。**

[English](README.en.md) · [研究执行层](docs/RESEARCH_RUNTIME.md) · [代码复用清单](docs/REUSE.md) · [本地工具](docs/LOCAL_TOOLS.md) · [ESMC-600M](docs/ESMC600M.md) · [评测](docs/EVALUATION.md) · [许可](THIRD_PARTY.md)

> **v0.5.0 是研究软件，不是已验证的自动蛋白设计产品。** 本版整合 v0.3 的本地工具与隔离环境，并新增资源选择、可修订的结构化计划和已揭示数据分析。没有 NIM 依赖，不需要启动 MCP 服务就能使用本地工具。默认演示仍是合成数值＋确定性角色；它不调用真实 LLM、蛋白模型或实验室。当前受限 RSI 修改的是提示与类型化策略，不执行任意自修改 Python。

## 整体架构与四个角色

| 角色 | 工作 | 实现 |
|---|---|---|
| A 科研负责人 | 选择相关资源、制定研究计划、根据实际结果修订未执行步骤、审核最终排序 | `agents.PrincipalAgent`＋`research/runner.py` |
| B 蛋白设计员 | 直接给出序列／编辑，或连续调用允许的设计工具 | `agents.DesignerAgent` |
| C 分析评估员 | 分析已知测量、调用计算工具、评价候选、解释新实验 | `agents.AnalystAgent`＋`research/analysis.py` |
| M 方法改进员 | 提出工作流补丁或自身策略的后继版本；不能自行验收 | `agents.MetaAgent`＋`improvement.py` |

四个逻辑角色可以共用一个对话模型。ESMC 是蛋白计算模型，不是对话 LLM。资源选择器、计划运行时、预算账本与验收器不是额外的科研 Agent。

![实验反馈驱动的自改进蛋白智能体架构](docs/assets/proteinrsi-architecture-zh.svg)

图为用户提供的研究架构，原样保留；跨任务迁移、湿实验与 RSI 收益仍需验证，不能从图推断已完成实验。

**三个层次不可混淆**：调整当前计划是内环；修改可复用工作流 `W` 并验证是系统自改进；改进器 `M` 产生经独立验收的后继并接管后续修改，才进入本项目的受限 RSI。`evidence_version`、`workflow_version`、`meta_version` 和每次 `research run_id` 分别记录。

## v0.5：LLM 自主选方法，而不是强制工具流水线

**配置模型只是让工具可用。真实 LLM 路径中，不再自动 ESMC 打分、提取特征或 Ridge 排序。**
A/B/C 可直接依据已知证据设计和排序，也可明确请求一个或多个工具；整轮零蛋白模型调用是合法结果。
注册工具、读取配置、构造缓存标识不下载权重、不启动模型进程。
`--agent deterministic` 是另外标注的脚本基线；配置了 ESMC 的确定性基线保留自动数值排序，不能与 LLM 路线混称。
`esmc-check --download` 是操作者主动检查，不是 Agent 隐式调用。

| 本次实现 | 入口与边界 |
|---|---|
| 提示词集中、可审计 | `src/proteinrsi/prompts/*.md`；初始内容快照进任务，W/M 的策略文本另外版本化。不是直接复制他人四角色提示词 |
| 显式预测工具 | `research_fit_predict` 自选 `mutation` 或 `esmc` 特征，只使用已揭示测量。没有明确预测产物引用时不允许填造数字 |
| 更完整工具契约 | 13 个蛋白工具都带适用/不适用条件、成本提示、示例和严格结果字段；模型推理与科学效能需独立验证 |
| GB1 母本冷启动 | `initial_observation_policy=parent_once`：先付费查母本，不增加轮数；第一轮余下23个（24上限）。后续无自动重复母本 |
| 预览与全集分开 | `candidate_access=catalogue` 允许提出预览外的可测序列；`library_check/sample` 仅返回无标签成员信息。提交前全目录验证 |
| 查询预算统一 | M 评测子运行通过 `SponsoredStore` 向主任务扣费，含初始测量披露、验证、LLM、工具及配置的模型输入；不另开免费账本 |
| 正式 replay 分离 | 默认 `--execution guarded`，完整 CSV 仅在可信控制端；科研代码在新解释器经 Landlock/seccomp 限制。网络/模型经受控 RPC。系统不支持则失败，不降级 |
| 完整报告 | 最好序列与突变、逐轮最好值、唯一变体/重复查询、工具请求用途、provider token 记录、版本和验证结果。未配置价格则成本金额为空 |

**兼容性：** 新语义必须新建任务。旧任务可 `status` 检查，但继续研究会被拒绝，不能在一次实验中途偷偷改变工具规则。
只改单个 JSON 文件不会改已有任务，数据目录、权重和密钥不进入 Git。

## GB1 最小启动

使用你已经准备的**无标签** task/library；不要把完整 fitness CSV 传给 LLM。
以下路径是本地示例，数据必须真实存在。脚本只读 task/library，不下载数据或查询标签。

```bash
# 先按实际 CPU/CUDA 安装 PyTorch，再安装本项目。
pip install -e '.[esmc]'
python scripts/prepare_gb1_run.py \
  --task /home/zjw/data/ssmula/agent_inputs/GB1/task.json \
  --library /home/zjw/data/ssmula/agent_inputs/GB1/library.json \
  --out runs/gb1-task.json --rounds 20 --queries 480 --batch-size 24

# 不需要先启动 NIM/MCP。
proteinrsi init --task runs/gb1-task.json --out runs/gb1-v05 \
  --protein-config examples/esmc600m.json --research-config configs/research.json

# 操作者选择使 ESMC 可运行：显式下载/预热；模型也可以整轮不被请求。
proteinrsi esmc-check --campaign runs/gb1-v05 --download
set -a; source .env; set +a
proteinrsi sandbox-check
proteinrsi replay --campaign runs/gb1-v05 \
  --dataset /home/zjw/data/ssmula/processed/GB1/measurements.csv --agent llm
```

先用 `--rounds 1 --queries 12 --batch-size 12` 创建不同的调试任务，再运行正式20轮。LLM/工具额度在准备脚本参数中显式给定，不承诺自动适配付费预算。
`--execution inprocess` 仅用于明确可信的开发调试，不满足 OS 隔离要求，不能因为缺少内核功能就悄悄使用。
说明：[回放隔离](docs/REPLAY_SECURITY.md)、[提示词](docs/PROMPTS.md)、[工具契约](docs/TOOL_CONTRACTS.md)。

```bash
proteinrsi prompts --role designer
proteinrsi prompts --campaign runs/gb1-v05 --role analyst
proteinrsi status --campaign runs/gb1-v05
```

## 保留的 v0.4 研究执行能力

**资源选择。** 从已授权且可用的工具、当前可见数据、工作流 Skill、初始 Know-how 和当前作用域经验中选择资源。默认使用轻量排序；`resource_selection=llm` 才调用预算内的 LLM 检索。选择不扩大权限，不读取隐藏标签，不安装新工具。文档和工具描述都是参考数据，不是更高权限的指令。

**结构化计划。** A 可以安排“先分析→再设计”，也可在分析后再次设计。计划包含假设、步骤、依赖、问题和预期产物；执行器记录实际负责人、完成状态、输出引用及修订历史。只能修改未执行部分；最终提交必须经过设计、最新候选池的 C 排序和 A 审核。每个完成步骤持久化。已失败／状态不确定的执行明确阻塞，不静默改用假结果。

**只读实验分析。** 增加 `research_evidence_summary`、`research_prediction_errors`、`research_combination_effects` 三个上下文绑定工具。它们不能接收隐藏标签、任意文件路径或自报实验值，只访问当前 `TaskView`。误差分析使用当初送测时冻结的预测；组合效应需显式选择 linear/log 标度，默认不跨实验批次混合，缺母本或组成单突变实测就报告缺失。这些是描述性分析，不自动校正测量，也不是显著性证明。

**知识与经验分离。** Know-how 是初始方法说明，Skill 是操作方法，Method Experience 是有验证记录及适用范围的方法修改。Know-how 在任务初始化时保存内容快照。当前经验主要在任务内记录／检索；跨蛋白传递和收益仍需专门协议，不宣称已解决。

## 实际复用与借鉴：不是把多个仓库直接嵌套

| 来源 | 类型 | 具体复用／借鉴 | 本项目位置及边界 |
|---|---|---|---|
| **Biomni** | **有限的源码改编＋架构借鉴** | 改编 `ToolRetriever` 的分类检索与资源格式化；借鉴计划—执行—观察和 Know-how 思路 | `research/biomni_retriever.py` 为 **Apache-2.0**；它在 `resource_selection=llm` 路线被调用。预算化 JSON 客户端、索引校验和权限预过滤是本项目修改。没有引入 A1 主类、全局 REPL、全部工具或 E1 环境 |
| **ProteinMCP** | 架构借鉴，**未复制源码** | 一模型一环境、工具注册、Skill 组织 | `localtools/` 是原创函数／描述／Worker；不依赖 Claude Code 或其 MCP 服务管理器 |
| **LangGraph** | 可选库/API 复用 | StateGraph、SQLite 检查点、批准／结果中断恢复 | `integrations/langgraph.py`；复用同一个 `Campaign`，不是第二套实验循环 |
| **GEPA** | 可选库/API 复用 | `evaluate`、反思数据接口与优化入口 | `integrations/gepa.py`；需要操作者提供受控 evaluator 和反思模型，候选必须另过 gate；未自动取代 M |
| **Virtual Lab** | 可选库/API 复用＋角色思路借鉴 | 调用上游 `Agent` / `run_meeting`，参考 PI／专家分工 | `integrations/virtual_lab.py` 是显式可选讨论，不是默认 A/B/C 的隐藏依赖 |
| **Transformers / ESMC** | 实际模型/API 复用 | 原生 ESMC-600M 加载、掩码打分和特征 | `protein/esmc.py`；权重另行下载并固定版本，模型不是本项目训练 |
| **ProteinMPNN / RFdiffusion / Protenix / PyRosetta** | 部署后调用上游原生程序/API | 逆折叠、骨架生成、结构预测、可选物理分析 | `localtools/functions.py`＋`worker.py`；不重写模型，安装、许可与权重另行准备，新增引擎尚未端到端实测 |
| **ProteinSwarm** | 架构参考，**未复制源码** | LLM 直接序列编辑与反馈的思路 | 不宣称复现其实验、残基群体或设计收益 |
| **HyperAgents / ADAS** | 架构参考，**未复制源码** | 后继改进器／工作流搜索 | 本项目是原创的受限策略演化，未包含 HyperAgents 非商业源码 |
| **ALDE / SSMuLA / EVOLVEpro** | 方法／评测参考，**不是代码依赖** | 实测反馈、主动学习和蛋白特征＋轻量预测器的分工 | 自带 Ridge 是原创 NumPy 基线，不是 ALDE/EVOLVEpro；真实数据由用户依法提供 |

Biomni 源码审读版本固定为 [`400c1f3`](https://github.com/snap-stanford/Biomni/tree/400c1f366b96a35ca253e13c9b06c5076af41d65)。源文件、修改内容、许可和验证边界见 [REUSE](docs/REUSE.md)。不复制论文性能数字，不声称当前仓库等同于 Biomni 正刊使用的确切代码版本；也不将它的单次运行 `self_critic` 称为本项目的 RSI。

## 安装与离线演示

Python 3.11+，Linux/macOS，Windows 使用 WSL。代码包不包含模型权重、实验数据和密钥。

```bash
cd ProteinRSI
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# 新增研究运行时，角色与数据仍明确为脚本/合成
proteinrsi demo --adaptive --out runs/research-demo --rounds 3
proteinrsi research plans --campaign runs/research-demo/campaign
proteinrsi research resources --campaign runs/research-demo/campaign
proteinrsi research analyze --campaign runs/research-demo/campaign
pytest -q
```

不带 `--adaptive` 的 `demo` 保留原固定流程，可做工程对照；不能把两条脚本流程的演示差异当作 LLM 协作收益。输出含 SQLite、批次与结果模板、资源快照、研究计划、逐步输出和 `report.json`。不会强制让补丁被采纳；结果可以是 `inconclusive`。

## 配置入口

| 文件 | 作用 |
|---|---|
| `.env.example` | A/B/C/M 对话 LLM 的模型、BASE_URL 和密钥模板 |
| **`configs/research.json`** | 资源选择模式、上下文额度、计划步数、修订次数和执行上限 |
| **`configs/protein_tools.json`** | 本地重型模型环境、资产、版本与执行限额；新增引擎默认禁用 |
| `examples/esmc600m.json` | 单独使用 ESMC 时的设备、精度、批量和输入限额 |
| `examples/wetlab_task.json` | 母本、允许位点、任务目标、测量协议与预算 |
| `examples/workflow.json` / `workflow.binder.json` | W 的提示、策略、工具及 Skill 白名单 |
| `examples/meta_policy.json` | M 的初始受限策略 |

`init` 保存配置；修改样例文件不会热更新既有任务。**新 CLI 任务默认 adaptive＋ESMC可用（不强制调用）；未传 research_config 的 Python API 保持固定路径。v0.5 拒绝继续旧版本创建的任务，请保留旧程序或新建任务，避免悄悄改变实验语义。** 用 `--research-mode fixed` 创建固定流程基线；用 `--protein-model none` 创建不加载蛋白模型的基线。协议和权限不能由 M 修改。

```bash
# 首先根据机器安装相应 CPU/CUDA PyTorch，再安装 ESMC 依赖
pip install -e '.[esmc]'
# 将任务样例改成自己的序列、位点、指标与已审核实验协议
proteinrsi init --task examples/wetlab_task.json --out runs/protein \
  --protein-config examples/esmc600m.json --research-config configs/research.json
proteinrsi esmc-check --campaign runs/protein --download

# 编辑 .env 后手工加载；程序不会自动 source 文件
set -a; source .env; set +a
proteinrsi step --campaign runs/protein --agent llm
```

`PROTEINRSI_MODEL` 填对话模型，**不要填 ESMC**。客户端支持 Chat Completions 与 Responses JSON 接口，用 `PROTEINRSI_API_PROTOCOL` 选择（默认 `chat_completions`）；`PROTEINRSI_REASONING_EFFORT=medium` 设置推理强度。配置 `PROTEINRSI_CODEX_AUTH_FILE` 可读取本机 Codex `auth.json` 的 `OPENAI_API_KEY`，无需复制密钥到 `.env`。第三方 HTTP 地址需要显式设置 `PROTEINRSI_ALLOW_HTTP=true`。LLM 出错明确失败。开启 `resource_selection=llm` 会消耗同一个 `llm_calls` 预算，默认 rules 不额外调用检索 LLM。

## 蛋白工具与独立环境

本版保留 **13 个蛋白／结构／MSA 工具**、**3 个任务上下文分析工具**，再提供显式 `research_fit_predict` 与 `library_check/sample`。ESMC、ProteinMPNN、RFdiffusion、Protenix 与可选 PyRosetta 各司其职，所有任务不必运行全部模型。

```text
Agent → 类型化工具函数 / 独立描述 → 白名单、预算、产物校验
      → 模型独立 Python 环境或可选容器 → 上游原生程序 → 结构化结果
```

```bash
proteinrsi tools list --config configs/protein_tools.json
proteinrsi tools describe --name proteinmpnn_design
python scripts/setup_local_tools.py --engine proteinmpnn --prefix /opt/proteinrsi
# 先阅读计划，确认后才加 --execute。环境/权重/许可完成后生成 tools.local.json。
proteinrsi tools doctor --config tools.local.json --probe --strict
proteinrsi init --task examples/wetlab_task.json --out runs/isolated \
  --local-tools tools.local.json --research-config configs/research.json
```

`tools list` 列的是模型工具部署目录；上下文分析／预测／目录工具在运行时绑定当前可见数据。ESMC 也可使用 `configs/esmc600m.isolated.json` 指定独立解释器。MCP 仍为兼容已有外部工具的可选通道，但本地路线不需要它。**独立环境隔离依赖，不等于操作系统安全沙箱。** 本版不开放任意 `exec()`／模型生成 shell。

## 多轮实测反馈与双闭环

```bash
proteinrsi step --campaign runs/protein --agent llm
# 人工检查批次、序列和预算
proteinrsi approve --campaign runs/protein --batch YOUR_BATCH_ID --operator YOUR_NAME
# 实验室完成测量，保留模板样本身份、单位、来源和协议
proteinrsi import-results --campaign runs/protein --file /path/to/results.csv --agent llm
proteinrsi step --campaign runs/protein --agent llm
```

只有新实测导入后才开始下一轮实验反馈；一次湿实验前的多步计算不是多轮湿实验。批准／提交名额计费，不能通过回滚 W 退款。导入一个批次的所有最终状态，失败／无结论不是零；相同结果重复导入不重复计费。人工 CSV 只是集成接口，软件不能自行证明其真实性。

历史数据使用 `scripts/prepare_replay.py` 准备，再运行 `proteinrsi replay --dataset ...`。源数据不附带。隐藏标签只由受控揭示器读取，**资源检索与分析工具没有“免费查真实标签”的通道**；查询式回放仍不是新湿实验。

工作流补丁先经类型、权限和可用性检查，再在后续相同名额双臂试用。Meta 补丁由 `proteinrsi evaluate-meta --cases ... --promote` 独立比较后继工作流；相同研究配置、Know-how 内容和固定 ESMC 快照传给双方，且所有子运行实际消耗都扣主研究账本。初始标签披露也收费，没有待验收 Meta 补丁会拒绝。最终测试不能选版本。涉及外部结构引擎的通用 Meta 验收仍被阻止，需要另行准备任务范围内的结构产物与评价器。探索性 bootstrap gate 不是持续多次搜索后仍有效的统计证明。

## 当前没有完成的能力

本次运行环境不提供 Landlock 系统调用；已测试默认失败退出、RPC权限检查，实际受限worker测试有明确跳过标记，需在支持的 Linux 上运行。没有新增重型引擎的真实端到端／GPU 验证，没有付费 LLM 端到端运行，没有新湿实验或 RSI 科学收益证明。定量蛋白—蛋白亲和力、完整全原子质量验证、跨蛋白持续经验迁移和单 Agent／多 Agent 科学对照仍需专项工作。历史 ESMC CPU 推理记录不能替代本版新功能验证。分析算子为确定性白名单函数，不是自由数据分析代码沙箱。

## 许可与测试

原创核心代码、测试与文档继续采用 **MIT**。`research/biomni_retriever.py` 的改编部分采用 **Apache-2.0**，完整原许可在 `licenses/Biomni-Apache-2.0.txt`，出处和修改记录在 `NOTICE`。分发包元数据写作 **`MIT AND Apache-2.0`**，表示并存文件义务，**不是任选其一的双许可**。其他依赖、权重、数据库、PyRosetta 分别遵守其条款；未重新许可它们。

```bash
pytest -q --cov=proteinrsi --cov-report=term-missing
python -m compileall -q src
python -m build
```

实际执行结果与未运行项见 [TESTING](docs/TESTING.md)；版本变化见 [CHANGELOG](CHANGELOG.md)。本次发行内容不包括用户研究 PDF、私有实验数据、密钥或模型权重。
