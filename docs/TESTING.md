# 测试

默认只运行 `core` 标记的关键场景，复用现有测试，不增加一套测试框架。
详细边界和历史兼容用例保留为按需回归；增加核心用例时优先扩展已有场景。

2026-10-12 本地验证：42 个核心用例全部通过（210 秒），独立查询账本、嵌套验证、
ESMC 与候选交接等 80 个相关回归通过；1 个需要 PyTorch 的检查在控制环境中跳过。
Ruff 通过。本次没有重跑完整回归，也未新增真实 LLM 或蛋白推理实验。

核心覆盖：

- 自然语言启动、母本初始证据和数据来源校验。
- 开放式候选设计、多轮反馈、整板提交与暂停恢复。
- 隐藏标签隔离、可选工具和生成代码执行。
- 调用重试、请求大小限制、预算和幂等记账。
- W/M 独立验收、权限继承、方法采用和回退。
- 冻结能力记忆、分支隔离和可追溯记录。
- Task/M 上下文、续跑记录及生成文件的角色边界。

```bash
pip install -e '.[dev]'
pytest -q                                     # 默认核心测试
pytest -q -m 'not real_esmc'                    # 完整回归
pytest -q -m 'not core and not real_esmc'       # 只跑扩展回归
pytest -q tests/test_general_research.py -m '' # 指定模块，覆盖默认筛选
ruff check .
```

`core` 是运行范围，不是测试成功标记。沙箱测试会实际启动隔离进程；缺少
Landlock/libseccomp 时明确跳过。CI 设置 `PROTEINRSI_REQUIRE_METRIC_SANDBOX=1`，
使核心的真实指标沙箱检查在不支持的环境中失败，不能以跳过冒充通过。

普通 push/PR 运行核心测试。GitHub Actions 的手动启动可勾选 `full_regression`
运行完整回归，或 `real_esmc` 运行原生适配及真实 ESMC 权重检查。
可选集成的完整回归需要额外安装 `.[graph,gepa,mcp]`；数据转换检查可能需要 Biopython。

```bash
pip install -e '.[esmc,dev]'
pytest -q tests/test_esmc.py -m 'not real_esmc'
PROTEINRSI_RUN_ESMC600M=1 pytest -q -s -m real_esmc
```

脚本 LLM、合成数据和模拟蛋白引擎仅验证软件机制。上述测试不代表真实 API、
GPU、湿实验或 M 能力提升已经验证；真实 ESMC 命令会单独下载权重并执行推理。
