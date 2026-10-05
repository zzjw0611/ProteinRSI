# Resource-aware research runtime (v0.4)

This is an additional, typed inner-loop execution strategy. It runs inside the
existing Campaign and never submits experiments or publishes W/M versions itself.

## Activation and backward compatibility

New CLI `init` uses `ResearchConfig()` (adaptive) unless `--research-mode fixed`.
`--research-config configs/research.json` configures it explicitly. The configuration
and original Know-how text are stored once in SQLite. Existing saved campaigns
without `configuration/research`, and the Python `Campaign.initialize` API without
`research_config=ResearchConfig()`, retain the fixed runner. `demo --adaptive`
exercises the new code with deterministic roles and synthetic outcomes.

Do not upgrade code/dependencies halfway through a registered scientific experiment.
Use separate new campaign directories to compare old and new harnesses. Workflow
and MetaPolicy version calculation are unchanged. The adaptive run cache additionally
includes runtime version, tool schemas, research configuration, model identity,
Know-how snapshot and visible evidence.

## A bounded plan is not arbitrary code

Available plan operations:

| Operation | Logical owner | Execution and validation |
|---|---|---|
| `evidence` | C | Read-only QC and frozen-prediction error reports, using two metered context tools |
| `design` | B | Existing legal sequence/edit proposals and bounded registered-tool dialogue |
| `tool` | A | One explicitly named registered operation; cannot supply commands or paths outside its schema |
| `rank` | C | Optional analysis calls, ESMC/task-head numerical analysis when configured, then C review |
| `finalize` | A | Validate a permutation of the fresh ranking; return priorities, not an approved batch |

`ResearchStep` declares ID, operation, question, expected output, dependencies and
optional ToolCall. Owner is inferred by code, not supplied by the LLM. Plans are
ordered dependency graphs, executed serially; this is not a parallel DAG scheduler.
Only earlier IDs can be dependencies. There is exactly one terminal finalize step,
and a fresh ranking must precede it. Design is optional for supplied ranking inputs
or candidates returned by a scientific/generated-code tool. Tools/evidence after rank invalidate that ranking.
Any newly introduced candidate must pass the task's fixed positions, library,
sequence and target constraints.

`A-plan` produces the first plan. After each completed nonterminal step, `A-review`
can keep it or replace the unexecuted suffix. Completed steps cannot be edited.
Limits on plan length, total executed steps, design/analysis calls and revisions
remain enforced. Once the revision limit is reached, the remaining plan runs without
further revision calls. All JSONLLM calls share the campaign LLM budget/cache.

For example, A can change:

```
evidence → design → rank → finalize
```

to:

```
completed evidence → combination diagnostic → design → rank → finalize
```

or schedule a second design after an analysis result. Revisions of the current plan
are inner-loop adaptation, not a W/M publication. Only the existing outer gate can
publish a persistent workflow or successor MetaPolicy.

## Selection and permissions

Resources have stable IDs, content versions, categories and descriptions:
`tools`, `data_lake` (visible campaign evidence and registered artifacts only),
`libraries` (authorized Skills in this implementation), `know_how`, `experience`.
The registry is not a filesystem crawler or web retriever. It never opens hidden
label tables or imports a global directory of experimental data.

1. Tool registration and workflow whitelist restrict the candidate catalog.
2. Task kinds and outbound-data permission are checked before selection.
3. Know-how is filtered by task kind and minimum known data; experience must match
   task kind and evidence source. These are coarse eligibility rules, not a proof of transfer.
4. `rules` ranks descriptions lexically (English tokens and Chinese characters),
   `all` keeps catalog order, and `llm` invokes the adapted Biomni selector.
5. A round-robin category merge enforces max resource count and character budget;
   omitted IDs are explicit. Whole resource entries are kept or omitted, not partly
   truncated JSON schemas. TaskView's scientific constraints and observations are
   outside this supplementary resource budget and are never dropped by retrieval.

The selected subset informs A/B; the full permission-filtered catalog remains
available to C's explicit analysis step so necessary validators are not lost.
A plan revision can select resources again for the revised question. Retrieval is
not authorization: every execution still goes through the gateway. The default rules
selector deliberately does not claim semantic/vector retrieval performance.

The adapted source lives in `research/biomni_retriever.py`, is Apache-2.0, and is
actually invoked only in LLM selection mode. The surrounding resource catalog,
knowledge loader and execution state machine are original MIT implementations.

## Read-only analysis semantics

`research_evidence_summary({})` groups current observations by batch, records valid,
failed, inconclusive and unavailable counts and descriptive WT statistics. It does not assume all
controls are WT, estimate assay drift causally, normalize values or exclude rows.

`research_prediction_errors({})` matches currently revealed rows against predictions
saved in their submitted Batch, excluding designated controls and missing predictions.
It reports MAE, signed bias, tied-rank Spearman where defined and errors by mutation
depth. It does NOT retrospectively fit a new head then score its own training labels.
Technical replicates remain dependent; metrics are descriptive, not out-of-sample
confidence or causal attribution. External engines must not put unrelated energy/
confidence scores into Candidate.predicted_value: this field is for task-outcome
predictions only.

`research_combination_effects({"scale":"linear", "pooling":"within_batch"})`
compares measured combinations with measured WT and constituent singles:

```
linear null = WT + sum(single_i - WT)
log null    = log(WT) + sum(log(single_i) - log(WT))
```

For k substitutions this gives a scale-dependent descriptive deviation, not an
interaction p-value. Technical replicates are averaged on the recorded scale before
an optional log transform. Log mode requires positive relevant means. The default
requires all constituents in the same batch. `pooling=pooled` is explicit and carries
a cross-batch confounding warning. Missing constituents are returned as missing,
never replaced with ESMC scores, a fitted head or hidden dataset queries.

All three functions are bound to the current TaskView; the LLM cannot supply input
measurement tables, SQL, paths or arbitrary numerical labels. Results are persisted
under `research_analysis`. The optional `research_python` tool runs generated Python
in a separate bounded process; it has no controller RPC, network, database or label
authority. No unrestricted R/Bash REPL is enabled.

## Persistence, failures and experiment boundary

SQLite stores `resource_selections`, `research_runs`, `research_step_outputs` and
`research_analysis`. A run records its plan, completed-prefix ledger, current pool,
rank, actual outputs, reviews and revisions. A completed run is cached. An external
interruption can resume from completed steps; successful tool and LLM request IDs
prevent duplicate effects. These guarantees assume the same code/config/dependency
versions and the existing single-writer campaign lock.

A normal exception marks the run `blocked`; repeated invocation does not silently
resubmit a failed job. The operator must investigate, reconcile external work, and
choose a reviewed new run/strategy. There is no automatic "clear all caches and retry"
button. An uncertain remote LLM call can remain blocked by the underlying JSONLLM
client even when the plan checkpoint exists. This is deliberate, not full distributed
transactional recovery or a scheduler.

The returned ranking is sent to the existing batch builder. Only that trusted path
reserves experiments; only explicit approval commits the batch. On import, measured
facts are retained even when later reasoning fails. QC/error reports and relevant
plan/revision summaries are added to the round history seen by M. No plan command
can write measurements, alter assay definitions, change budgets or promote itself.

## Outer loop and paired Meta evaluation

A Meta proposal still goes through Patch validation and the existing experimental or
independent Meta gate. Both offspring in the numeric Meta evaluator receive the same
research configuration, Know-how snapshot, frozen protein model and resource limits.
Their evidence, computation caches and outputs remain separate. Changing the current
plan, adding a note or rerunning a fit is not by itself RSI.

The restricted numeric Meta evaluator remains one-step frozen-improver evaluation,
not a demonstration of arbitrary recursive code improvement. Structural-engine
campaigns require case-scoped artifact provisioning and are rejected by that generic
evaluator. General cross-protein experience transport is not implemented here.

## Inspecting a run

```bash
proteinrsi research plans --campaign runs/my-campaign
proteinrsi research resources --campaign runs/my-campaign
proteinrsi research analyze --campaign runs/my-campaign
```

These inspection commands do not invoke an LLM or protein inference. `analyze`
persists descriptive derived results; it neither changes scientific observations nor
triggers Meta promotion. New experiments still require the normal approval/import flow.

## Validation boundaries

Tests use real local controller, SQLite and NumPy computations with synthetic numbers,
scripted LLM objects or mocked HTTP transport. They demonstrate routing, replan
constraints, evidence isolation and accounting, NOT better protein outcomes. Compare
fixed versus adaptive, rules versus LLM retrieval, and single versus multiple research
roles under matched total budgets in a separate scientific study. The one-Agent
ablation and statistically powered biological comparisons are not shipped benchmarks.

## Goal-driven research and computational rounds

`goal.py` resolves a natural-language objective against supplied scientific inputs and
the deployed tool catalog. It asks for missing essentials and selects an experimental
or computational route. Task-specific sequence constraints live in TaskSpec, not in
a GB1-specific checker. `computational.py` records each candidate/metric iteration
and gives actual outputs to the next plan, without creating Observation records or
lab charges. Purely computed evidence does not pass the measured Meta promotion gate.

`research_python` is available in new goal-driven studies. Programs see only revealed
context, explicit JSON inputs and selected artifact files. They may compute metrics,
transform candidates, and return scientific file contents through write_artifact.
Source is saved in programs/<hash>.py; gateway records include inputs, output, errors
and provenance. Failures are results the next plan review can repair. Budget and
fixed CPU/memory/output limits remain outside generated-code control.


### LLM 临时故障恢复

LLM 请求默认最多发送 4 次（首次 + 3 次重试），可通过 `PROTEINRSI_LLM_MAX_ATTEMPTS=1..10` 调整。HTTP 408/429/500/502/503/504，以及连接、读写超时或临时网络错误会自动重试；等待时间按 2、4、8 秒增长并加少量随机延迟，参考服务返回的 `Retry-After`（最多等待 60 秒）。认证、参数错误及无效模型输出不自动重试。

每次发送独立计入 LLM 调用预算，失败也不退款；服务未返回 token usage 时不推测 token 或金额。网络超时后的重发可能在服务端产生额外推理费用。实验查询预算不受 LLM 重试影响。

每次尝试保存在 `llm_attempts`，验证分支同步到 `validation_llm_attempts`；轨迹包含尝试次数、等待时间、状态码、请求 ID 和脱敏后的有限长度错误响应。成功结果继续缓存。旧的明确临时失败可在恢复运行时重试，保留原失败记录；重试次数跨恢复累计。已达上限或进程中断留下的 `started` 不明状态仍需检查审计，不会无限重发。调整重试配置不会改变请求或研究缓存身份。


## Open design, ranking and experimental feedback

Natural-language design with historical replay now constructs an open TaskSpec:
`candidate_access=open`, `candidates=[]`. The controller privately loads the assay
index. It does not put measured sequence identities into a task, sample 128 items,
or register library browsing tools. Design validity depends on the user's sequence
constraints; actual historical availability is evaluated only by the feedback backend.
A/B can generate full sequences, explicit residue edits or use optional scientific
engines/generated Python. Ranking then orders those generated proposals. Empty
proposals cannot silently claim that an open design space is exhausted.

A user-requested ranking task retains its explicitly supplied set, including when
it exceeds the old 128-item preview size. Legacy explicit closed-library tasks remain
supported. All natural-language entry points and the GB1 preparation helper default
to open design; `prepare_gb1_run.py --library` is an explicit closed-library override.

Submitted open-replay queries with no historical record return `unavailable` with
`value=null`. They count toward submitted-query budgets, never enter phenotype
training or best-fitness calculations, and are separate from assay/QC failures.
The next round sees the missing identities and can revise its search. Trial results
with unavailable non-control samples are inconclusive and cannot promote W or M.
Reports separate valid query measurements, unavailable queries and provided parent
observations. An unavailable row cannot be labeled as a new wet-lab measurement.

These changes do not certify the scientific quality of a model's design strategy.
Tests exercise natural-language task construction, HTTP-client role calls, guarded
workers, generated edits, sparse feedback, next-round adaptation, budget accounting
and private-index isolation using artificial fixtures, not real benchmark experiments.
Existing catalogue-screening campaigns retain their recorded evidence and cannot
silently resume as an open-design experiment.

## 整板实验与候选排序

自然语言目标可明确要求“10 轮，每轮必须用满 384 孔”，启动时还可加
`--full-plate` 强制启用 `TaskSpec.batch_fill_policy=full_plate`。
每轮容量由任务的 `batch_size` 决定，不固定为 384；总查询预算必须是整板倍数。
已提供的母本测量不占孔；显式配置的板内对照占孔，其余孔用于不同的新候选。
开放设计不再向 Agent 暴露旧的 `proposal_pool_size=128` 字段。

控制器在同一轮内最多发起 6 次候选补齐请求，将多个科学假设的小批次合并成整板。
不足一板时停止并记录缺口，不提交部分板、不预占实验预算、不推进轮次，
也不会用随机序列静默填孔；已经发生的 LLM/工具费用仍计费。
补齐次数跨恢复累计，不会因重新启动而无限重试。
候选的科学选择、蛋白工具调用和各探索方向的比例仍由 Agent 决定。

W/M 验证按实际可用候选分配等量、不重叠的比较组，满足预设最小样本量，
不要求各有半板候选。分组在查询前固定；剩余孔标记为 `research`，
其结果进入后续研究证据，但不进入该次方法优劣检验。
完全相同的候选优先级不消耗比较查询；不足以比较时记录 inconclusive。
M 获得已用计算预算和方法验证结果，所有角色可看到剩余轮次、有效查询容量及无法结转的孔位。

排序使用由序列生成的稳定 `candidate_id`，而非要求 LLM 抄写完整蛋白序列。
非法 ID、遗漏或重复触发有界修复（合计最多 3 次模型调用）；代码不猜测或修改序列。
板补齐请求、验证分组、排序修复、每轮孔位利用率均进入结构化记录和轨迹。
这些语义适用于新建整板研究；已有部分板研究的历史不会被重写。
