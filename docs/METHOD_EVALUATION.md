# W/M 方法对照与可追溯经验复用

## 这次保留什么、补什么

在 `7c582c2` 的两个循环、公共 Task Loop、隔离执行、成对预算和内容寻址版本之上增量修改。
不新增 Agent、常驻服务、第二套验收器或 skill 发布系统，也不开放 Controller 源码自修改。

| 增量 | 必要性与实现边界 |
| --- | --- |
| 统一权限 | 原来的“验证分支一律不能改方法”不足以比较改进器。权限由治理模块集中推导，执行器不再自行解释验证场景。 |
| 完整 M 对照 | 复用 `evaluate_method()`、子 Campaign 和现有 W 验收。固定 M，验证后才在分支内改 W；不能跳过验收直接改后继方法。 |
| 轻量能力库 | 可选的本地共享 Store，记录、检索、冻结，不添加向量数据库。未经确认的经验是可检验信息，不是已发布能力。 |
| 方法导出与轨迹 | 直接读取现有快照、转换和实际调用记录，不另存一份可能失真的版本状态。 |

新建 CLI 研究使用 `method-loop-v1`。`decision-v1`、`task-loop-v1` 的旧入口和协议身份保留。
**历史研究仍需原代码和依赖环境恢复；不是升级源码后忽略快照检查。** 新版本应新建研究目录。

## 一套权限接口，三种运行上下文

| 上下文 | 可提案/发布的对象 | 不变对象 |
| --- | --- | --- |
| 主研究 | 冻结的管理配置授权的 W/M | 预算、数据入口、当前验收协议 |
| W 对照 | 无；不调用 Improver | W、M 与权限 |
| M 对照 | 仅分支内 W，且须经过同一 W 对照验收 | M、权限与外层验收协议 |

`MethodGovernance.allowed_targets`、`require_target()` 和 `require_evaluation()` 共用此规则。
它同时约束提案、采用、回退和直接评估入口；不能靠 `confirmation` 参数绕过分支权限。
每次恢复仍核对活动方法与固定对象。原来缺少 target 的验证分支继续按固定 W/M 解释。
新协议同时冻结父研究的治理限制；子分支只能收窄可修改对象，不能放宽字段数、失败次数
或延迟次数等限制。主研究未授权 W 修改时，M 对照也不能在子分支内启用 W 修改。

这里“固定 W”不是冻结任务学习。Task 仍会吸收新反馈、重新规划、训练其方法允许的模型。
M 只能改进产生决策的方法，不直接提交下一批实验候选。

## M 对照怎么运行

```text
冻结起点、共同 W、经验/工具/模型配置、两组资源上限和评估计划
                        ↓
        M0 分支                          M1 分支
        固定 M0                          固定 M1
        Task Loop                        Task Loop
        提出 W 修改                      提出 W 修改
        └→ 固定 W 的子对照               └→ 固定 W 的子对照
           验收后本分支采用                  验收后本分支采用
        Task 继续                        Task 继续
                        ↓
            Evaluator 按冻结标准比较两组
                        ↓
          Controller 仅发布或拒绝候选 M
```

分支里的 W 不会自动成为主研究的 W。外层比较结束时保留其后继 W、方法切换、嵌套验收、
新查询结果、运行状态及实际资源使用。M 对照中途的提供商暂停必须恢复原分支，不能跳过
失败分支后宣布胜出。W 子对照禁止再提方法修改，递归权限只开放到这一层。

`EvaluationSpec.target` 明确 `workflow/meta`；M 额外冻结 `initial_workflow_ref`。
`method-loop-v1` 必须配置 `workflow_validation=independent`，防止不小心走旧在线采用路径。
当前自动回放从已有证据构建 `same_snapshot` 对照。它评估该起点下的改进过程，不是自动
产生独立新蛋白或证明通用能力。正式结果仍需要独立案例、多次对照及预留确认数据。

同一基础模型的角色调用不代表独立研究者；相同预算也不要求相同实际调用次数或候选。
主任务最终表现、改进成本及失败应联合报告，不用“接受补丁数”代替改进效果。

还有一个归因边界：现有 TaskView 保留 M 的身份和定义；本次没有重写所有任务上下文接口。
因此当前 M 对照衡量完整分支的运行表现，不能断言所有差异都只通过 W 更新产生。若要
严格证明“新版 M 更会产生有效 W”，应另将后继 W 放入同一个固定环境复测，或增加任务
输入遮蔽对照。这不影响分支权限与预算检验，但限制最终科学结论的解释范围。

## 嵌套预算：记一次，不少记

实测回放没有新增湿实验，报告中的实验货币费用为 **0**。`experimental_wells` 仍是查询/
实验孔位计数；LLM、工具及 PLM 输入继续记账，不能因为回放免费而无限读取标签。
本次没有把 LLM 的实际提供商费用假设成 0，也没有新增货币结算服务。

在外层开始前，两组及 Evaluator 的资源上限成对预留。M 分支里的 W 对照先在该分支预留，
每一次实际调用再沿 `SponsoredStore` 链逐层计入 **W 子分支 → M 分支 → 主账本**。
上层收到同一个稳定请求键；恢复不重复计费。不同分支分别发起的同序列查询
是两个决策查询，仍各占各自额度，不与同一次请求的幂等重放混淆。局部或上层拒绝资源时回滚局部预留；未知完成
状态保持阻塞，不擅自退款。对各级 usage 做总和会重复统计，根账本才是整个研究的总量。

初始合法证据复制不重新查询。未用预留在完成后释放；已发生的实验和调用不因拒绝或回退
而撤销。嵌套验收同样保留当前分支的后续查询额度。预算不足且验证尚未开始时保留提案，
继续原 W/M；已开始或结果不确定时必须先恢复或对账。

默认自动配置的 M 分支资源比 W 分支大，但仍有限。根据实际任务预算设置
`meta_rounds`、`meta_query_budget_per_arm`、`meta_llm_calls_per_arm`、
`meta_tool_calls_per_arm`。省略 M 查询上限时，默认额外预留一组 W 对照的查询空间；这并不
保证任意复杂改进都有足够预算。必须同时预算每个嵌套 Evaluator 的调用。

## 经验库与信息隔离

新研究可用 `--memory-library /path/to/capability-library`。路径必须与研究目录分开。
不提供该参数时，沿用研究内经验，无跨研究读写，也没有隐式的用户目录依赖。

`ExperienceMemory` 保留原接口，旧 import 路径兼容；`CapabilityLibrary` 提供
`record()`、`retrieve()`、`freeze()`。共享记录只含能力主张、方法修改、范围、状态和证据
引用，不自动复制完整标签、原始执行日志或最终确认结果。状态从真实 Controller 转换和
绑定的已完成验收报告得到，不接受 LLM 自填的“验证成功”。

新研究按任务类型及反馈来源检索并冻结不可变快照；LLM 判断其中经验是否适用。运行期间
不会读取公共库后来新增的内容。对照的两个分支获得相同的有效经验快照，随后各写自己的
笔记。分支不能发布共享经验；公共库的可写路径不进入 Agent 配置或工具权限。
详细证据保留来源引用，需要时由操作者通过现有授权证据机制提供，不允许 Agent 直接读取
另一个研究的任意目录。公共库写入失败被记录，不撤销已完成验证或阻断正常任务。

发布并不等于科学验证成功：共享记录的 `local_support` 仍只适用于原验收范围，
`transfer_validated` 不自动设为真。失败、拒绝和未验证记录保留，拒绝不等于生物学假设被否定。

版本选择与最终确认继续分开。确认只比较已接受的固定版本，不采用新版本，不将确认
结果写入选择记忆。检查已使用证据时也包含 M 对照内部 W 验证揭示的测量，不能把嵌套使用
过的标签重新当成保留证据。数据的独立性仍需研究者管理，换名称/种子不能制造独立性。

## 命令与方法复用

```bash
proteinrsi init --task task.json --out runs/method-study \
  --research-config configs/research.mutation.json --protein-model none \
  --memory-library capability-library
proteinrsi replay --campaign runs/method-study --dataset measurements.csv --agent llm

# 显式案例也复用同一个多轮方法评估器：
proteinrsi evaluate-meta --campaign runs/method-study \
  --cases meta-validation.json --agent llm --promote
# 已接受 M 的最终确认：不加 --promote。
proteinrsi evaluate-meta --campaign runs/method-study \
  --patch-id p-<accepted> --cases meta-confirmation.json --agent llm

proteinrsi methods history --campaign runs/method-study
proteinrsi methods export --campaign runs/method-study --target meta --out learned-meta.json
proteinrsi init --task next-task.json --out runs/next-study --meta learned-meta.json
proteinrsi trace --campaign runs/method-study --format json --out trace.json
```

导出的是原 `MetaPolicy`/`Workflow` 定义 JSON，另有 `.provenance.json` 保存来源、配置与源码
标识。仅允许导出初始或曾激活的方法，不将待验证候选冒充已发布能力。目标文件已有不同
内容时拒绝覆盖。导出不是完整运行环境迁移；新研究须重新授权工具、数据和预算，必要时
显式指定原工具配置。程序仍必须在现有隔离环境中执行。

`methods history` 展示父版本、状态、活动版本、验收关联和发布后的实际使用事件。
轨迹中的 `improvements` 摘要同样从原始记录派生“为什么改、改哪里、如何验证、是否采用、
采用后是否实际使用”。摘要最多展示最近 100 项并标记截断，完整记录不会被删去。
验证子分支调用不算主研究使用；M 的开始/决策事件也不能相加后
冒充精确的独立调用数。

## 验收与剩余边界

机制测试覆盖权限矩阵、M 内 W 验收/采用、嵌套资源守恒、祖先记账中断恢复、提供商暂停、
预算不足、最终确认隔离、可选经验库冻结、篡改检测、方法导出、采用后实际调用和回退。
脚本化模型及模拟指标工作器只验证协议和执行，不证明真实 LLM 的改进能力。真实代码沙箱
须另在支持 Landlock/seccomp 的主机通过验收，不允许不安全回退。

不新增 skill 注册服务，不自动下载蛋白模型，不启动未授权的付费模型或 GB1 效果实验。
本轮提供 M 对照的可执行与审计机制；真实改进收益需要有明确预算的后续实验。
