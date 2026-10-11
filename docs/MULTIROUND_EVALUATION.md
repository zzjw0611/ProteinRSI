> 本文记录前一阶段协议；新建研究的 W/M 对照与经验复用见 [METHOD_EVALUATION.md](METHOD_EVALUATION.md)。

# 共用 Task Loop 与自动 H₀/H₁ 验证（前两批）

## 范围

新协议为 `task-loop-v1`，第一阶段仍只研究组合突变优化的 Task Harness。
改进器 M₀/M₁ 效果验证、跨研究永久能力库、新 skill 发布系统不在本次实现范围。
源码、模型权重、Controller 权限、验收规则不会因方法提案而自动开放修改。

本次支持实测数据回放或明确标注的合成测试；不会用 CSV 冒充新湿实验。
真实实验可通过公共执行器停在等待批准/反馈状态；没有授权验证反馈适配器时，
自动方法验证保持待验证，不调用未授权实验，也不阻断原版本的正常任务分析。

## 两个循环，一套执行和记账基础

```text
主研究：Planner → Executor → 受控反馈 → Analyst → 下一轮/报告
                        ↓ 执行轨迹
                  Improver 提出修改
                        ↓
      冻结起点、H₀/H₁、资源/记忆、指标及停止条件
                        ↓
     H₀ 子 Campaign             H₁ 子 Campaign
     同一 run_task_loop()       同一 run_task_loop()
     多轮反馈、分析、重新规划      多轮反馈、分析、重新规划
                        ↓
           Evaluator → Controller 发布/拒绝
                        ↓
                  原研究安全边界续跑
```

`task_loop.run_task_loop()` 是薄驱动器，不另写规划、候选生成、评估算法。
它调用 Campaign 现有 prepare/approve/ingest/analyze_feedback 检查点，返回
`completed`、`waiting_approval`、`waiting_feedback`、`paused`、
`budget_exhausted`、`waiting_validation`。未知副作用与完整性冲突仍失败关闭。
最后一轮也分析；成功完成的反馈和调用通过原检查点恢复，不重新查询。

验证分支冻结 W/M，控制器禁止提案和版本发布，**但不冻结数据学习**：后续轮次
仍然使用新揭示证据、更新计算模型、调整本次任务计划。蛋白工具仍为可选能力。
保留的 W 程序在原来的工具/代码沙箱运行；不新增本地不安全 exec 回退。

## 起点与公平性

`fork_for_evaluation()` 从内容寻址快照创建两个可恢复子 Campaign，各自拥有
数据库、调用缓存、任务历史及分支笔记。共享的是相同初始证据、基础配置、模型
标识、工具权限和初始经验快照，不是之后各自发现的结果。

任务输入、方法版本、预算、资源/记忆引用、指标计划引用和停止条件记录在
`EvaluationSpec`。指标计划先冻结再产生结果。方法程序和配置沿用现有可执行版本；
模型设置、权限或冻结资料改变时拒绝悄悄恢复旧验证。

启动器默认可以从当前已揭示快照自动构造 H₀/H₁ 对照，无须手写案例 JSON。
这种自动对照的范围明确标记为 **same_snapshot**，不是跨蛋白泛化，也不是独立
最终确认。一条多轮轨迹不会被计为多个独立案例。原有显式案例入口保留，用于
授权的独立案例及最终确认。

最终确认只读取已经接受的固定版本，不发布，不写入 Improver 的能力记忆。
选择与确认不能重复使用相同科学起点；改名或改随机种子不构成新起点。
已经在选择阶段揭示的实测值必须作为已知证据声明，不能伪装成确认阶段未知标签。
程序约束并不替代研究者对真正独立确认数据的管理。

## 预算

- 起点前已经合法提供/揭示的证据只复制，不重新收费。没有提供的初始实测仍需要付费查询。
- 两组的实验、LLM、工具及可选 PLM 预算通过 SQLite 事务成对预留；E 的调用也有独立预留。
- 主研究默认保留下一板查询额度，可由研究配置明确调整。额度不够则待验证、原版继续。
- 预留不是消费。实际调用把预留转成真实收费；全部完成后释放未用额度。
- 同一分支/请求的重启不重复收费。已批准实验和已执行调用不因拒绝或回退退款。
- 未知完成状态的资源不能直接释放；必须先完成原有恢复/对账流程。
- 整板任务的验证预算也必须满足整板约束。

`budget` 中 `allocated_unspent` 表示尚未执行的预留，与 committed 和普通 reserved
分开显示，避免重复计算。状态机可能有相同上限下的不同实际成本，评估会保留
这些差异，而不为了凑足次数人为追加无意义操作。

## 配置与命令

新的 `start` 和默认 `init` 使用新协议；显式旧配置不会静默升级。
`start` 默认不限制 LLM/tool 总次数，显式 `--llm-calls`、`--tool-calls` 仍生效。
单次执行超时、格式修复、重试限制和生成代码隔离保持有效。正式对照使用有限上限。

可在 `init --research-config` 指定的 JSON 中调整自动验证的授权范围，例如：

```json
{
  "enabled": true,
  "protocol_mode": "typed",
  "context_policy": "evidence-v1",
  "improvement_contract": "capability-v1",
  "workflow_validation": "independent",
  "evaluation_protocol": "task-loop-v1",
  "auto_evaluation": {
    "enabled": true,
    "rounds": 2,
    "query_budget_per_arm": 24,
    "llm_calls_per_arm": 64,
    "tool_calls_per_arm": 128,
    "evaluator_llm_calls": 6,
    "evaluator_tool_calls": 16,
    "main_reserve_wells": 12
  }
}
```

这些是执行/资源约束，不是科学诊断模板。实际任务和整板大小应一致。
省略 `query_budget_per_arm` 时按 `batch_size × rounds` 授权；
省略 `main_reserve_wells` 时在仍需执行的主研究中保留一板。

```bash
proteinrsi init --task task.json --out runs/multiround \
  --research-config configs/research.mutation.json --protein-model none
proteinrsi replay --campaign runs/multiround --dataset measurements.csv --agent llm
# 需要人工控制案例时，沿用接口；此时案例的 max_rounds 真正驱动多轮运行：
proteinrsi evaluate-workflow --campaign runs/multiround \
  --cases validation-cases.json --agent llm --promote
proteinrsi status --campaign runs/multiround
proteinrsi trace --campaign runs/multiround --format html --out trace.html
```

确认入口仍为 `evaluate-workflow --patch-id <accepted-patch> --cases confirmation.json`，
不得使用 `--promote`。完整标签只由控制器读取，不能进入 TaskView、工具输入或 E 请求。

## 恢复、历史协议与审计

`evaluation_drafts` 冻结执行前输入；`evaluation_specs` 记录冻结指标后的协议；
`harness_attempts` 区分 started、paused_provider、blocked、completed。
`evaluation_deferrals` 是尚未开始的验证，不冒充已经评估。
`validation_*` 与主研究记录分开；轨迹显示子研究轮次、免费初始证据、真实查询、
预留/释放和采用，发布后实际使用仍由 `task_harness_invoked` 记录。

已开始但暂停的比较恢复原分支；不会另起一次“运气更好”的试验。
预算不足但尚未开始的提案不会卡住主研究。完整性冲突和未知执行结果仍需人工对账。

旧 `decision-v1` 单步诊断仅保留作历史兼容，不标为多轮能力证据。
历史研究仍须使用原安装环境/代码快照恢复；不能在新安装中忽略源代码哈希强行续跑。
新协议应新建研究目录。

## 验证边界

合成/脚本化测试用于验证状态机、真实版本调用、隔离接口和成本守恒，不能证明
真实 LLM 会改进蛋白设计。需要 Linux Landlock/seccomp 的测试在不支持的主机跳过，
生产执行仍失败关闭。本次不启动付费 LLM、下载模型权重或开展 GB1 效果实验。
先完成机制回归，再另行授权总查询和计算预算开展小规模实测回放及 H₀/H₁ 对照。
