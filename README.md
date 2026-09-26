# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本。
- `revision_order`：针对已发布（`published`）或已修订（`revised`）事件的补修订单。

## 补修订单流程

已发布/已修订事件不能再用动作直接改动（`revise` 动作已移除），所有震级、位置、深度修改必须走补修订单：

1. 分析员 `POST /api/revision_orders` 建单，必填 `event_id`、`basis`（修改依据）、`proposed_magnitude`、`proposed_location`、`proposed_depth_km`。单据状态为 `pending`，并冻结所依据的事件版本 `event_version`。
2. 审核员可 `return` 退回（必填 `comment`），意见累加保留在订单 `comments` 中；分析员在**原单**上 `resubmit` 修改并重新提交，`resubmit_count` 递增，审核意见不清空，同时重新对齐到事件最新版本。
3. 审核员 `approve` 批准（必填 `communication_id`）：在单个数据库事务内校验订单和事件版本均未过期，随后更新事件为 `revised`、生成新的目录快照。旧发布版可通过 `GET /api/entities/<id>/versions` 查询。
4. `pending`/`returned` 的订单不改动当前目录。版本过期（事件已被别的订单修订）或越权操作返回 409/403，事务回滚。

建单、退回、重提、批准均写入审计（批准同时记录订单、事件变更和快照三条审计）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（含 `revision_orders`）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `GET /api/entities/<id>/versions`：读取事件历次发布/修订快照。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录，可用`?entity_id=`过滤。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
