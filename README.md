# 公交电动车配车测算 API

公交运营分析员审核电动车配车量时，用于核算**一批固定班次最少需要几辆车**的
FastAPI 服务。精确求解并给出任一最优方案（逐车班次顺序与总里程），供核对配车计划。

## 测算语义

输入：

| 字段 | 含义 |
| --- | --- |
| `depot` | 车场地点名，须出现在 `locations` 中 |
| `locations` | 全部地点（含车场），1～8 个，名称唯一 |
| `deadhead_seconds` | 地点间直达空驶秒数矩阵（行=起点、列=终点），非负整数 |
| `deadhead_meters` | 地点间直达空驶米数矩阵，非负整数 |
| `range_meters` | 车辆统一续航（米），正整数 |
| `trips` | 1～12 个固定班次：`id`（唯一）、`origin` / `destination`（须已登记）、`start` / `end`（秒，非负整数，`end` 严格大于 `start`）、`distance`（米，非负整数） |

规则：

1. 车辆 **0 秒起**可自同一车场满电出车，允许等待、**不补能**，末班后**必须回场**。
2. 每个班次恰由一辆车完整执行；衔接不得迟到——上一班结束时刻 +
   终点到下一班起点的空驶秒数 ≤ 下一班开始时刻，**等号可接上**。
3. 空驶时间与里程**直接采用矩阵值**（含对角线，不假设对称性与三角不等式）。
4. 每车总里程（出场 + 班次行驶 + 班次间转场 + 回场）**不得超过续航**（等号允许）。

求解：

- 同一车辆执行的班次开始时刻必然严格递增，故任一班次集合若能由一车完成，
  执行顺序唯一（按开始时刻升序），问题等价于「把班次集合划分成最少个一车可行子集」。
- 用**子集动态规划**精确求解：`f[S] = min{ f[S\T] + 1 | T ⊆ S 且 T 一车可行 }`，
  复杂度 O(3ⁿ)，n ≤ 12 时约 53 万次转移，在线毫秒级返回。
- 输出任一最优方案：逐车班次顺序、最晚出场 / 回场时刻、里程构成
  （出场 / 转场 / 班次 / 回场 / 合计）。结果确定（同输入必同输出）。

无解与非法输入：

- 若某班次任何车都无法单独完成（车场来不及赶到，或单班次最小里程已超续航），
  整体**无解**：返回 `status=infeasible`、原因与全部阻塞班次，**不返回任何部分安排**。
- 输入非法（班次数越界、ID 重复、结束不晚于开始、负数 / 小数时间、矩阵维度不符、
  起终点未登记等）返回 **422**，`detail` 中带出错字段位置（`loc`）与中文说明。
- 有解与无解均返回 HTTP 200，用响应体 `status` 字段（`ok` / `infeasible`，
  OpenAPI discriminator）区分。

## 响应示例

有解：

```json
{
  "status": "ok",
  "vehicle_count": 2,
  "vehicles": [
    {
      "vehicle": 1,
      "trips": ["T1", "T2"],
      "depot_departure_second": 24600,
      "depot_return_second": 30000,
      "mileage": {"pull_out": 8000, "between_trips": 0, "trips": 40000, "pull_in": 8000, "total": 56000}
    },
    {
      "vehicle": 2,
      "trips": ["T3"],
      "depot_departure_second": 29400,
      "depot_return_second": 32700,
      "mileage": {"pull_out": 8000, "between_trips": 0, "trips": 20000, "pull_in": 11000, "total": 39000}
    }
  ]
}
```

无解：

```json
{
  "status": "infeasible",
  "reason": "班次 B1 无法由任何一辆车单独完成，配车无解",
  "blocking_trips": [
    {"trip_id": "B1", "reasons": ["单班次最小总里程 64000 米（出场 8000 + 班次 45000 + 回场 11000）超过续航 60000 米"]}
  ]
}
```

## 启动（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：<http://localhost:8000>
- Swagger UI（内置「累计里程受限」「时段重叠」「无解」三个可直接运行的
  请求示例）：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- OpenAPI JSON：<http://localhost:8000/openapi.json>
- 健康检查：`GET /health`

本地裸机调试：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 请求示例（累计里程受限）

三个班次时间上完全可由一辆车衔接，但一车跑完全程需
`8000+20000×3+11000=79000` 米，超过续航 60000 米，只能拆成 2 车：

```bash
curl -X POST http://localhost:8000/api/v1/vehicle-schedules/compute \
  -H 'Content-Type: application/json' \
  -d '{
    "depot": "车场",
    "locations": ["车场", "甲站", "乙站"],
    "deadhead_seconds": [[0, 600, 900], [600, 0, 240], [900, 240, 0]],
    "deadhead_meters": [[0, 8000, 11000], [8000, 0, 4000], [11000, 4000, 0]],
    "range_meters": 60000,
    "trips": [
      {"id": "T1", "origin": "甲站", "destination": "乙站", "start": 25200, "end": 27000, "distance": 20000},
      {"id": "T2", "origin": "乙站", "destination": "甲站", "start": 27600, "end": 29400, "distance": 20000},
      {"id": "T3", "origin": "甲站", "destination": "乙站", "start": 30000, "end": 31800, "distance": 20000}
    ]
  }'
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

覆盖：单车 / 链接班次、等号衔接、差 1 秒不可接、续航等号边界与差 1 米拆分、
对角线与非对称矩阵语义、两类无解（赶不上 / 超续航）、输出确定性、12 班次规模、
随机小实例与暴力枚举划分的对拍、OpenAPI 示例与 discriminator、20 组非法输入的
422 定位。

## 目录结构

```
app/
  __init__.py
  schemas.py     # Pydantic 请求/响应模型与输入校验（422 定位）
  solver.py      # 单班次可行性、子集可行性、子集 DP 精确求解
  main.py        # FastAPI 应用、OpenAPI 示例
tests/           # pytest 单元与 API 测试
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

## 复杂度

设班次数为 n（≤ 12）：子集可行性预处理 O(2ⁿ·n)，子集 DP 转移 O(3ⁿ)，
空间 O(2ⁿ)；n = 12 时约 53 万次转移，毫秒级完成。
