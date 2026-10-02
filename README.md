# 识别靶点赛道拥挤与差异化窗口基础平台

本项目是一套可离线运行的 Python 服务端平台，供创新药企业的研发管理、转化医学、商务拓展和基金运营团队管理候选药实验记录、研发证据、协作中心资源、尽调交接通道、交易风险告警与跟进任务。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

`competitive_intel` 子包面向靶点评审会，提供可解释的赛道比较能力：把靶点、作用机制、适应症、开发阶段、关键实验版本与公开时间线组织成**可追溯的竞争快照**，指出赛道拥挤程度、尚未被覆盖的临床差异、证据缺口，以及形成结论所引用的具体记录/实验版本。

## 目录

- src/portfolio_ops/：研发中心、尽调通道、研究资源、交接计划和商业情景；
- src/discovery_lab/：研究协议、实验记录、异常排除、分析任务租约和候选结论；
- src/licensing_ops/：管线信息、交易风险告警、跟进工单和资源分配；
- src/competitive_intel/：赛道资产记录版本化、同源合并/拆回、证据可信度标注、竞争快照与投决锁定、按角色最小披露；
- fixtures/：离线验收使用的研究协议、结构化实验记录与 TL1A 赛道演示数据；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 赛道竞争快照的核心规则

- **记录只追加版本**：资产记录（靶点/机制/适应症/阶段/关键实验版本/公开时间线）每次修订追加一个带 SHA-256 的新版本，旧版本永不修改。
- **合并同源、拆回误关联**：归并关系与 `previous_membership_id` 谱系全程留痕；拆回只把旧关系置为 revoked 并恢复独立条目，不删除历史。
- **证据可信度独立标注**：记录自带来源证据层级（rumor/press_release/conference/peer_reviewed/regulatory），分析员另给 high/medium/low 可信度判定并附理由与证据出处，标注历史全部保留。
- **快照不可变、投决即锁定**：快照逐条钉住当时的记录版本、归属与可信度标注（含冻结副本与输入 SHA-256）。发布后冻结；投决引用后置为 locked，此后任何记录修订、合并或拆回都只能产生快照新版本，**已经用于投决的历史判断可逐字节复现**。
- **可解释分析**：拥挤度按“靶点 × 作用机制”归组，以显式阈值规则（open/emerging/active/crowded/saturated）判定并返回规则求值轨迹；临床差异在适应症宇宙内列出无人占位格、单一资产占据的差异化位置与外部有/内部无的窗口；证据缺口逐记录引用 `记录@版本` 与具体实验版本。
- **按角色最小披露**：analyst 只见比较所需结构字段（敏感项目身份与实验细节遮蔽）；reviewer 可见身份但不见敏感实验细节；committee 与 auditor 对已发布快照拥有完整披露。

角色：`analyst`（登记/修订、可信度标注、合并拆回、起草快照）、`reviewer`（发布快照）、`committee`（投决）、`auditor`（审计与完整披露）。


## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.acceptance --workspace .
PYTHONPATH=src python3 -m discovery_lab.acceptance --workspace .
PYTHONPATH=src python3 -m licensing_ops.acceptance
PYTHONPATH=src python3 -m competitive_intel.acceptance --workspace .
~~~

四条命令会在临时 SQLite 数据库中完成研发中心与交接通道登记、研究资源分配、候选药证据分析、交易风险处置，以及赛道记录登记、同源合并与拆回、快照发布投决锁定、投决后新版本不改变历史判断和按角色最小披露的自检，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.api --database portfolio.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m discovery_lab.api --database discovery.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m licensing_ops.api --database licensing.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m competitive_intel.api --database competitive_intel.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。赛道服务除 `/health` 与 `/users` 外均需 `X-Actor-Id` 头标识操作者，主要路由：

- `POST /records`、`POST /records/{id}/revisions`、`GET /records/{id}/versions/{n}`：资产记录登记、修订与历史版本；
- `POST /credibility_annotations`：证据可信度判定；
- `POST /assets`、`POST /assets/{id}/merge`、`POST /assets/unmerge`、`GET /assets/{id}`：同源归并、并入、拆回与谱系；
- `POST /snapshots`、`POST /snapshots/{id}/publish`、`GET /snapshots/{id}/versions/{n}`：快照新版本、发布与历史复现；
- `POST /judgments`：投决判断（写入即锁定引用快照版本）；
- `GET /audit_events`：追加式审计事件。
