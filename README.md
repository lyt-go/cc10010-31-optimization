# 数据仓库ETL任务编排平台

面向数据仓库ETL任务开发、作业编排调度、任务依赖解析、源端数据校验、异常任务重跑、血缘分板与运行全景监控的ETL工程管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| ETL作业 | `etljob` | 作业定义 | 作业编号、作业名称、源端数据表 |
| 任务编排 | `dag` | 编排图 | 图编号、图名称、拓扑结构 |
| 依赖解析 | `dependency` | 依赖关系 | 关系编号、上游作业、下游作业 |
| 源端校验 | `sourcecheck` | 校验配置 | 配置编号、源端数据表、行数下限 |
| 运行实例 | `runtime` | 运行记录 | 记录编号、作业名称、开始时间 |
| 异常重跑 | `rerun` | 重跑任务 | 任务编号、原始运行编号、重跑区间开始 |
| 基线管理 | `baseline` | 基线定义 | 基线编号、基线名称、承诺完成时间 |
| 分区检测 | `partitioncheck` | 检测规则 | 规则编号、数据表名、期望分区数 |
| 数据比对 | `comparator` | 比对任务 | 任务编号、源表名、目标表名 |
| 元数据存储 | `metastore` | 元数据表 | 表编号、表名、所属数仓层 |
| 资源配置 | `resource` | 资源池 | 池编号、池名称、最大并发数 |
| SLA监控 | `slavalidate` | SLA规则 | 规则编号、适用作业、完成截止时间 |
| SQL模板 | `sqltpl` | 模板定义 | 模板编号、模板名称、模板类型 |
| 数据备份 | `backuprestore` | 备份策略 | 策略编号、数据表名、备份周期 |
| 日志归档 | `logarchive` | 归档策略 | 策略编号、日志类型、保留天数 |
| 通知渠道 | `notifychan` | 渠道配置 | 渠道编号、渠道类型、接收地址 |
| 发布计划 | `publishplan` | 发布单 | 发布编号、发布内容、变更说明 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 输出列校验数据快照（SQL模板 ↔ 元数据存储）

发布SQL模板前不再只检查字段是否为空：`app/services/columnsnapshot.py` 把 SQL 片段解析出的
输出列与元数据存储登记的目标表字段逐列比对，判定口径只有一份，两个模块读同一份快照。

- **判定项**：字段数量、列顺序、类型范围与标准。默认阈值要求数量完全相等、顺序逐位一致、
  类型可写入目标列（`整数→整数/小数/文本`、`时间戳→时间/日期/文本` 等）；任一项超阈值即不允许
  保存为发布版本，失败原因逐列写入快照。
- **别名优先**：列映射按字段别名核对，而不是按 SELECT 中的位置。模板下载再载入后即使
  SELECT 顺序变化，只要别名不变映射就不变；声明列与别名冲突时以 SQL 别名为准并记录告警。
- **版本与并发**：同一文件在同一目标表口径下重复载入只回用一个校验版本（以
  `模板指纹 + 校验指纹` 判重），并发载入由仓储级 `RLock` 串行化，只允许一个版本生效。
  失败文件留下审计记录，但不会被当作可用快照、也不会顶替上一可用版本。
- **跨模块联动**：元数据存储读取快照并标记字段一致性；目标表字段变化会作废关联模板的
  已生效结论，已发布模板退回「模板评审中」，字段不一致时不允许「确认元数据」。
- **历史兼容**：老快照缺字段时由 `normalize_row` 补默认值；老元数据只有「字段数量」时
  按占位字段只校验数量；类型列同时接受紧凑编码（I/D/S/T/U）、中文类型名与完整 DDL 类型。

主要接口：

| 接口 | 说明 |
| --- | --- |
| `POST /api/sqltpl/validate-file` | 只读预检模板文件，不落快照 |
| `POST /api/sqltpl/import` | 载入模板文件，同文件同口径只生成一个版本 |
| `GET /api/sqltpl/{id}/download` | 下载模板文件（附带当前校验快照） |
| `GET /api/sqltpl/snapshots` | 查看全部校验版本 |
| `GET /api/sqltpl/{id}/snapshot` | 读取模板当前快照与上一可用快照 |
| `GET /api/metastore/{id}/output-column-snapshots` | 元数据侧读取同一份快照并标记字段一致性 |
| `PUT /api/metastore/{id}/columns` | 更新目标表字段，联动作废/退回关联模板 |

回归测试：`cd backend && .venv/bin/python -m unittest discover -s tests`。

领域：数据工程
子方向：数仓与数据湖
