# Dataflow增量的复用与许可

本增量在 `0c6254674455c4c013cd9d56e631ac7bfa8d36e0` 的已审读接口上实现，新增 `dataflow/`、提示词、配置、测试和文档为 ProteinRSI 原创 MIT 代码/文字。

| 来源 | 本次实际关系 |
|---|---|
| ProteinRSI现有 `ToolGateway` / `ToolSpec` | 直接复用原生调用、Schema、权限、预算及缓存，不重写蛋白模型推理；工具Schema是唯一来源 |
| ProteinRSI现有排序与角色 | 复用 `request_ranking()` 的稳定ID与实际 A/B/C 调用；增加的设计修复同样走既有LLM计费接口 |
| ProteinRSI现有资源/实验控制 | 复用 `research_step_outputs`、ArtifactStore登记、受限RPC和Campaign；不复制、不修改私有数据下载脚本，不扩大查询能力 |
| Pydantic / jsonschema | 已有库/API复用，生成类型Schema并验证；不引入另一套模型框架 |
| Biomni | 架构启发：函数/描述分离、计划—执行—观察。此前的Apache检索改编不变；本次没有再移植Biomni源码或原始提示词 |
| protein-design-mcp v2 | 架构启发：原子工具、Manifest派生接口、显式链/MSA；没有复制其源码、镜像、权重或全部工具目录 |
| ProteinMCP | 架构启发：方法Skill与独立计算环境分层；没有复制服务器管理器 |

此前的 `NOTICE`、`THIRD_PARTY.md`、Apache-2.0许可文本及用户SVG原样保留。MIT仅覆盖本项目原创文件，不能重新许可模型权重、数据库或上游代码；工具Schema校验通过不等于获得模型许可或科学验证。

参考入口（设计来源，不是新增运行时依赖）：
- https://github.com/snap-stanford/Biomni/blob/400c1f366b96a35ca253e13c9b06c5076af41d65/CONTRIBUTION.md
- https://github.com/jasonkim8652/protein-design-mcp/blob/main/src/protein_design_mcp/manifest/registry.py
- https://github.com/charlesxu90/ProteinMCP/blob/main/CLAUDE.md

第二、三个链接是审读时的上游路径，不代表锁定或导入其最新实现。若以后实际移植源码，应另固定commit、保留许可并说明改动，不能将本文件当作源码移植授权。
