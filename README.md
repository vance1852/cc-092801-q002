# 识别靶点赛道拥挤与差异化窗口基础平台

本项目是一套可离线运行的 Python 服务端平台，供创新药企业的研发管理、转化医学、商务拓展和基金运营团队管理候选药实验记录、研发证据、协作中心资源、尽调交接通道、交易风险告警与跟进任务。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/portfolio_ops/：研发中心、尽调通道、研究资源、交接计划和商业情景；
- src/discovery_lab/：研究协议、实验记录、异常排除、分析任务租约和候选结论；
- src/licensing_ops/：管线信息、交易风险告警、跟进工单和资源分配；
- src/track_intel/：靶点赛道竞争快照、同源记录合并与拆回、证据可信度标注、投决锁定与按角色最小披露；
- fixtures/：离线验收使用的研究协议与结构化实验记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

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
PYTHONPATH=src python3 -m track_intel.acceptance --workspace .
~~~

四条命令会在临时 SQLite 数据库中完成研发中心与交接通道登记、研究资源分配、候选药证据分析、交易风险处置以及靶点赛道竞争快照比较，不访问外部网络。

## 赛道比较（track_intel）

- 竞争快照把靶点、作用机制、适应症、开发阶段、关键实验版本和公开时间线固化为内容寻址版本：输入清单、算法版本与结果一并落库，可随时重放校验（`GET /snapshots/{id}/verify`）。
- 分析人员可以合并同源记录、拆回误关联（事件溯源，历史不丢失），并对记录或时间线事件标注证据可信度（追加式，最新标注生效）。
- 快照内容在数据库层不可改写、不可删除；投决会引用的版本转为 decision_locked，此后任何记录修订、合并或标注只会产生新的快照修订，历史判断保持原样。
- 比较结果给出拥挤度（含各阶段权重因子）、跟随风险、尚未被覆盖的临床差异、证据缺口，以及形成结论的具体版本（快照修订、算法版本、输入摘要、各记录修订号）。
- 角色最小披露：viewer 只能看到脱敏视图（敏感项目身份化名、关键实验版本与时间线细节隐藏），reviewer 与 auditor 可见完整身份，analyst 负责登记与计算。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.api --database portfolio.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m discovery_lab.api --database discovery.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m licensing_ops.api --database licensing.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m track_intel.api --database track_intel.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。
