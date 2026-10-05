# 上次 GB1 研究：逐轮设计理由原文

来源：本目录 state.sqlite3 的 research_runs、batches 与已揭示 observations。这里只导出模型已返回的说明；候选理由写于测量前，表内 fitness 为之后揭示的历史实测值。未读取完整标签数据库。

第 1–7 轮已提交；第 8 轮只有方案，未提交。方法分支方案不等于已经执行或验收通过。

## 第 1 轮

### 方案 w-065f689c980923d5（状态 complete）

记录 ID：`578808c5b9fa6d5b4954dd96d35bbba2a5ac03738b729280e59541530487ee1f`

假设：首轮以全部76个合法单点替换建立局部实测基线，同时加入48个双点变体和4个四点变体探索相互作用，可以在128次新查询内兼顾后续设计所需的信息与发现高fitness变体的机会。这是待检验的搜索策略；目前只有母本fitness=1.0的实测证据，不能据此预测任何替换的收益。

选择理由：当前只有免费提供的母本实测值，适合先建立全面的单点基线，并用少量组合检验单点之外的搜索机会。128个候选符合proposal_pool_size，低于384个单轮上限，保留后续轮次根据反馈调整的预算。一次Python调用有明确用途：生成完整序列并检查硬约束；本轮无需蛋白模型评分，也不拟合只有一个观测支持的fitness模型。所有步骤均为计划，尚未执行或取得新测量。

排序安排：对工具实际返回的合法候选重新评估，以信息价值和覆盖度排序：先按位点轮转排列76个单点变体，再按位点对轮转排列48个双点变体，最后排列4个四点探索变体。保留全部128个候选用于同一批次；排序不表示预计fitness高低，所有候选的数值fitness预测为null。若生成或检查失败，则先修复，未通过检查者不得提交。

### 实际提交 128 条

| 母本相对突变 | 候选理由原文 | 后续揭示 fitness |
|---|---|---|
| V39A | single: 39V>A. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.061909656 |
| D40A | single: 40D>A. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 2.401243496 |
| G41A | single: 41G>A. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.120576127 |
| V54A | single: 54V>A. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.372949333 |
| V39C | single: 39V>C. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.242237278 |
| D40C | single: 40D>C. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.838187182 |
| G41C | single: 41G>C. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.06071262 |
| V54C | single: 54V>C. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.413935906 |
| V39D | single: 39V>D. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.00647209 |
| D40E | single: 40D>E. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.860930664 |
| G41D | single: 41G>D. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.005953787 |
| V54D | single: 54V>D. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.031541991 |
| V39E | single: 39V>E. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.032719185 |
| D40F | single: 40D>F. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 3.054548651 |
| G41E | single: 41G>E. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.006434566 |
| V54E | single: 54V>E. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.035307131 |
| V39F | single: 39V>F. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.377100728 |
| D40G | single: 40D>G. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.864025848 |
| G41F | single: 41G>F. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.067022094 |
| V54F | single: 54V>F. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.820788194 |
| V39G | single: 39V>G. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.008297906 |
| D40H | single: 40D>H. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 3.081165808 |
| G41H | single: 41G>H. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.004347009 |
| V54G | single: 54V>G. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.265820486 |
| V39H | single: 39V>H. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.026479849 |
| D40I | single: 40D>I. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.791713331 |
| G41I | single: 41G>I. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.003424055 |
| V54H | single: 54V>H. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.161839956 |
| V39I | single: 39V>I. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.445905086 |
| D40K | single: 40D>K. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.137884775 |
| G41K | single: 41G>K. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.004075387 |
| V54I | single: 54V>I. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.377105031 |
| V39K | single: 39V>K. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.020160656 |
| D40L | single: 40D>L. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.907305261 |
| G41L | single: 41G>L. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.009770061 |
| V54K | single: 54V>K. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.007655337 |
| V39L | single: 39V>L. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.690163554 |
| D40M | single: 40D>M. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.0935288 |
| G41M | single: 41G>M. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.013110316 |
| V54L | single: 54V>L. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.380472898 |
| V39M | single: 39V>M. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.170549883 |
| D40N | single: 40D>N. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.802922799 |
| G41N | single: 41G>N. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.005746208 |
| V54M | single: 54V>M. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.585648854 |
| V39N | single: 39V>N. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.011627896 |
| D40P | single: 40D>P. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.402165979 |
| G41P | single: 41G>P. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.004904716 |
| V54N | single: 54V>N. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.22509206 |
| V39P | single: 39V>P. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.00894644 |
| D40Q | single: 40D>Q. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.113893638 |
| G41Q | single: 41G>Q. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.004179375 |
| V54P | single: 54V>P. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.007556195 |
| V39Q | single: 39V>Q. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.102672418 |
| D40R | single: 40D>R. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.645471649 |
| G41R | single: 41G>R. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.004352436 |
| V54Q | single: 54V>Q. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.124301624 |
| V39R | single: 39V>R. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.017201864 |
| D40S | single: 40D>S. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 2.16359336 |
| G41S | single: 41G>S. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.013884301 |
| V54R | single: 54V>R. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.006167567 |
| V39S | single: 39V>S. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.034312109 |
| D40T | single: 40D>T. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.165465363 |
| G41T | single: 41G>T. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.004107048 |
| V54S | single: 54V>S. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.495759852 |
| V39T | single: 39V>T. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.246551701 |
| D40V | single: 40D>V. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 1.947667868 |
| G41V | single: 41G>V. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.015988627 |
| V54T | single: 54V>T. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.615188985 |
| V39W | single: 39V>W. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.010396949 |
| D40W | single: 40D>W. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 3.901460756 |
| G41W | single: 41G>W. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.007521238 |
| V54W | single: 54V>W. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.699995429 |
| V39Y | single: 39V>Y. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.072022513 |
| D40Y | single: 40D>Y. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 3.891461091 |
| G41Y | single: 41G>Y. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.013714474 |
| V54Y | single: 54V>Y. Measure the individual substitution effect to establish a complete local baseline; benefit is unknown. | 0.028526258 |
| V39A/D40A | double: 39V>A, 40D>A. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.274083335 |
| V39A/G41A | double: 39V>A, 41G>A. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | unavailable |
| V39A/V54A | double: 39V>A, 54V>A. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.120596667 |
| D40A/G41A | double: 40D>A, 41G>A. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.259548613 |
| D40A/V54A | double: 40D>A, 54V>A. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 1.751532552 |
| G41A/V54A | double: 41G>A, 54V>A. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 4.157983125 |
| V39F/D40F | double: 39V>F, 40D>F. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 3.600575884 |
| V39F/G41F | double: 39V>F, 41G>F. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.42456046 |
| V39F/V54F | double: 39V>F, 54V>F. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.212318076 |
| D40F/G41F | double: 40D>F, 41G>F. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.058867783 |
| D40F/V54F | double: 40D>F, 54V>F. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 2.888281388 |
| G41F/V54F | double: 41G>F, 54V>F. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.005075617 |
| V39W/D40W | double: 39V>W, 40D>W. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.370586499 |
| V39W/G41W | double: 39V>W, 41G>W. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.044969826 |
| V39W/V54W | double: 39V>W, 54V>W. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.196534918 |
| D40W/G41W | double: 40D>W, 41G>W. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.010427083 |
| D40W/V54W | double: 40D>W, 54V>W. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 2.357452978 |
| G41W/V54W | double: 41G>W, 54V>W. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.0 |
| V39Y/D40Y | double: 39V>Y, 40D>Y. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 2.403087625 |
| V39Y/G41Y | double: 39V>Y, 41G>Y. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.009492064 |
| V39Y/V54Y | double: 39V>Y, 54V>Y. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.06276278 |
| D40Y/G41Y | double: 40D>Y, 41G>Y. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.013140982 |
| D40Y/V54Y | double: 40D>Y, 54V>Y. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.217005076 |
| G41Y/V54Y | double: 41G>Y, 54V>Y. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.0 |
| V39L/D40I | double: 39V>L, 40D>I. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 1.729098742 |
| V39L/G41I | double: 39V>L, 41G>I. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.002476657 |
| V39L/V54I | double: 39V>L, 54V>I. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.472257754 |
| D40L/G41I | double: 40D>L, 41G>I. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.002509921 |
| D40L/V54I | double: 40D>L, 54V>I. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.191202782 |
| G41L/V54I | double: 41G>L, 54V>I. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.0 |
| V39I/D40L | double: 39V>I, 40D>L. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.546493282 |
| V39I/G41L | double: 39V>I, 41G>L. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.010541667 |
| V39I/V54L | double: 39V>I, 54V>L. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.218452559 |
| D40I/G41L | double: 40D>I, 41G>L. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.029712574 |
| D40I/V54L | double: 40D>I, 54V>L. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.759897565 |
| G41I/V54L | double: 41G>I, 54V>L. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.001274806 |
| V39S/D40T | double: 39V>S, 40D>T. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.073442935 |
| V39S/G41T | double: 39V>S, 41G>T. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.0 |
| V39S/V54T | double: 39V>S, 54V>T. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.156510687 |
| D40S/G41T | double: 40D>S, 41G>T. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.00938849 |
| D40S/V54T | double: 40D>S, 54V>T. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.961935932 |
| G41S/V54T | double: 41G>S, 54V>T. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.019647551 |
| V39T/D40S | double: 39V>T, 40D>S. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 1.166588878 |
| V39T/G41S | double: 39V>T, 41G>S. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.0 |
| V39T/V54S | double: 39V>T, 54V>S. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.370520064 |
| D40T/G41S | double: 40D>T, 41G>S. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.022255933 |
| D40T/V54S | double: 40D>T, 54V>S. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.37193458 |
| G41T/V54S | double: 41G>T, 54V>S. Explore small, aromatic, hydrophobic or polar residue combinations and compare with the measured constituent singles; interaction is unknown. | 0.002661003 |
| V39A/D40A/G41A/V54A | quadruple: 39V>A, 40D>A, 41G>A, 54V>A. Probe a four-position combination spanning different side-chain compositions; this is exploratory and has no established fitness advantage. | 1.611610009 |
| V39F/D40L/G41A/V54I | quadruple: 39V>F, 40D>L, 41G>A, 54V>I. Probe a four-position combination spanning different side-chain compositions; this is exploratory and has no established fitness advantage. | 0.036581965 |
| V39W/D40Y/G41A/V54F | quadruple: 39V>W, 40D>Y, 41G>A, 54V>F. Probe a four-position combination spanning different side-chain compositions; this is exploratory and has no established fitness advantage. | 0.149314056 |
| V39Y/D40F/G41S/V54L | quadruple: 39V>Y, 40D>F, 41G>S, 54V>L. Probe a four-position combination spanning different side-chain compositions; this is exploratory and has no established fitness advantage. | 0.004480505 |

## 第 2 轮

### 方案 w-065f689c980923d5（状态 complete）

记录 ID：`3d1eaff95db295dba4f4efb8b7d386cc00617a9652e3a6d9fd4c1b055111c123`

假设：本轮检验一个有界策略改变：将首轮固定残基配对探索改为反馈驱动的8个双点组合，以母本为背景，组合D40{F,H,W,Y}和V54{A,C}，保持39V、41G及所有其他残基不变。D40这4个替换和V54这2个替换的单点实测值均高于母本，但组合收益仍未知。预先定义局部成功标准为至少一个有效历史实测结果超过当前最佳reported_assay_fitness=4.157983125；此假设不包含数值fitness预测，也不代表已经提交查询。

选择理由：主要策略局限是首轮组合采用固定配对规则，对已揭示的有利单点利用有限；当前证据没有显示测量故障或搜索平台期。本轮只改变组合选择规则，用8个新双点变体检验反馈驱动设计。D40W/Y的实测单点值接近当前最佳，D40F/H提供同一局部面板中的替换多样性，V54A/C均有高于母本的单点记录。已测D40A/V54A和G41A/V54A说明组合不能从单点简单外推，因此不使用加性数值预测。已有证据足以提出这8个显式设计，无需蛋白模型调用；本计划中的工具调用均为null。所有步骤是待执行安排，不声称已完成新的计算核验或测量。

排序安排：只对通过运行时核验的候选排序。将D40W和D40Y组合置于第一优先层，D40H和D40F组合置于第二优先层；层内采用固定顺序，得到D40W/V54C、D40W/V54A、D40Y/V54C、D40Y/V54A、D40H/V54C、D40H/V54A、D40F/V54C、D40F/V54A。顺序用于查询安排，不构成组合fitness预测；W与Y、H与F及C与A的单点小差异没有重复测量支持可靠区分。保留全部8个合法候选，以完整覆盖这个局部面板。

### 实际提交 8 条

| 母本相对突变 | 候选理由原文 | 后续揭示 fitness |
|---|---|---|
| D40W/V54C | D40W/V54C：检验D40W单点实测3.901460756与V54C单点实测1.413935906组合后能否进一步提高fitness；组合效应未知。 | 4.239155578 |
| D40W/V54A | D40W/V54A：检验D40W与有利单点V54A的组合，并与D40W/V54C比较第54位的背景依赖。 | 2.013521848 |
| D40Y/V54C | D40Y/V54C：检验D40Y单点实测3.891461091与V54C组合是否保留或提高收益；不假定单点效应可加。 | 4.026443968 |
| D40Y/V54A | D40Y/V54A：与D40Y/V54C形成配对比较，检验第54位A或C在D40Y背景中的组合收益。 | 2.40599753 |
| D40H/V54C | D40H/V54C：D40H单点实测3.081165808高于母本；检验这一替换与V54C的组合，扩大局部反馈驱动面板的覆盖。 | 3.158259117 |
| D40H/V54A | D40H/V54A：与D40H/V54C比较，检验V54A在D40H背景中是否带来进一步收益。 | 2.788874921 |
| D40F/V54C | D40F/V54C：D40F单点实测3.054548651高于母本；检验与V54C组合的收益，避免仅依赖W和Y两个背景。 | 3.703517871 |
| D40F/V54A | D40F/V54A：完成D40{W,Y,H,F}与V54{C,A}的8个双点组合，检验V54A在D40F背景中的收益。 | 1.70807468 |

## 第 3 轮

### 方案 w-065f689c980923d5（状态 complete）

记录 ID：`82f93c865967e6425a429f7673b184eaac23cb16a6487c6269777fcb12c8f573`

假设：本轮只改变组合背景：在已测G41A/V54A背景上加入D40{F,H,W,Y}，生成4个新三点变体，检验这些在母本背景中有利的D40替换是否仍能提高fitness。预先区分两个标准：至少一个有效结果超过背景基线4.157983125，表示本面板实现背景收益；至少一个超过当前全局最佳4.239155578，表示发现新的最佳实测变体。组合收益未知，不给出数值fitness预测。本轮最多使用4个新查询，无需蛋白模型调用；以下步骤均为待执行计划。

选择理由：最新面板实现了局部提升，没有明确QC故障或持续平台期证据。当前策略的主要限制是有利单点组合表现依赖背景：D40W/V54A低于D40W单点，但G41A/V54A远高于其两个单点。本轮用4个三点变体检验D40替换在这一已测高fitness背景中的可迁移性，改变范围明确且可被实际反馈否证。该面板兼顾发现新最佳与检验背景收益，两个阈值分别报告。现有实测证据足以生成候选；ESMC的WT背景加性先验不能回答本轮的组合收益问题，因此不安排科学工具调用。序列、身份、权限与预算检查仍须在提交前执行。

排序安排：重新评估通过检查的候选，查询顺序为D40W/G41A/V54A、D40Y/G41A/V54A、D40H/G41A/V54A、D40F/G41A/V54A。W/Y优先的依据是其母本背景单点值较高，以及W/C组合取得当前最佳；这些证据仅支持查询优先级，不能预测三点fitness。H/F保留替换多样性，层内顺序用于固定查询安排，不将接近的单点差异解释为可靠效应差异。保留全部4个合法身份，预测值及数值置信度继续为null。

### 实际提交 4 条

| 母本相对突变 | 候选理由原文 | 后续揭示 fitness |
|---|---|---|
| D40W/G41A/V54A | D40W/G41A/V54A：检验母本背景中有利的D40W替换能否进一步提高已测G41A/V54A背景的fitness。D40W单点及D40W/V54C的实测结果支持优先查询，但不能预测此三点组合的收益。 | 6.123804795 |
| D40Y/G41A/V54A | D40Y/G41A/V54A：检验D40Y的有利单点效应是否能迁移至G41A/V54A背景，并与D40W三点组合比较；组合fitness尚未知。 | 5.156918406 |
| D40H/G41A/V54A | D40H/G41A/V54A：加入母本背景中有利的D40H替换，检验该背景的收益是否适用于不同D40残基；不从单点值外推三点fitness。 | 5.110623519 |
| D40F/G41A/V54A | D40F/G41A/V54A：检验D40F在同一G41A/V54A背景上的作用，完成预先指定的四残基面板；有利单点不保证组合改善。 | 4.293553734 |

## 第 4 轮

### 方案 w-065f689c980923d5（状态 complete）

记录 ID：`43e5952c2807afc0d70a9e43576f92ef76f66fac179f12d4a91857a14c4361f7`

假设：本轮检验一个最多4个新查询的背景迁移假设：保持39V和41A，在D40{W,Y,H,F}/G41A/V54A这4个已测背景中，仅将54位A改为C。此前41G背景下，这4种D40替换的V54C组合均高于对应V54A组合；该现象是否能迁移到41A背景仍未知。分别检验每个新候选是否超过其对应已测A变体，以及是否至少一个超过当前最佳reported_assay_fitness=6.123804795。以下为待执行计划，不包含数值fitness预测。

选择理由：最新4个三点A变体均超过此前最佳，因此当前证据支持继续检验局部组合背景，而非认定平台期或测量故障。此前固定41G时，4种D40背景中的C组合均高于对应A组合；本轮仅改变54位，可直接检验这一方向是否迁移到41A背景，同时探索更高实测fitness。使用4个配对候选使结果可按D40背景解释，查询成本明确。现有证据足以提出显式设计，无需蛋白模型或额外科学计算；所有tool_call均为null。序列、身份、权限和预算核验仍是提交前必须完成的运行时检查。

排序安排：重新评估通过核验的候选，优先顺序为D40W/G41A/V54C、D40Y/G41A/V54C、D40H/G41A/V54C、D40F/G41A/V54C。W优先依据是对应A变体为当前最佳，且W在41G/C背景也取得该局部面板最高实测值；Y/H/F保留对背景迁移的比较覆盖。此顺序用于查询安排，不是新候选fitness预测；Y与H的接近实测值缺少独立重复支持可靠区分。保留全部4个合法候选，数值预测及置信度为null。

### 实际提交 4 条

| 母本相对突变 | 候选理由原文 | 后续揭示 fitness |
|---|---|---|
| D40W/G41A/V54C | 母本相对替换D40W/G41A/V54C。与已测D40W/G41A/V54A配对，仅改变54位A→C，检验是否超过对应A基线6.123804795；该基线也是当前全局提升阈值。 | 2.59276534 |
| D40Y/G41A/V54C | 母本相对替换D40Y/G41A/V54C。与已测D40Y/G41A/V54A配对，仅改变54位A→C，检验是否超过对应A基线5.156918406。 | 2.978737683 |
| D40H/G41A/V54C | 母本相对替换D40H/G41A/V54C。与已测D40H/G41A/V54A配对，仅改变54位A→C，检验是否超过对应A基线5.110623519。 | 3.28257619 |
| D40F/G41A/V54C | 母本相对替换D40F/G41A/V54C。与已测D40F/G41A/V54A配对，仅改变54位A→C，检验是否超过对应A基线4.293553734。 | 2.112405515 |

## 第 5 轮

### 方案 w-065f689c980923d5（状态 complete）

记录 ID：`3ba0b6656170a8c3f3547a3bac5c22b099973dd3ce149ba9ef009865d10d5781`

假设：本轮检验一个最多3查询的局部策略改变：保留当前最佳变体的40W/41A/54A背景，仅将39V分别替换为L、I、M。这3种39位替换的母本背景实测值均高于母本，但最新A/C比较已表明替换收益可能随背景反转，因此单点证据仅支持提出检验，不保证四点组合提高fitness。预先定义成功标准为至少一个有效reported_assay_fitness超过当前最佳6.123804795；所有新候选的数值fitness预测及数值置信度保持null。以下步骤为待执行计划。

选择理由：最新4个A/C配对全部出现C低于A，直接限制了将一个背景中的有利替换方向迁移到另一个背景的策略。保留已测最佳40W/41A/54A背景，仅改变39位，使本轮问题明确且只需3个新查询。L、I、M的单点实测值均高于母本，为候选选择提供可见依据，但不作为加性预测。当前无明确测定失败或持续平台期证据；主要问题是背景依赖。现有证据足以生成该面板，不安排科学工具调用。所有新序列仍须通过提交前的运行时检查，本计划不声称已完成新的计算核验或测量。

排序安排：仅排序通过运行时核验的候选，优先顺序为V39L/D40W/G41A/V54A、V39I/D40W/G41A/V54A、V39M/D40W/G41A/V54A。顺序依据是母本背景中L、I、M的实测单点值依次降低，同时保留全部3个候选用于同一局部检验。这是查询优先级，不是四点fitness预测；单点差异没有独立重复支持，不能保证组合顺序。数值预测与置信度继续为null。

### 实际提交 3 条

| 母本相对突变 | 候选理由原文 | 后续揭示 fitness |
|---|---|---|
| V39L/D40W/G41A/V54A | V39L/D40W/G41A/V54A：在当前最佳40W/41A/54A背景中检验39L的局部收益。V39L单点实测fitness为1.690163554，高于母本1.0，支持优先查询；四点组合收益尚未知。 | 6.027488546 |
| V39I/D40W/G41A/V54A | V39I/D40W/G41A/V54A：保持当前最佳三点背景，检验39I是否提供进一步收益。V39I单点实测fitness为1.445905086，但不能将该单点效应加到最佳背景上预测四点fitness。 | 6.042077639 |
| V39M/D40W/G41A/V54A | V39M/D40W/G41A/V54A：检验另一种疏水侧链在最佳背景中的局部作用。V39M单点实测fitness为1.170549883，提供探索依据；是否改善四点组合需由实际反馈确定。 | 4.492225858 |

## 第 6 轮

### 方案 w-065f689c980923d5（状态 complete）

记录 ID：`0d823ae96a60405a48cb34ff4566e49af01f1653a0fad1c0f39f0f82e1213ed9`

假设：本轮检验一个3查询的策略改变：不再仅依据母本背景有利单点筛选候选，而是在已测40W/54A背景中探索41S、41T、41C。G41A单点fitness仅为0.120576127，但D40W/G41A/V54A达到当前最佳6.123804795，说明低fitness单点可能在组合背景中获得补偿；这一证据支持提出其他41位替换的检验，但不能保证收益。预先定义成功为至少一个有效reported_assay_fitness超过6.123804795；另行比较保留41G的背景基线2.013521848。所有新候选的数值fitness预测及数值置信度保持null。以下均为待执行计划。

选择理由：最新39L/I/M面板没有达到预定提升标准，限制了仅按母本背景有利单点选择组合的规则。G41A从母本单点0.120576127到40W/54A组合6.123804795的可见结果，为探索其他41位替换提供了背景补偿的描述性依据。本轮固定39V、40W、54A，仅检验41S/T/C，问题明确且最多消耗3次新查询。没有证据保证这些候选提高fitness，也不能把母本单点低值解释为组合不可行。现有证据足以生成这个面板，无需科学工具调用；所有tool_call为null。序列身份、固定残基、去重、权限及预算检查仍须在运行时完成。

排序安排：对通过核验的候选重新评估，顺序为D40W/G41S/V54A、D40W/G41C/V54A、D40W/G41T/V54A。S优先用于检查从已成功的41A背景增加羟基侧链的局部变化；C和T随后提供不同侧链化学性质及体积的比较。该顺序是有限探索的安排，没有组合实测证据支持fitness高低排序；不按母本背景低fitness排除任何一个。保留全部3个合法候选，数值预测及置信度为null。

### 方案 w-47f818520c6ad35f（状态 complete）

记录 ID：`ad3df3f1dba4776285d6f5170052d33512fc8a81b3da673e13410529760a72b9`

假设：本轮将局部搜索从母本单点筛选的小面板改为最佳实测背景上的完整单位置邻域：固定39V、40W、54A，枚举41位全部标准氨基酸替换，排除已提交身份后保留18个新候选。G41A在母本背景中的低值与其在40W/54A背景中的高值支持优先检查41位的背景依赖，但不能预测其他残基的收益。预先定义本轮成功为至少一个有效reported_assay_fitness超过当前最佳6.123804795。该方案最多消耗18个新查询，以下均为待执行步骤，不安排科学工具调用。

选择理由：最新39L/I/M面板未提升，说明继续仅按母本有利单点选择少量替换存在局限。workflow.designer_prompt要求在这种情况下开展最佳背景上的完整单位置邻域。41位具有直接的组合补偿证据，且在40W/54A背景尚未系统覆盖，因此选择该位点，保留全部18个未提交替换。该改变能够检验完整41位邻域是否包含更高实测fitness，同时避免遗漏母本单点低值但组合可能有利的残基。最新记录均valid，无显式测定失败；没有冻结数值预测，不能诊断拟合模型误差。现有证据足以生成并排序该面板，科学工具调用不提供本轮必需的信息，故全部tool_call为null。序列、身份、权限和预算运行时检查仍须执行。

排序安排：重新评估通过核验的候选。18个新身份均缺少匹配40W/54A背景的实测值，因此采用固定氨基酸字母顺序：D40W/G41C/V54A、D40W/G41D/V54A、D40W/G41E/V54A、D40W/G41F/V54A、D40W/G41H/V54A、D40W/G41I/V54A、D40W/G41K/V54A、D40W/G41L/V54A、D40W/G41M/V54A、D40W/G41N/V54A、D40W/G41P/V54A、D40W/G41Q/V54A、D40W/G41R/V54A、D40W/G41S/V54A、D40W/G41T/V54A、D40W/G41V/V54A、D40W/G41W/V54A、D40W/G41Y/V54A。保留全部合法候选；顺序只用于查询安排，预测值及数值置信度保持null。

### 实际提交 3 条

| 母本相对突变 | 候选理由原文 | 后续揭示 fitness |
|---|---|---|
| D40W/G41S/V54A | D40W/G41S/V54A：固定40W/54A背景，检验41S的小型羟基侧链是否能产生背景补偿收益；组合fitness未知。 | 0.230585889 |
| D40W/G41C/V54A | D40W/G41C/V54A：固定40W/54A背景，以41C的含硫侧链提供化学性质比较；没有结构或测量证据支持具体相互作用。 | 5.359942652 |
| D40W/G41T/V54A | D40W/G41T/V54A：固定40W/54A背景，以41T检验相较S增加甲基和侧链分支后的变化；不假定其提高fitness。 | 0.009736531 |

## 第 7 轮

### 方案 w-065f689c980923d5（状态 complete）

记录 ID：`0a23eafbce2b4ff3dd76a2a233a21e5940b57bf4ad3b3ebde86ed5bad8b57f1b`

假设：本轮检验一个有界策略改变：将41位的少量侧链探索扩展为当前最佳40W/54A背景上的完整单位置邻域，提交尚未查询的15种41位替换。最新41S、41C、41T结果分别为0.230585889、5.359942652、0.009736531，均未超过当前最佳41A的6.123804795；这些结果不能代表其余替换。预先定义成功为至少一个有效reported_assay_fitness超过6.123804795。本轮最多消耗15个新查询，所有候选数值fitness预测及数值置信度保持null；以下均为待执行计划。

选择理由：最新实际执行的3查询面板没有改善最佳值，但41C达到5.359942652，说明不同41位残基在同一背景中表现差异明显。当前主要策略限制是邻域覆盖不足及母本单点效应难以迁移到组合背景；没有明确测定故障或已拟合模型失败证据。补全其余15种41位替换能直接检验当前最佳在这一坐标上的剩余提升机会，成本明确且避免重复。历史中另一方案提出18候选，但实际反馈只包含S/C/T，因此本轮按实际身份排除这三项。当前designer_prompt仅要求使用提供证据提出合法变体；完整邻域是本轮自主策略选择。现有证据足以提出显式设计，不安排科学工具调用；提交前的运行时约束检查仍必须执行。

排序安排：重新评估合法候选后，保留全部15个，并采用固定字母顺序：D40W/G41D/V54A、D40W/G41E/V54A、D40W/G41F/V54A、D40W/G41H/V54A、D40W/G41I/V54A、D40W/G41K/V54A、D40W/G41L/V54A、D40W/G41M/V54A、D40W/G41N/V54A、D40W/G41P/V54A、D40W/G41Q/V54A、D40W/G41R/V54A、D40W/G41V/V54A、D40W/G41W/V54A、D40W/G41Y/V54A。没有匹配背景测量支持这些新候选的fitness排序；顺序仅用于查询安排，数值预测及置信度继续为null。

### 方案 w-47f818520c6ad35f（状态 complete）

记录 ID：`b63eff4fbd33e6ae957fbed641755c9aec52273847511f7a9c49f015cd9c03c7`

假设：本轮完成当前最佳40W/41A/54A背景上的41位系统邻域，检验尚未提交的15种残基是否包含更高fitness的变体。最新41S/C/T面板均未超过当前最佳6.123804795，其中C=5.359942652、S=0.230585889、T=0.009736531，提示同一背景中的替换收益差异明显，不能依据母本单点值或侧链相似性筛除剩余替换。固定39V、40W、54A，枚举41位全部标准氨基酸并排除已提交的A、C、G、S、T，保留全部15个新身份。预先定义成功为至少一个有效reported_assay_fitness超过6.123804795；数值fitness预测与数值置信度保持null。以下步骤为待执行计划，最多消耗15个新查询，不安排科学工具调用。

选择理由：最新3查询面板未改善最佳值，工作流要求随后开展最佳背景上的系统单位置邻域。41位具有直接补偿证据：母本背景G41A为0.120576127，但40W/41A/54A达到6.123804795；最新同背景C值较高而S/T值较低，进一步限制侧链相似性短名单策略。该位点尚有15种未提交残基，完成其覆盖可直接检验遗漏替换是否提高fitness，同时遵守保留全部合法邻居的规则。历史18候选方案仅是计划，最新实际批次只有3条测量，因此必须排除新近已测C/S/T而保留其余15种。当前无需蛋白模型或额外计算来提出这一明确面板；所有tool_call为null，提交前运行时核验仍必须执行。

排序安排：重新评估通过核验的候选。剩余15种替换均无匹配40W/54A背景的实测值，采用固定字母顺序：D40W/G41D/V54A、D40W/G41E/V54A、D40W/G41F/V54A、D40W/G41H/V54A、D40W/G41I/V54A、D40W/G41K/V54A、D40W/G41L/V54A、D40W/G41M/V54A、D40W/G41N/V54A、D40W/G41P/V54A、D40W/G41Q/V54A、D40W/G41R/V54A、D40W/G41V/V54A、D40W/G41W/V54A、D40W/G41Y/V54A。保留全部合法身份；顺序仅用于查询安排，不代表预计fitness排序，数值预测及置信度保持null。

### 实际提交 15 条

| 母本相对突变 | 候选理由原文 | 后续揭示 fitness |
|---|---|---|
| D40W/G41D/V54A | D40W/G41D/V54A：检验酸性残基D在40W/54A背景中的适应性，补全41位邻域。 | 0.003480423 |
| D40W/G41E/V54A | D40W/G41E/V54A：检验较长酸性侧链E在40W/54A背景中的效应。 | 0.001851914 |
| D40W/G41F/V54A | D40W/G41F/V54A：检验芳香侧链F能否在40W/54A背景中获得组合收益。 | 2.659890609 |
| D40W/G41H/V54A | D40W/G41H/V54A：检验可电离的芳香侧链H在这一固定背景中的效应。 | 0.005770175 |
| D40W/G41I/V54A | D40W/G41I/V54A：检验β支链疏水残基I在40W/54A背景中的兼容性。 | 0.003117308 |
| D40W/G41K/V54A | D40W/G41K/V54A：检验带正电的长侧链K在固定背景中的效应。 | 0.001971823 |
| D40W/G41L/V54A | D40W/G41L/V54A：检验疏水侧链L，并与本面板中的I、V提供侧链形状比较。 | 1.335356528 |
| D40W/G41M/V54A | D40W/G41M/V54A：检验柔性含硫疏水侧链M在这一组合背景中的效应。 | 1.410261388 |
| D40W/G41N/V54A | D40W/G41N/V54A：检验短酰胺侧链N在40W/54A背景中的效应。 | 0.004783304 |
| D40W/G41P/V54A | D40W/G41P/V54A：检验P所引入的局部主链构象限制；不因母本单点低值排除。 | 0.010263032 |
| D40W/G41Q/V54A | D40W/G41Q/V54A：检验较长酰胺侧链Q，与N形成侧链长度比较。 | 0.0 |
| D40W/G41R/V54A | D40W/G41R/V54A：检验带正电的胍基侧链R在固定背景中的效应。 | 0.004273014 |
| D40W/G41V/V54A | D40W/G41V/V54A：检验较小β支链疏水残基V，补全疏水侧链覆盖。 | 0.017360781 |
| D40W/G41W/V54A | D40W/G41W/V54A：检验相邻40W/41W的大体积芳香组合，收益方向未知。 | 0.011084253 |
| D40W/G41Y/V54A | D40W/G41Y/V54A：检验带羟基的芳香侧链Y，与F、W共同补全芳香残基覆盖。 | 0.621399152 |

## 第 8 轮

### 方案 w-065f689c980923d5（状态 complete）

记录 ID：`fa1838a336188d0195a33f9beb25b48b36d0055443fa7cfa12bae0ce020e4774`

假设：本轮将搜索从已完成的41位邻域扩展为当前最佳序列其余三个可变位点的完整单位置邻域，一次提出49个未提交身份。最新15个41位替换全部有效但均未超过6.123804795；结合此前记录，固定39V/40W/54A时，41A仍是20种残基中的最高原始实测值。这只排除了该指定坐标背景中的剩余提升，不能说明其他位点或多位点组合无益。本轮固定最佳背景的其余残基，分别枚举39、40、54位，排除所有已提交身份。预先定义成功为至少一个有效reported_assay_fitness超过6.123804795。最多消耗49个新查询，不安排科学工具调用；以下均为待执行计划。

选择理由：最新15查询补全了41位邻域且未提高最佳值，继续在同一坐标重复搜索没有依据。此前小面板已显示母本单点收益和其他背景的替换方向不能直接迁移，因此本轮采用一个明确的策略改变：补全当前最佳其余三个坐标的全部未提交单位置邻居，取消有利单点筛选。49查询足以扩大覆盖并直接服务于最大化原始实测fitness的目标，低于单轮和总预算限制，也保留后续轮次根据真实反馈改变组合背景的机会。最新记录没有显式测定失败；没有冻结数值预测，不能认定拟合模型失配。当前证据足以提出并安排这些显式设计，蛋白模型的WT背景加性先验不能回答其组合收益问题，因此不安排工具调用。全部步骤为计划，运行时检查和测量尚未执行。

排序安排：保留全部通过核验的候选。查询顺序先54位18组，再40位15组，最后39位16组；每组按设计步骤列出的固定字母顺序排列。54位优先用于补全已显示背景方向反转但目前仅有A/C记录的坐标；40位随后扩展已有D/F/H/W/Y比较；39位补全此前仅检验I/L/M的覆盖。该顺序用于查询安排，匹配背景证据不足以支持新候选fitness高低排序，数值预测及置信度保持null。若实际去重或核验改变候选集合，按相同规则对实际合法集合重新排序。

### 方案 w-47f818520c6ad35f（状态 blocked）

记录 ID：`0cf041f08b7022b8718595065af549aea1ff33193124b5471ab31046a0a76bc8`

假设：本轮检验一个有界改变：在当前最佳实测变体39V/40W/41A/54A背景上，完整枚举54位尚未提交的18种标准氨基酸替换。最新15个41位候选均未超过6.123804795，结合此前测量，固定39V/40W/54A的41位完整邻域已覆盖，未发现更高原始记录值。54位已有A/C效应随41位背景反转的证据，但在当前最佳背景中仅覆盖A和C，因此优先系统检查54位。预先定义成功为至少一个有效reported_assay_fitness超过6.123804795；不提供数值fitness预测。以下为待执行计划，最多使用18个新查询，不安排科学工具调用。

选择理由：最新完整41位邻域没有提高最佳值，当前限制是仍有其他坐标未系统覆盖，而非已发现测定故障或数值模型失配。工作流要求未提升后使用最佳背景的完整单位置邻域，并优先选择存在补偿或方向反转证据且尚未覆盖的位置。54位符合这一条件：C相对A的方向随41G到41A改变而反转，当前最佳背景仅测过A/C。本轮固定其余位置并保留全部18个新邻居，可直接检验该坐标是否还有提升机会。已有证据足以生成面板，蛋白模型先验不能替代匹配背景测量，因此不安排科学工具调用。所有步骤均为计划，运行时核验和测量仍待执行。

排序安排：重新评估合法候选并保留全部18个。新候选均无匹配39V/40W/41A背景的实测值，采用固定字母顺序：D40W/G41A/V54D、D40W/G41A/V54E、D40W/G41A/V54F、D40W/G41A/V54G、D40W/G41A/V54H、D40W/G41A/V54I、D40W/G41A/V54K、D40W/G41A/V54L、D40W/G41A/V54M、D40W/G41A/V54N、D40W/G41A/V54P、D40W/G41A/V54Q、D40W/G41A/V54R、D40W/G41A/V54S、D40W/G41A/V54T、D40W/G41A、D40W/G41A/V54W、D40W/G41A/V54Y。该顺序用于查询安排，不代表预计fitness高低；预测值及数值置信度保持null。
