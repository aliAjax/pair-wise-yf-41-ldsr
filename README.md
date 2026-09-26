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

- `station`：观测台站；`event`：地震事件；`revision_order`：针对已发布/已修订事件的修订单。

## 修订单流程

已发布事件不允许直接修改（`revise`动作已关闭），必须通过修订单：

1. 分析员对`published`或`revised`事件创建`revision_order`，必填`event_id`、`basis`（修改依据）、`magnitude`、`location`、`depth`；建单时自动锚定所基于的事件版本（`event_version`）。
2. 审核员`reject`时必填`opinion`（审校意见）；分析员在同一单上`resubmit`（重新给出依据和拟改值），`resubmit_count`自动累加，并重新锚定事件版本。
3. 审核员`approve`后，拟改值与单据状态在同一个事务内写入：事件变为`revised`，同时生成新的目录快照并置为当前版本；审核中的单子不影响当前目录。
4. 若事件版本在建单后已变化，`approve`以版本过期（409）拒绝，需退回后重提锚定新版本；单据本身也受`expected_version`乐观锁保护。
5. 建单、退回、重提、批准全部写入审计，事件侧同步记录一条`revise`审计并关联单号。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/catalog`：当前外发目录（每个事件的最新快照）。
- `GET /api/events/<id>/snapshots`：该事件全部发布快照，旧版本可查。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
