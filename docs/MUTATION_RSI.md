# 组合突变任务：自主 Meta 与递归方法进化

## 当前范围

只围绕一个组合突变优化任务：母本、可变位点、已揭示实测数据和有限实验预算。
第一阶段用历史湿实验数据回放，不称为已经执行的新湿实验闭环。
保留现有其他任务接口以兼容代码，但本方案不依赖多任务或跨蛋白迁移。

## 整体框架

```text
母本 / 可变位点 / 已揭示实测数据
                  ↓
         Task：A/B/C 使用方法 W
                  ↓
       候选 → 实验控制器 → 真实反馈
                  ↓
       Meta M：自主选择分析、工具与修改
              ↙              ↘
           W 候选            M 候选
              ↓              ↓
        独立验证与采用     新旧 M 同起点产生后继 W
              ↓              ↓
          新 W 执行       验证后新 M 接管后续改进
                              └────────↺
```

W 是任务方法：A/B/C 策略提示、授权工具使用配置和可复用程序。
M 是改进方法：自由策略提示和可复用分析程序。基础模型权重不在本次进化范围。
程序和策略只有通过原有版本验收后才部署，不允许直接修改控制器源码。

## Meta 自己决定什么

新 `ResearchConfig` 的 `meta_autonomous=true` 让 LLM 自行决定证据是否足够、
分析什么、何时停止、改 W 还是改 M。`min_observations`、`cooldown_rounds`、
`min_remaining_wells` 仅保留给历史配置和显式 deterministic 基线，不在新自主路径中
强制执行。`enabled=false` 仍是不可自行解除的管理开关。

M 每步可以返回一个工具调用，读取结果后继续分析或输出最终提案。
预置分析工具是可选能力，不是必须依次执行的诊断模板。
`research_python` 允许 M 编写自己的分析；错误结果供其检查、修改，不伪装成成功。
也可以不调用任何工具、不修改任何方法。

固定的是任务、证据权限、预算、工具授权、输出接口、验收协议和资源上限。
`meta_tool_rounds` 是一次提案最多可自主调用工具的次数，不是科学阈值。
所有 LLM 和工具调用继续使用原账本，验证实验与候选实验共享预算。

## 可复用程序：不是一次性反思

`Workflow.programs` 和 `MetaPolicy.programs` 是可选 `MethodProgram` 列表。
每项包含 `name`、`purpose`、`code` 和可选 `inputs`。不预置任何示范突变规律。
程序必须设置 JSON 对象 `result`，使用既有 `research_python` 接口：
`context` 只含当前已揭示的 TaskView，`inputs` 是该程序声明的参数。

系统将完整源码随 W/M 版本保存，而不是依赖父运行目录内的可变脚本路径。
因此新旧 Meta 的隔离验证分支可直接加载同一版本，不需要读取另一分支的私有缓存。
源码、参数或提示改变都会改变方法版本；空程序列表不改变旧方法标识。

被保留的每个程序会在其 W/M 下一次调用时运行，结果作为计算证据提供给 Agent。
W 程序结果在 `research_context.workflow_program_outputs`；M 结果在 `program_results`。
M 可通过后续补丁修改或移除程序。结果不自动变成候选、预测标签或实测 fitness；
候选仍经过 Task 的既有生成、排序、合法性检查和提交过程。

每个程序调用独立计工具成本；最多四个已保留程序，外加 `meta_tool_rounds` 次自主工具调用。
它们使用相同生成代码沙箱，不允许网络、密钥、活动数据库或隐藏测量文件访问。
平台缺少 Landlock/seccomp 时拒绝执行，不降级到不安全的本地 `exec`。
未启用 `enable_generated_code` 时也不能采用带程序的补丁。

## 启动与检查

沿用已有数据准备流程，准备 `task.json` 和控制器独占的实测表。新建研究：

```bash
proteinrsi init --task task.json --out runs/mutation-rsi \
  --meta examples/meta_policy.json \
  --research-config configs/research.mutation.json \
  --protein-model none
```

`--protein-model none` 只关闭可选 PLM，不代表关闭 LLM；执行时仍用既有
`--agent llm` 路径和本地配置的提供商。配置 `configs/research.mutation.json`
开启类型化任务执行、按需证据读取、自主 Meta 和沙箱 Python。
需要蛋白模型时改用已部署并核对的配置，不隐式下载或调用。

```bash
proteinrsi status --campaign runs/mutation-rsi
proteinrsi trace --campaign runs/mutation-rsi --format json --out trace.json
```

`status` 新增 `rsi_evidence`：区分 M 被采用、主运行实际调用新 M、以及其后继提案。
`meta_analysis_*` 与 `method_program_executed` 事件通过 `output_id` 指向原有
`research_step_outputs`；对应 LLM 和工具收据继续保存在原命名空间并纳入 trace。
恢复运行复用已经完成的分析、程序结果和提案，不重复计费；不确定的工具调用仍阻塞。

## 只在同一个任务上做三组对照

所有组使用相同任务、初始证据、基础模型、工具、预算、评价设置和成对随机种子。
为每组使用新的研究目录，不共用后来形成的记忆或标签。

| 组别 | init 的额外设置 | 可改变的对象 |
|---|---|---|
| 固定 W | `--meta configs/meta.disabled.json --method-governance configs/methods.fixed_workflow.json` | 不允许 W/M 补丁，仍正常使用新增实测数据 |
| 固定 M | `--method-governance configs/methods.fixed_meta.json` | M 可以提出 W 修改，不能采用 M 修改 |
| 可演化 M | 默认方法治理配置 | 可以提出并验证 W/M 修改 |

可修改目标由控制器的冻结管理配置约束，不靠提示词假装“固定 Meta”。
本次不改变原有科学指标和采用协议。

## 如何展示 RSI

分别展示：

- 任务表现：最好实测 fitness 对累计查询成本，包含方法验证成本。
- 改进器表现：旧 M 与新 M 从同一 W、同一份可见数据、相同预算出发产生后继 W。
- 实际递归链：M0 提出 M1 → 验证采用 → 主运行调用 M1 → M1 再提出 W/M 修改。

`rsi_evidence.recursive_use_observed` 只报告实际机制链；验收分支调用不算部署后使用。
`efficacy_claim` 始终标为 `not_established_by_this_audit`，不能把采用记录或单次涨分
当作改进能力已经增强。科学结论需要重复对照及未参与版本挑选的确认实验。

PPT 用四页即可：单一研究问题、上面的 W/M 架构、真实版本修改与调用链、三组对照。
没有运行得到的经验或结果不要提前填入。

## 兼容性与测试边界

旧研究不原地迁移：保留其提示和配置快照；源码/依赖快照不匹配时仍拒绝继续执行。
应在新环境和新研究目录运行本次版本。
新增测试覆盖自主分析、停止/拒绝、恢复幂等性、程序版本/继承、共享预算、
固定 Meta 约束与实际调用审计。脚本化模型和模拟计算只证明机制，不证明真实 LLM
会产生有效方法，也不代表已经得到 SSMuLA/TadA 实验收益。
