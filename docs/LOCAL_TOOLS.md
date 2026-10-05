# 本地蛋白工具：函数、描述、隔离环境（v0.3.0）

本版不使用 NIM，不要求把本地模型架成 HTTP/MCP 服务。标准调用路径是：

```text
Agent 或 protein-tool
  → ToolGateway（Schema、任务保护字段、白名单、预算、缓存）
  → localtools/functions.py（科学输入转换与输出验证）
  → engine 专用 Python 子进程 / 可选 Docker
  → 上游原生程序 / Python API
  → 结构化结果、科学文件、来源与运行时间
```

**适配实现与环境部署不是一件事。** 本版提供函数、描述、配置模板、安装步骤、
环境检查及离线测试；不附带 ProteinMPNN/RFdiffusion/Protenix/PyRosetta 的运行环境、
权重或许可证。新增模型没有在本轮完成真实权重推理。模板中的禁用状态不能当作已部署。

## 一个配置入口

`configs/protein_tools.json` 是统一模板，包含：

- `esmc`：ESMC-600M 模型配置及专用解释器 `worker_python`。
- `utilities`：无需模型的序列／结构／MSA检查工具。
- `engines.proteinmpnn / rfdiffusion / protenix / pyrosetta`：各自的解释器、源码、
  资产目录、源码提交、资产哈希、许可确认、超时和输出限额。

示例路径采用 `/opt/proteinrsi/...`，需要改成实际路径。源码提交固定在
`configs/sources.lock.json`。这些提交是核查接口时读取的上游版本，不代表在所有
GPU/驱动上验证过。`enabled=false` 是安全默认值，不是空壳计算后端；开启后必须
通过真实环境检查，缺文件／缺许可／版本不匹配会直接失败，不返回假结果。

ESMC 的旧配置 `examples/esmc600m.json` 和 `--protein-config` 保持可用。
首次初始化决定环境，之后修改配置文件不会热修改已存在的实验。

## 安装与检查

主环境可以只安装轻量控制层和 Hugging Face 下载客户端：

```bash
pip install -e '.[esmc-client,dev]'
```

为 ESMC 单独创建环境。下面脚本默认只打印计划；明确加 `--execute` 才下载和安装。
在无 `/opt` 写权限的机器上改用自己的前缀，并相应修改统一配置的路径。

```bash
python scripts/setup_local_tools.py --engine esmc600m --prefix /opt/proteinrsi
python scripts/setup_local_tools.py --engine esmc600m --prefix /opt/proteinrsi --execute
```

其它引擎同理，分别执行 `--engine proteinmpnn`、`--engine rfdiffusion`、
`--engine protenix`。RFdiffusion 使用上游 SE3nv 环境文件，需提前安装 micromamba；
其旧依赖可能需要针对机器适配。PyRosetta 必须使用合法取得的发行版手动安装，
脚本不会下载它或自动接受许可证。

安装代码后还要准备资产：

| 引擎 | 资产位置与动作 |
|---|---|
| ESMC | 任务初始化后执行 `esmc-check --download`，首次解析后固定权重提交 |
| ProteinMPNN | 从固定源码版本的 `vanilla_model_weights/` 取 `v_48_020.pt`，放到配置的 `assets/` |
| RFdiffusion | 按固定版本 README 下载 binder 的 `Complex_base_ckpt.pt`，放到配置的 `assets/` |
| Protenix | 准备 `assets/checkpoint/protenix_base_default_v1.0.0.pt` 以及该源码版本要求的 `assets/common/` 推理 CCD 等数据 |
| PyRosetta | 合法安装的环境与自带数据库，模型配置使用 `ref2015`；需保留发行版版本和许可 |

不要将不同模型的文件改名冒充所需权重，也不要从不可信来源加载原生 PyTorch/pickle
资产。需要的下载位置与文件应以固定提交的官方文档为准；这里不维护未经验证的镜像。

记录实际环境与资产哈希，例如 ProteinMPNN：

```bash
python scripts/pin_local_engine.py \
  --config configs/protein_tools.json --out tools.local.json \
  --engine proteinmpnn \
  --python /opt/proteinrsi/envs/proteinmpnn/bin/python \
  --repo /opt/proteinrsi/engines/proteinmpnn \
  --assets /opt/proteinrsi/assets/proteinmpnn \
  --asset v_48_020.pt --license-reviewed
```

给下一引擎配置时，以前一个 `--out` 文件作为新的 `--config`，输出到新的文件；
文件存在时拒绝覆盖。`--license-reviewed` 是操作者的确认，不是授予任何许可。
可只启用当前任务需要的引擎。检查无需安装全部模型：

```bash
proteinrsi tools list --config tools.local.json
proteinrsi tools describe --name proteinmpnn_design
proteinrsi tools doctor --config tools.local.json --probe --strict
```

`disabled`、`missing_requirements`、`configured_not_inference_tested` 和
`runtime_checked` 是不同状态。`--probe` 检查导入，不做蛋白推理；ESMC 权重仍通过
`esmc-check` 检查。必须再对每个新增引擎运行合适的真实输入，才能宣称推理已验证。

## 启动任务与调用

```bash
proteinrsi init --task examples/wetlab_task.json \
  --local-tools tools.local.json --out runs/local-protein
proteinrsi esmc-check --campaign runs/local-protein --download
```

不显式指定 workflow 时，ESMC工具和已启用的本地工具自动进入初始白名单。
传入 `--workflow` 时白名单完全由该文件决定。环境参数仍然只有操作者能改。

先导入结构文件，不让 LLM 访问任意本机路径：

```bash
proteinrsi artifact-import --campaign runs/local-protein \
  --file /path/to/reviewed_backbone.pdb --kind pdb
```

输出 `artifact:SHA256.pdb`。把实际引用填进 `examples/local_tools/proteinmpnn.json`
的副本，同时填正确的母本、设计链、可变位点和采样参数。全零哈希只是模板标记，
不会指向任何真实结构。然后：

```bash
proteinrsi protein-tool --campaign runs/local-protein \
  --name proteinmpnn_design --arguments my-mpnn-input.json

# 对话 LLM 配置与原版相同，ESMC 不是对话模型
set -a; source .env; set +a
proteinrsi step --campaign runs/local-protein --agent llm
```

`--local-tools` 保存到任务后，恢复任务时不需要反复传入配置。
`protein-tool` 也能调用检查、结构预测及 Rosetta 工具，不仅限于 ESMC。

## 13 个工具，不是 13 个模型

| 工具 | 输入要点 | 输出与边界 |
|---|---|---|
| esmc600m_score_variants | 母本、候选序列 | WT背景掩码边际先验；多突变为加和，不是组合效应真值 |
| esmc600m_suggest_mutations | 母本、允许位点、top_k | 单点建议，不是从头结构生成 |
| esmc600m_embed_sequences | 序列列表 | 特征引用；不把大矩阵塞进 LLM 上下文 |
| protein_sequence_qc | 母本、候选 | 字母表、长度、替换与简单描述符；不是稳定性／溶解度预测 |
| structure_inspect | PDB引用 | 链序列和原始编号—观察位置映射 |
| interface_geometry | PDB引用、两个链、距离阈值 | CA距离和接触计数；不是全原子碰撞检查或亲和力 |
| structure_compare | 两个PDB引用、明确的链 | 等长对应残基的Kabsch CA RMSD，无自动序列比对 |
| msa_validate | A3M引用、查询序列 | 检查查询和对齐长度；不执行MSA数据库搜索 |
| proteinmpnn_design | 骨架、设计链、母本、可变位点 | 单设计链序列；固定其它链及不可变残基，结果再次校验 |
| rfdiffusion_binder | 靶点PDB、靶点链、Binder长度、热点 | 骨架引用与验证后的链角色，不把poly-G当最终设计 |
| protenix_predict | 候选、单体／复合物、明确MSA模式、采样参数 | PDB/CIF及原始置信度文件；不输出校准Kd |
| rosetta_relax | 结构、次数、种子 | 坐标约束FastRelax、REU能量、原序列不变 |
| rosetta_interface | 两链复合物及角色 | REU界面能与面积；许可需单独准备 |

源码入口：`localtools/descriptions/*.json` 是独立描述，
`localtools/functions.py` 是科学函数，`localtools/worker.py` 调用上游程序，
`localtools/execution.py` 管理子进程。ESMC 通过原有 `protein/esmc.py`，
可选 `protein/isolated.py` 保持同一打分／特征实现。

## 环境隔离与执行边界

每个外部引擎使用独立解释器。父进程不传递 LLM 密钥、云密钥或代理凭据；调用是
参数数组，不执行 LLM 生成的 shell。标准输出、标准错误、输入文件和版本记录保留
在 `local_jobs/`；超时杀掉进程组；失败不自动重试。文件引用及SHA防止路径穿越和
缓存串用，预算仍由统一 ToolGateway 计量。

**venv/Conda 只隔离依赖，不是安全沙箱。** Python audit hook 阻止普通 Python
联网，但不等价于内核防火墙，也不能阻止本地程序读取同用户权限下的文件。
涉及不可信代码时，用可选 Docker 路线或站点作业隔离：`runtime=docker`、
固定 `image@sha256:...`、配置 `container_python`，只挂载只读代码/资产和单个工作目录。
Docker命令使用 `--network=none`、只读根文件系统和非root UID；不自动拉取镜像。
本轮未运行 Docker 路线，需要在部署机器验证驱动、镜像和挂载。

本地作业采用有界同步等待，不是成熟的集群调度器。记录wall-time及工具计数，
不是GPU秒精确配额。ESMC专用解释器模式每个未缓存操作重新加载权重，适合先确保
依赖隔离；高吞吐时可以保留已支持的native模式，或后续增加常驻本地Worker。

## MSA与结构范围

Protenix直接调用 `runner/inference.py`，不调用远程MSA预处理CLI。
输入必须明确为 `none` 或 `precomputed`。有预计算MSA时，提供每个输入实体的
query匹配A3M。没有MSA的运行可用于流程测试，但不保证与有MSA时同等准确。
本版没有内置MMseqs2搜索，不能把验证A3M说成生成了MSA。

结构适配目前限制为典型规范蛋白、单模型PDB、完整N/CA/C/O骨架。
Protenix适配只封装蛋白单体/蛋白复合物，不宣称覆盖该模型的所有配体、核酸和修饰能力。
de novo Binder 可由 Agent 在任务长度范围内选择本次生成长度；每次 RFD 调用仍生成该长度的骨架，要求完整固定靶点。已有 scaffold 按其任务约束执行。
旧的部分可变Binder任务可继续做ProteinMPNN骨架重设计，不强行走RFD。

## RSI保持独立

增加了有界B工具回合和可选C分析回合，允许“骨架→序列→结构评估”的真实顺序依赖。
这是可以演化的工作流参数，不是每个任务都必须走的硬编码管线。
工作流/Meta补丁不能修改解释器、源码提交、资产哈希、许可或验收规则。

当前独立Meta数值评测器仍适合序列任务。若启用外部结构引擎，它会明确拒绝通用
Meta晋级，因为需要每个验证任务独立提供结构/环境与等预算计算；不能悄悄复用
另一个靶点的结构或省略工具后宣称Meta更强。任务内工作流试用仍使用原来的实验验收。
