# GB1 实验审计归档

此目录保存 2026-10-05 三次 GB1 研究的操作员审计记录，便于随代码上传和回看。原始运行目录 `runs/` 保持不变。

| 目录 | 查询数 | 完成轮次 | 说明 |
|---|---:|---:|---|
| gb1-10r-20261005-143743 | 96 | 4 | 旧候选目录模式，主动停止 |
| gb1-open-10r-384-20261005-165119 | 165 | 7 | 开放设计，第 8 轮排序错误停止 |
| gb1-fullplate-10r-384-20261005-222211 | 384 | 1 | 整板模式，第二轮设计格式错误停止 |

每个目录包含：

- `summary.json`：从保存时数据库生成的状态、实际预算、最好测量及各轮批次数；进程状态为 stopped。
- `audit-db.gz`：压缩、脱敏且经过 SQLite 完整性检查的审计数据库，包含原有表及记录。goal-intake 或验证分支如存在，也分别保存数据库。
- `revealed_measurements.csv`：仅此运行已经揭示的观测，包括免费母本和 unavailable；value 为空表示没有数值，不能当作零。
- `batches/`、`programs/`、`artifacts/`：已提交批次、生成代码及相关文本结果（如原运行存在）。模板 CSV 不是测量结果，以 revealed_measurements.csv 为准。
- `console.txt`、`launch.json`：运行日志和启动参数；不保存启动 shell。文本副本统一为 LF 并去除行尾空白，原数据库内容保留。
- `task.json`（旧闭库任务因候选目录较大压缩为 `task.json.gz`）、原有 `report.json` 和分析 Markdown（如存在）：历史快照；report 可能滞后，以 summary 和数据库为准。
- `manifest.json`：归档文件大小和 SHA256 清单。

解压到本地临时目录后，可使用项目已有轨迹导出功能：

```bash
mkdir -p /tmp/gb1-audit
gzip -dc experiments/2026-10-05-gb1/gb1-fullplate-10r-384-20261005-222211/audit-db.gz > /tmp/gb1-audit/state.sqlite3
python - <<'PY'
from proteinrsi.trajectory import export_html
export_html('/tmp/gb1-audit', '/tmp/gb1-audit/trajectory.html')
PY
```

HTML 可在浏览器离线查看。该归档用于审计；含原机器路径和脱敏配置，不保证能直接恢复运行。恢复实验应使用原 `runs/` 目录，并先处理已记录的错误。

认证字段、已知本地密钥、GitHub token 及 Bearer 凭据已做脱敏；没有打包 `.env`、完整标签表、模型权重或 Python 环境。旧闭库运行可能包含当时可见的候选身份，保留用于说明目标偏差；不等于提供完整 fitness 标签。此目录是操作员档案，不应注册成研究 Agent 的知识库或输入资源。
