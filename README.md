# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8320
```

默认端口为`8320`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/boom`，布设段台账（含完成/待重布判定）
- `POST /api/items/{id}/boom`，登记段号、布设船、起止时刻、长度和起止位置
- `GET /api/items/{id}/boom/summary`，完成与待重布汇总及缺口明细
- `POST /api/items/{id}/boom/{deployment_id}/recover`，撤收并登记回收长度
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录。

围油栏台账规则：同一 段号 只允许一条未撤收记录，重复布设返回409并说明占用的事件与记录；相邻两段端点间距超过5米的拼接判为待重布，不计入完成；撤收时登记回收长度，损耗超过原长两成的段报废且不能再布设。判定在`src/rules.py`，存储在`src/repository.py`，请求入口在`src/http_api.py`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
