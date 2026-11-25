# 职校学分互认台规则登记

- 规模：中型

## 功能模块

| 模块 | 动作 | 接口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 样例采信 | 预览并定位坏行 | `POST /api/stages/intake` | `app/intake/service.py` | `tests/test_intake.py` |
| M2 课程适配 | 检查领域约束 | `POST /api/stages/prepare` | `app/modules/prepare.py` | `tests/test_prepare.py` |
| M3 互认分配 | 查看业务计算结果 | `POST /api/stages/calculate` | `app/modules/calculate.py` | `tests/test_calculate.py` |
| M4 培养要求核验 | 查看签收判定 | `POST /api/stages/policy` | `app/modules/policy.py` | `tests/test_policy.py` |
| M5 运行对账 | 对照输入、采信和隔离行 | `POST /api/stages/balance` | `app/modules/reconcile.py` | `tests/test_reconcile.py` |
| M6 批次复核 | 执行批次并签发或暂缓 | `POST /api/runs、PATCH /api/runs/<id>` | `app/review/service.py` | `tests/test_review.py` |
| M7 证据交付 | 下载复核清单 | `GET /api/runs/<id>/evidence` | `app/evidence/render.py` | `tests/test_evidence.py` |
| M8 签发后变动 | 申请层撤回/补正复算、当前学分、原编号凭证快照 | `GET /api/workbench/credits`、`POST /api/applications/<id>/withdrawals`、`POST /api/applications/<id>/corrections` | `app/credits.py`、`app/lifecycle.py` | `tests/test_credit_lifecycle.py` |

## 输入陷阱

| 编号 | 规则 | 处理 | 测试 |
|---|---|---|---|
| TRAP-02 | 去除 BOM | `app/intake/reader.py` | `tests/test_intake.py::test_bom_preserves_identifier` |
| TRAP-03 | 按逻辑记录读取引号内换行 | `app/intake/reader.py` | `tests/test_intake.py::test_quoted_newline_is_one_logical_record` |
| TRAP-04 | 重复表头计入忽略行 | `app/intake/service.py` | `tests/test_intake.py::test_repeated_header_is_counted` |
| TRAP-05 | 异常列数隔离 | `app/intake/schema.py` | `tests/test_intake.py::test_extra_column_is_isolated` |
| TRAP-06 | 归一化全角数字和千分位 | `app/intake/numeric.py` | `tests/test_intake.py::test_full_width_and_grouping_are_canonical` |
| TRAP-07 | 空值行隔离 | `app/intake/numeric.py` | `tests/test_intake.py::test_sentinel_keeps_row_out_of_total` |
| TRAP-08 | 记录键保留前导零 | `app/intake/schema.py` | `tests/test_intake.py::test_bom_preserves_identifier` |
| TRAP-14 | 冲突版本都进入隔离 | `app/intake/resolve.py` | `tests/test_intake.py::test_duplicate_conflict_removes_both_copies` |
| TRAP-16 | 引号未闭合时拒收文件 | `app/intake/reader.py` | `tests/test_intake.py::test_unclosed_quote_invalidates_source` |
| TRAP-18 | 区分空文件、全空白和仅表头 | `app/intake/reader.py` | `tests/test_intake.py::test_empty_states_have_distinct_causes` |
| TRAP-26 | 拒绝旧版本复核 | `app/review/store.py` | `tests/test_review.py::test_review_optimistic_lock` |

## 领域反例

权重为课程互认学分。最多 12 门校内课程。外校课与校内课均只有一个名额，先最大化学分，再比较审批等级之和，最后按课程键稳定择一。最低学分由培养方案给出。

签发后撤回外校课程时，只冻结受影响申请并复算：未撤回的已签发配对继续保留，被释放名额可由已登记等价关系或换课补正补入。复算总分低于最低学分则整份抵扣的当前可用学分记为 0；达到最低学分则其余配对继续有效，换课补正达标后可恢复。同一逻辑时间同时收到撤回和旧课程补正时，撤回优先；撤回生效后到达的旧补正记录标记为 `STALE_CORRECTION`，不能恢复已撤回课程。凭证按原签发编号读取签发时冻结的字节，不随后续复算改变。

`sample_data/incoming.csv` 的 0003 组触发预检异常，0004 组触发业务判定。`tests/test_calculate.py` 还核对逐组结论和证据字段。
