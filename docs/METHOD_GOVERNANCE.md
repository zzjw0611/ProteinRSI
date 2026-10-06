# 方法版本治理：候选、验收、采用与回退

本增量保留现有 A/B/C/M、类型化研究执行器和实验控制器，不引入新运行框架，也不扩大 M 的代码执行权限。新建研究默认启用；没有方法快照的旧研究保持原有兼容路径，不能通过新命令补造历史或回退。归档实验、已揭示测量及已发生费用未修改。

## 实际实现

`governance.MethodGovernance` 是可信控制端模块。M 仍只能提出现有 `Workflow` / `MetaPolicy` 的类型化补丁；一个研究同时最多有一个待处理的 W 或 M 候选。

```text
staged → validating → awaiting_approval → awaiting_results
              │                                │
              ├─ 明确执行失败 → failed           ├─ accepted → 采用新方法
              ├─ 无有效比较 → inconclusive      ├─ rejected → 继续稳定版
              └─ 完成状态不明 → blocked          └─ inconclusive → 继续稳定版

blocked → 操作者核对外部工作后显式 abandon → cancelled
已采用版本 → 空闲轮次边界显式 rollback → 曾生效的历史版本
```

离线 M 验收不经过实验批次审批状态，但使用同一候选记录与版本切换逻辑。只生成离线报告而不请求采用时，候选仍保留；相同评估清单不能重复领取预算。

候选的真实执行就是原有的有界验证执行，不额外偷偷增加一遍模型运行。格式、字段、工具与技能预检在暂存阶段执行；实际候选生成及原有科学约束验证在验证阶段执行；实验效果仍由原有可信 gate 判断。**验收指标仍是已有的平均带方向测量值，本次没有改变统计口径，也没有新增科学效能保证。**

明确失败的 W 候选不会阻止已成功规划的稳定版批次继续准备；在线 M 候选同样处理。超时、未完成外部调用、未知运行错误被保守标记为 `blocked`，恢复时不自动重复调用。离线 M 错误也会保存终止或阻塞记录，已经产生的子运行费用继续保留。

## 快照和执行归属

- `method_assets`：按内容哈希保存分发包中的 Python、提示词、技能和工具描述源码，以及本解释器/已记录依赖版本。不读取 `.env`、API 密钥、外部权重或实验标签文件。
- `method_snapshots`：不可变 W/M 定义、源代码包引用、提示词/研究/工具配置和接口 Schema。
- `method_candidates`：不可变补丁、父版本、基线/候选快照、证据版本、轮次和验证计划。
- `method_candidate_states` / `method_transitions`：当前候选状态及不可覆盖的转换记录。
- `method_activations` / `method_switches`：曾生效版本及每次采用/回退的操作者、理由、证据和预算快照。
- `batch_method_bindings`：批次中每个样本实际使用的方法，以及在线 M 两臂产生的后继 W、研究运行引用、已解析模型快照和对话模型标识。

**方法模板与本轮实际计划不是同一对象。** 本轮协议、成功结果收据和生成的分析代码继续保存在已有的研究资源、运行记录及 `code_programs` 中；它们不会因回退被删除。外部引擎/权重只记录已有声明或实际解析身份，不声称已把外部环境完整打包，也不加载归档源码执行。

新治理路径在研究规划和版本切换时校验快照；不兼容的源码、已记录依赖或配置会被拒绝，需使用原环境或新建研究，而不是以相同 W 名义偷偷换运行环境。运行中不支持热替换安装包。

## 操作命令

查看候选与切换历史（不调用 LLM 或蛋白工具）：

```bash
proteinrsi methods status --campaign runs/YOUR_RUN
proteinrsi methods snapshot --campaign runs/YOUR_RUN --snapshot-ref method-SHA256
```

回到本研究此前生效的 W；M 同理使用 `--target meta`：

```bash
proteinrsi methods rollback --campaign runs/YOUR_RUN \
  --target workflow --version w-PREVIOUS_VERSION \
  --operator YOUR_NAME --reason "后续运行发现回归，恢复此前已采用的方法"
```

只能在 `ready`、没有待审批/已提交批次、没有待处理候选的轮次边界回退。不能通过指定一个仅暂存、失败或未验收的版本绕过 gate。回退不重新规划旧批次，不删除测量，不重置轮数，不释放已发生费用。

离线 M 验收在取得评估执行权的同一事务中检查冻结方法和该候选的在线／离线评估记录。存在 `started`、`blocked` 或在线 `planned` 记录时，即使换了案例清单或仅生成报告，也不能再启动离线验收。应先完成原评估，或核实外部执行状态后显式放弃候选；已完成的仅报告评估允许继续使用不同案例清单验收，新增查询仍计入共享预算。

放弃尚未提交批次的候选：

```bash
proteinrsi methods abandon --campaign runs/YOUR_RUN \
  --patch-id p-CANDIDATE --operator YOUR_NAME --reason "不继续该候选"
```

对于 `blocked` 或仍显示 `validating` 的候选，必须先在实际执行环境核对并停止/处理外部工作，再额外传入 `--acknowledge-uncertain`。此参数只是操作者确认，**不负责取消进程、撤销实验、删除失败收据或退款**。待审批批次仍用已有 `cancel`；已提交批次必须按原版本导入结果。

默认连续 3 次未接受/失败结果暂停自动改进；同一候选/同一失败记录不重复计数。默认候选最多跨 2 个不同轮次延期，重启不增加或重置次数；默认一个补丁最多改 3 个字段。操作者可在创建时通过 `init --method-governance configs/method_governance.json` 设置这些边界，M 无权修改。

```bash
proteinrsi methods resume --campaign runs/YOUR_RUN \
  --operator YOUR_NAME --reason "已检查失败记录，允许继续提出候选"
```

`resume` 只解除治理暂停，不把管理员关闭的 `MetaPolicy.enabled` 改为开启。

## 原子性与历史

`Store.transaction()` 为控制端提供短事务和嵌套保存点；不会把远程 LLM/工具调用放入数据库长事务。版本指针、切换记录及相关候选状态一起提交，UI 事件在提交后发送；界面输出失败不会把已提交操作伪装成可重试失败。

实测导入先保存不可变原始测量，再原子提交本轮状态与验收结果。因此，即使后续诊断/审计写入出错，原始测量和已提交预算仍存在；重试相同导入不会重复追加观测或计费。

`trace --format html/json` 展示方法候选、快照引用、切换和 GEPA 档案。A/B/C/M 只能获得最近的本研究已结束候选摘要，含成功、失败及证据不足；待验证候选不会提前写进两臂共享的方法经验。这些经验均标记 `transfer_validated=false`，没有跨研究自动传播或直接采用机制。

## GEPA 与剩余范围

向 `ProteinWorkflowAdapter(evaluator, store=campaign.store)` 传入现有账本后，包装器保存 GEPA 全部候选、父子关系、逐案例验证分数以及 `to_dict()` 不包含的运行日志/元数据。失败也有独立档案。函数仍返回一个候选 `Workflow`，不负责采用；不传 store 时保持内存 API，并可从 `adapter.last_result` 读取完整结果。

本次没有复制 MLflow/OpenEvolve/ACE 源码或增加这些依赖。不可变版本与活动指针、候选档案和分层验收是设计参考；具体控制端实现为 ProteinRSI 原创，GEPA 继续是既有可选 API 适配。

尚未实现：M 任意协议拓扑/Schema/代码的持久补丁、跨研究经验库、新的任务专属验收指标、任意跨版本缓存复用。现有资源作用域、成功结果复用和标签隔离没有放宽。机制测试与人工故障注入不证明真实蛋白收益、长期 RSI 或外部模型运行质量。

## 验证

```bash
pytest -q tests/test_governance.py tests/test_method_transactions.py tests/test_gepa_archive.py
pytest -q
ruff check .
python -m build
```

专项测试覆盖明确失败/未知完成、子运行费用保留、原子切换故障、回退与重导入、连续失败暂停、延期上限、来源一致性、受限 worker 权限及 GEPA 档案。现有研究执行集成测试改为在创建研究时声明研究配置，不能先冻结快照再偷偷改变配置。
