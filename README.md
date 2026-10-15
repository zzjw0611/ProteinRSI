# ProteinRSI

**用自然语言提出蛋白研究目标，让 LLM 自主规划、设计候选、调用工具，并根据实验反馈迭代，同时改进研究方法。**

当前重点是组合突变优化与历史实测数据回放。Agent 可以编写分析代码，并按需调用 ESMC、ProteinMPNN、RFdiffusion、Protenix、PyRosetta；所有蛋白工具均为可选。

[English](README.en.md) · [快速开始](#快速开始) · [文档](#文档)

## 工作方式

系统包含两个循环，逻辑角色可以共用一个 LLM：

- **研究循环**：目标与初始证据 → 规划 → 候选设计／工具与代码执行 → 实测反馈 → 分析 → 下一轮。
- **方法改进循环**：分析研究轨迹 → 提出工作流 **W** 或改进器 **M** 的修改 → 按本次研究的采用策略处理 → 后续轮次使用新版本。

W/M 的修改可包含提示词、策略和版本化沙箱程序。

LLM 决定科学方案；控制器管理任务约束、权限、查询预算和记录。预测与计算指标作为辅助证据，不能替代实测 fitness。历史回放的完整标签表只由控制端读取，Agent 按提交查询获得反馈。

新研究默认使用类型化协议，步骤间通过资源引用交接数据。方法修改默认 `direct`：通过静态检查后采用，标记为 **未经效果验证**；使用 `--method-policy validate` 可先做新旧方法对照，再决定采用。方法采用不等于已证明 RSI 收益。

## 快速开始

Python 3.11+。受保护的历史回放需要支持 Landlock／seccomp 的 Linux 环境。

```bash
git clone https://github.com/zzjw0611/ProteinRSI.git
cd ProteinRSI
python -m venv .venv
source .venv/bin/activate
pip install -e .

# 离线演示：合成数据与脚本角色，不需要 API 或模型权重
proteinrsi demo --adaptive --out runs/demo --rounds 3
```

演示报告保存在 `runs/demo/report.json`。开发与测试见 [测试说明](docs/TESTING.md)。

## 启动真实研究

将 [.env.example](.env.example) 复制为 `.env`，填写对话模型、API 地址和密钥。程序本身不会自动加载 `.env`：

```bash
set -a
source .env
set +a
```

以 **GB1 历史实测回放**为例：先按[数据准备说明](docs/SSMULA_PREPARATION.md)准备已核验的 SSMuLA 数据，保存在仓库外，再输入目标：

```bash
proteinrsi start "使用GB1实测景观优化fitness：2轮，共48个新查询，每轮最多24个。仅修改母本第39、40、41、54位，其余不变。
母本及已有实测fitness免费提供。蛋白工具可选，允许根据证据改进方法。" \
  --data-root ~/data/ssmula \
  --out runs/gb1
```

Agent 自主生成变体，查询后才获得历史实测值；缺失记录返回 `unavailable`，不当作零分，仍占查询名额。主实验与方法对照分别核算查询额度，调用和费用证据保留在记录中。

按需添加启动选项：

| 选项 | 用途 |
| --- | --- |
| `--local-tools tools.local.json` | 使用已安装的蛋白工具；[安装与配置](docs/LOCAL_TOOLS.md) |
| `--method-policy validate` | 验证后采用方法修改；[验证与采用策略](docs/METHOD_EVALUATION.md) |
| `--input candidates.fasta` | 传入序列或科学文件；可重复，用于排序、结构等任务输入 |
| `--prepare-only` | 仅解析目标并保存任务，仍会调用 LLM，不执行研究 |

目标缺少必要信息时，程序保存澄清问题；用 `start "补充信息" --continue-from runs/<study>` 继续。计算研究使用实际计算结果迭代；湿实验任务等待真实测量导入。

## 查看研究轨迹

启动后终端显示运行事件，研究目录保存状态、计划、代码、测量、方法版本及报告。

```bash
proteinrsi status --campaign runs/gb1
proteinrsi trace --campaign runs/gb1 --follow
proteinrsi trace --campaign runs/gb1 --format html
```

轨迹记录 Agent 当时可见的证据、实际请求与返回、决策说明、工具输入输出、失败修复和资源消耗。HTML 可离线查看，复制时保留相邻附件目录；详见[轨迹导出](docs/TRACE_EXPORTS.md)。

## 文档

| 想了解什么 | 入口 |
| --- | --- |
| 架构、角色与双循环 | [双循环](docs/DUAL_LOOP.md) · [架构](docs/ARCHITECTURE.md) |
| 计划、工具与生成代码如何执行 | [执行契约](docs/EXECUTION_CONTRACT_REFACTOR_PLAN.md) · [数据流协议](docs/DATAFLOW.md) |
| W/M 修改、对照、采用与回退 | [方法评估](docs/METHOD_EVALUATION.md) · [方法治理](docs/METHOD_GOVERNANCE.md) |
| 蛋白工具与 ESMC 配置 | [本地工具](docs/LOCAL_TOOLS.md) · [ESMC](docs/ESMC600M.md) |
| 数据、上下文与标签隔离 | [SSMuLA](docs/SSMULA_PREPARATION.md) · [上下文](docs/LLM_CONTEXT.md) · [回放隔离](docs/REPLAY_SECURITY.md) |
| 测试、验证与版本变化 | [测试](docs/TESTING.md) · [接口验证记录](docs/INTERFACE_REFINEMENT_VALIDATION.md) · [更新记录](CHANGELOG.md) |

## 许可与验证范围

这是研究软件。离线测试、成功运行或方法版本采用，都不能单独证明蛋白功能提升或跨任务 RSI 能力。具体结果与限制以对应研究的原始记录为准；不同任务和蛋白工具需要分别验证。

原创代码采用 [MIT](LICENSE)，Biomni 改编文件采用 Apache-2.0，分发包为 **MIT AND Apache-2.0**。来源与修改见 [复用清单](docs/REUSE.md)、[NOTICE](NOTICE) 和[第三方条款](THIRD_PARTY.md)。模型权重、实验数据与密钥不随代码分发，各自条款独立适用。
