# 跨校成绩入仓资格核查 API

教研员按批号登记/撤回卷型等值报告。每份报告以唯一单号连接两个卷型（无向、可双向引用），
一个卷型必须存在**两条不共用报告**的等值链通往基准卷，才取得入仓资格。

## 资格规则

| 状态 | 含义 | 返回内容 |
| --- | --- | --- |
| `baseline` | 基准卷本身，免核查 | — |
| `qualified` | 存在两条边不相交的链到基准卷（链可经过相同卷型） | `chains`：两条链各自的报告单号 |
| `single_chain` | 尚能接通基准卷，但只有一条边不相交链 | `mandatory_report_nos`：所有必经单号（桥边） |
| `no_evidence` | 与基准卷完全断开，缺依据 | 空 |

- 同一对卷型的不同报告单号分别计入（平行边各自独立）。
- 两条链允许经过相同卷型，但不能共用同一份报告。
- 算法：无向图边容量为 1，Edmonds-Karp 最大流（最多增广两次）判定边不相交路径数；
  单链时用逐边删除 BFS 求全部桥边。

## 批次与版本语义

- 每个成功生效的批次产生一个自增版本号，按实际生效顺序核对；
- 报告变更、资格重算、回执写入在**同一个 SQLite 事务**中一起生效（`BEGIN IMMEDIATE` + 应用层写锁）；
  失败或重送都不会新增版本；
- 批次回执列出相对上一版**取得资格**（`qualified_papers`）和**失去资格**（`disqualified_papers`）的卷型，供数仓更新入仓名单；
- 已生效批次**原样重送**返回原回执（`replayed=true`，版本号不变）；
- **同号异文**返回 `409 batch_conflict`，不产生新版本；
- 数据落盘 SQLite（WAL），进程失败/重启后仍可查最近生效版及任一历史版本。

### 基准卷字段 `baselines`

| 请求 | 生效的基准卷 | 说明 |
| --- | --- | --- |
| `"baselines": ["BASE"]` | `["BASE"]` | 用给定全集替换 |
| `"baselines": []` | `[]` | **撤下全部基准卷**，按无基准卷重算资格 |
| 省略 `baselines` | 上一版实际生效值 | 上一版若已清空，则继续为空 |

- 显式 `[]` 与省略是**不同的载荷**：同一批号在这两种写法之间改动返回 `409`；
  各自原样重送则取回原回执。重启后依据落库载荷同样核对。
- 清空批次回执的 `disqualified_papers` 列出失去资格的卷型，`current_qualified` 为空。
- 清空前的历史版本仍按各自版本的基准卷查询（`?version=N`）。
- 修复前落库的旧载荷没有 `payload_schema` 标记，无法区分省略与 `[]`；
  只要其他内容一致，两种重送都按原回执处理。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# API:   http://localhost:8000
# Swagger: http://localhost:8000/docs
```

SQLite 数据保存在命名卷 `qualification-data`（容器内 `/data/qualification.db`）。

### 本地运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
QUAL_DB_PATH=./data/qualification.db uvicorn app.main:app --reload
```

## 接口（均可在 Swagger UI 提交）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 提交批次（登记/撤回，可同时指定基准卷全集） |
| GET | `/batches` | 列出全部生效批次/版本 |
| GET | `/batches/{batch_no}/receipt` | 按批号取回执 |
| GET | `/qualifications/{paper_id}?version=` | 查单个卷型资格（缺省最近生效版） |
| GET | `/qualifications?version=` | 查同一生效版下全部卷型资格 |
| GET | `/reports` / `/reports/{report_no}` | 报告查询（含撤回状态） |

### 批次请求示例

```json
{
  "batch_no": "B2026-001",
  "baselines": ["BASE-2026"],
  "operations": [
    {"action": "register", "report_no": "R001", "paper_a": "P-A", "paper_b": "P-B"},
    {"action": "withdraw", "report_no": "R099"}
  ]
}
```

`baselines` 为该批生效的基准卷全集；省略则沿用上一版，显式 `[]` 表示撤下全部基准卷。

### 在 Swagger 演示清空与重送

1. `docker compose up --build`，打开 <http://localhost:8000/docs>；
2. 展开 `POST /batches`，Examples 下拉框选择「清空全部基准卷」，Execute；
   回执 `baselines` 为 `[]`、`disqualified_papers` 列出失去资格的卷型；
3. 再次以同一请求 Execute（选择「原样重送取原回执」示例），回执 `replayed=true`、版本号不变；
4. 把请求体改为省略 `baselines`（或选择「省略 baselines 沿用上一版」但批号改为同一批号），
   返回 `409 batch_conflict`；
5. 用 `GET /qualifications?version=N` 查清空前的版本，仍按当时的基准卷判定。

## 自测

```bash
pip install pytest httpx
python -m pytest tests/ -q          # 27 项单元/接口测试（含清空/省略/重放/重启/旧库兼容）
python scripts/demo.py              # 对运行中的服务做端到端演示（含 baselines=[] 清空与重送）
python scripts/concurrency_check.py # 并发批次顺序与幂等核对
```

重点场景：
- `tests/test_graph.py::test_shared_edge_fails_even_with_extra_branch`：共用报告不通过；
- `tests/test_api.py::test_withdraw_downgrades_to_single_chain`：撤回后降为单链、再撤回变缺依据。
