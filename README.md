# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突。

## 区域人数按来源汇算

区域在场人数不再由各处分别上报，而是按来源汇算为一条条分录：

- **入场**（`admission`）：入场口放行，`zone.admit` 自动落一条分录。
- **现场任务带回**（`bringback`）：`task.bring_back` 落一条分录。
- **医疗点收治**（`medical`）：`medical_point.admit_patient` 落一条分录。
- **撤离**（`evacuation`）：`zone.evacuate` 按撤离人数（或全部在场上人数）落一条负向分录。

规则：

- 每条分录带来源（`source_type`）和数量（`quantity`，流入为正、撤离为负），并带 `source_ref` 来源引用。
- 同一来源重复提交只算一条：`source_type + source_ref` 唯一；重试沿用同一个 `Idempotency-Key`。
- 两个操作员同时提交时，SQLite `BEGIN IMMEDIATE` 事务把检查与落账串行化，按服务端先后落账。
- 任何一条分录更新或作废后，区域人数立即重算（`SUM(quantity)`）；超出容量即把区域转为 `limited` 并拒绝新的入场（409）。
- 写入失败后已确认分录保留；未完成的重试沿用同一幂等键，不重复落账。
- 只有值班指挥员（`coordinator`）能作废分录。
- 旧数据没有分录的，在首次落账时按现有人数回填一条 `opening` 期初分录。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常、数据对象和分录来源常量。
- `src/rules.py`：容量计算、事件优先级、状态机、团队冲突约束和分录校验。
- `src/repository.py`：SQLite持久化、乐观锁、幂等、分录落账/作废/更新与汇算、审计查询。
- `src/service.py`：用例编排、权限校验、版本控制和入场/撤离/带回/收治的分录副作用。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务，`headcount_entry`为区域人数来源分录。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

分录接口：

- `GET /api/headcount_entries`，可用`?zone_id=`、`?status=`过滤
- `GET /api/headcount_entries/<id>`
- `POST /api/headcount_entries`（`zone_id`、`source_type`、`source_ref`、`quantity`、`occurred_at`），可用`Idempotency-Key`
- `POST /api/headcount_entries/<id>/void`（仅值班指挥员，body 带`reason`）
- `POST /api/headcount_entries/<id>/update`（body 带`quantity`、`occurred_at`）
- `GET /api/zones/<id>/headcount`：返回`headcount`、`by_source`、`capacity`、`over_capacity`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建；入场重试沿用同一幂等键。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
