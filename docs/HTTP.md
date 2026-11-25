# 阶段接口

`POST /api/stages/<stage>` 接收 `multipart/form-data`。字段 `sample=dirty`、`sample=clean` 或 `file=<CSV>` 三选一。阶段为 `intake`、`prepare`、`calculate`、`policy`、`balance`。预览不入库。

`POST /api/runs` 接收同样的输入，执行全批并返回运行报告。`GET /api/runs` 返回最近 50 批。`GET /api/runs/<id>` 查询一批和当前复核状态。

`PATCH /api/runs/<id>` 接收 `revision`、`status` 和 `note`。状态使用 `APPROVED` 或 `HELD`。冲突返回 409，客户端读取新版本后重试。

`GET /api/runs/<id>/evidence` 下载内含运行清单、行号隔离、业务证据和复核信息的 JSON。签发时会冻结该凭证；之后的撤回、补正只改变工作台中的当前学分，不改变按原签发编号下载的快照。

## 签发后学分变动

签发通过后，系统按业务组（申请）建立学分位置。撤回或补正只作用于指定申请，不把整批改回暂缓；其他申请继续保留原结论。

- `GET /api/workbench/credits`：查询全部申请的当前可用学分。
- `GET /api/applications/<application_id>` 或 `GET /api/credits/<application_id>`：查询单个申请；返回体同时包含 `available_credits`、`recomputed_credits`、`minimum_credits`、`deduction_valid` 和当前配对。
- `POST /api/applications/<application_id>/withdrawals`：提交外校课撤回，支持 `external_course`、可选 `local_course`、`event_id`、`effective_at`。
- `POST /api/applications/<application_id>/corrections`：提交旧配对学分补正，或用 `new_external_course`/`replacement_course` 提交换课补正。
- `POST /api/settle`：一次提交多个 `events`；同一逻辑时间的撤回优先于旧课程补正。
- `GET /api/certificates/<issue_number>/download`：按原签发编号下载签发时快照；`/api/runs/<id>/certificate` 是批次签发编号的等价地址。

复算先保留未撤回的已签发配对，再用培养方案中的其余等价关系/补正配对填补被释放的名额。复算总分达到最低学分时，`available_credits` 等于复算总分；低于最低学分时整份抵扣失效，`available_credits` 为 `0`。撤回已生效后，指向同一被撤回课程的晚到旧补正只记录为 `STALE_CORRECTION`，不会恢复该课程；后续换课补正达标后可恢复整个申请的抵扣。
