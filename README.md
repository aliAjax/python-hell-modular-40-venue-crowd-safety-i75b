# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机和团队冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `POST /api/zones/<id>/entries`：记录区域人数来源分录，支持`Idempotency-Key`
- `GET /api/zones/<id>/entries`：列出区域分录，`?include_voided=false`隐藏已作废
- `GET /api/ledger/<id>`：查看单条分录
- `POST /api/ledger/<id>/actions`：分录操作，`adjust`（更正数量）或`void`（作废）
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 区域人数汇算

区域在场人数不再单点维护，而是按来源分录汇算：每次入场（`admission`）、现场任务带回（`return`）、医疗点收治（`intake`）和撤离（`evacuation`）都落一条带来源与数量的分录，区域人数为全部有效分录之和。入场和带回为正向，收治和撤离为扣减。

- 同一来源重复提交只算一条：`(zone_id, source_type, source_ref)`在有效分录中唯一，重复提交返回原分录。
- 并发提交按服务端先后落账：分录插入、人数重算和区域更新在同一事务内串行完成，`seq`为落账顺序。
- 任一分录新增、更正（`adjust`）或作废（`void`）后立即重算区域人数；超出容量时区域自动转为`limited`并拒绝新的入场分录，撤离等扣减分录仍受理。
- 写入失败不影响已确认分录；未完成的提交用同一个`Idempotency-Key`重试，不会产生重复分录。
- 只有值班指挥员（`coordinator`/`admin`）能作废分录，作废保留痕迹并参与审计。
- 没有分录的旧区域在首次落账时按当时`current_occupancy`自动回填一条`opening`期初分录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
