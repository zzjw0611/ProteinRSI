# GB1 首次试运行

在项目根目录执行：

```bash
/home/zjw/proteinrsi-tools/bin/proteinrsi start \
  "使用 SSMuLA 的 GB1 实测组合突变景观，开展历史实测数据回放。开放式组合突变优化，遵守数据集母本及可变位点约束，最大化原始报告的实测 fitness。共4轮，每轮做24个，总共96个新查询。母本实测fitness作为免费初始证据。Task Agent根据已揭示反馈自主设计候选。已提供蛋白设计工具的用途和接口，可按需调用辅助分析和验证，均非必选。LLM与工具调用不设总次数上限。Meta Agent自主分析并决定是否修改W或M，允许不修改。方法验证计入共享查询预算，保存完整轨迹与版本。" \
  --data-root "$HOME/data/ssmula" \
  --local-tools configs/protein_tools.local.json \
  --protocol-mode typed \
  --full-plate \
  --llm-calls unlimited \
  --tool-calls unlimited \
  --out runs/gb1-pilot-r4-q96
```

`--full-plate` 落实每轮24个；方法验证名额包含在实验预算内。
本地完整工具配置使 ESMC、ProteinMPNN、RFdiffusion、Protenix、PyRosetta 可按任务及接口适配情况选用。
`unlimited` 取消 LLM／工具调用总次数上限，实际调用和费用证据继续记录。
JSON 预算中对应值为 `null`；省略 CLI 参数仍使用原来的有限默认值。
每次执行的超时、重试、上下文大小等运行保护独立于总次数预算。
