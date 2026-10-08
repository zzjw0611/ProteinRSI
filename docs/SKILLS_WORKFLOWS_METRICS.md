# 模板绑定指标的研究路径

## 本次变化

新增一个小型指标库和一个按蛋白设计类型组织的方法模板库。A 先选择方法模板，再展开关联指标及少量显式补选条目，继续生成原有 `Protocol`。没有新增 Agent、第二套执行器或数据库；`TaskProfile`、实验预算、反馈入口、E/M 验收及晋升规则不变。

```text
任务输入和约束
  → A-resources：短目录选择（最多两个模板、四个额外指标）
  → 加载选中条目的冻结内容
  → A-plan：生成现有 Protocol
  → B 设计 / C 分析 / 已注册工具
  → research_metric_extract：从真实执行收据提取指标
  → MetricTable 与资源引用
  → C 分析排序、A 调整后续步骤、Campaign 处理反馈
```

`A-resources` 是现有角色的一次知识选择调用，不是新 Agent。LLM 模式仍另外调用原资源选择器选择工具和数据；两类调用都使用现有客户端预算、缓存与恢复机制。规则模式不增加 LLM 调用。

## 内容在哪里

`skills/protein-metrics/cards.json` 是九个指标族的唯一元数据来源；卡片只有 id、name、category、description、purpose、provider_tool、required_inputs、source、unit，没有 limitations。短目录由程序生成，不另建一份手工索引。

`skills/protein-design-workflows/templates.json` 按六类设计任务组织：已有蛋白性质优化、固定骨架序列设计、从头结构设计、Binder/界面设计、功能位点/酶设计、多聚体/组装设计。每条包含指标 ID 及用途、所需工具与输入、步骤、可调整部分、来源和版本。Virtual Lab、BindCraft、Claude 是 Binder 下的不同参考子模板；未部署的原方法依赖会明确显示，不能以替代分数冒称原方法复现。

## 激活和兼容

新的自然语言 typed 任务默认添加 `protein-metrics`、`protein-design-workflows` 两个 skill_names 和 `research_metric_extract` tool_name，并在任务初始化时保存知识快照。原 `--protocol-mode legacy` 路径不自动启用。

通过底层 API 或 init 使用自定义工作流时，应显式在 Workflow 中包含这两个 skill_names、`research_metric_extract` 以及打算使用的实际工具；ResearchConfig 使用 `protocol_mode=typed`。只有目录条目不会授予工具权限。现有没有这些配置的任务不自动迁移。

知识快照及有界选择记录复用 `research_step_outputs`。快照包含完整库、说明及内容哈希；选中条目哈希进入资源选择和 Protocol 计划身份。当前研究恢复时读取自己的快照，新安装内容只用于新的快照。A/B/C 接收该次选中的内容，不重新读取当前安装版本的全文。

这不是运行中升级代码的许可。原自定义验收程序仍锁定执行环境；不要在一个正在进行的科学研究中更换代码、依赖或冻结的验收口径。

## 指标如何取得

`research_metric_extract` 有两种输入方式：

- 专业工具输出：通过 Protocol binding 将真实 provider 步骤的整个 result 以 `delivery=ref` 绑定到 `source_ref`，并传入所选 `metric_ids`。不接受任意路径、数字表或用户指定的测量记录。
- 实测进展：`metric_ids=["observed.summary"]`，显式给出 `top_ns`，不提供 source_ref。只读取当前 TaskView 的已揭示观测，按序列平均有效重复。报告同时保留覆盖计数。该描述性集合包含当前已揭示母本和对照，不能冒充排除了对照的正式 W/M 比较。

输出中的 `metric_table` 可以直接绑定到 C 的 evidence，或通过 `/metric_table` 命名视图形成 `analysis.metric_table/v1` 资源。`adapter:merge_metric_tables` 用两个类型化引用合并实际指标行，不需要 LLM 重新填写数值。详细原始字段、样本身份和来源引用保存在同一次提取结果的 `row_provenance` 中。

控制器除检查资源哈希和任务作用域外，还匹配已完成的 ToolGateway 作业与 Protocol 收据。Agent 自己写的同名 producer 或指标表不能取得该提取权限。提取工具收取一次工具调用预算，不重新运行模型，也不收取实验查询预算。

## 实际支持的范围

| 入口 | 本版行为 |
|---|---|
| Protenix pLDDT_mean / ipTM / has_clash | 从原生 summary_confidence JSON 读取；严格关联输入、seed、sample、CIF、转换后的 PDB；一个预测可取得多项指标 |
| PAE | 只保留知识卡片；完整 token PAE/chain 映射和相应汇总适配器尚未接入，不在可执行指标枚举中 |
| C-alpha RMSD | 读取现有 structure_compare 的单链、等长、明确残基对应结果；不声称催化原子或整个装配体 RMSD |
| ESMC masked marginal log odds | 读取现有 ESMC 工具结果并核对完整序列；不冒称原 Virtual Lab 的 ESM 实现 |
| ProteinMPNN score | 从实际 FASTA 中匹配 designed-entry 的 score=，不使用 candidate.rationale，也不混用 global_score |
| Rosetta interface dG | 读取已有 REU 字段并保留明确结构/链/seed |
| Observed summary | 复用现有 summarize_metrics；明确 Top-k、方向、重复汇总和缺失值，不修改 E 的冻结指标程序 |

Protenix 文件匹配针对当前适配器的原生 `proteinrsi_<seed>_sample_<n>` 与 `proteinrsi_<seed>_summary_confidence_sample_<n>` 命名，不将独立排序的文件列表逐项拼接。未知命名版本、重复样本、缺失字段、非有限数或错配输入会失败，不猜测替代结果。原始指标逐结构样本保存，跨样本汇总由研究明确提出。

## 验证

新增测试覆盖目录、元数据引用、快照、预算、有界修复与恢复、工具权限、真实收据来源检查、模型多样本配对、数值异常、重复调用缓存、打包，以及性质优化/Binder 两条 A 规划路径。

```bash
pytest -q tests/test_skill_library.py tests/test_metric_extractors.py tests/dataflow/test_skill_planning.py
pytest -q
python -m build
```

端到端测试使用真实本地 Protocol/Store/ToolGateway 和确定性解析器，但外部蛋白引擎结果及 LLM 角色是明确的人工测试数据，不证明 GPU 推理或生物学改进。真实模型、PyRosetta 和湿实验需在部署环境另行验证。GitHub Actions 的 skill-package 工作流检查 wheel 内两个 Skill 的完整性；主 CI 保留原完整回归和真实受限内核验收。
