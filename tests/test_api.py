"""API 层测试（通过 FastAPI TestClient）。"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.main import _MILEAGE_LIMITED_EXAMPLE, app

client = TestClient(app)

URL = "/api/v1/vehicle-schedules/compute"

BASE_BODY: dict = {
    "depot": "车场",
    "locations": ["车场", "甲站", "乙站"],
    "deadhead_seconds": [
        [0, 600, 900],
        [600, 0, 240],
        [900, 240, 0],
    ],
    "deadhead_meters": [
        [0, 8000, 11000],
        [8000, 0, 4000],
        [11000, 4000, 0],
    ],
    "range_meters": 60000,
    "trips": [
        {"id": "T1", "origin": "甲站", "destination": "乙站",
         "start": 25200, "end": 27000, "distance": 20000},
    ],
}


def post(body: dict) -> "object":
    return client.post(URL, json=body)


def mutated(mutate) -> dict:
    body = copy.deepcopy(BASE_BODY)
    mutate(body)
    return body


# --------------------------------------------------------------------------
# 运维端点与文档
# --------------------------------------------------------------------------
def test_health_and_root() -> None:
    assert client.get("/health").json() == {"status": "ok"}
    root = client.get("/").json()
    assert root["docs"] == "/docs"
    assert client.get("/docs").status_code == 200


def test_openapi_contains_examples_and_discriminated_response() -> None:
    schema = client.get("/openapi.json").json()
    endpoint = schema["paths"][URL]["post"]
    examples = endpoint["requestBody"]["content"]["application/json"]["examples"]
    assert set(examples) == {"mileage_limited", "time_overlap", "infeasible"}
    resp_ref = endpoint["responses"]["200"]["content"]["application/json"]["schema"]
    assert "oneOf" in resp_ref
    assert resp_ref.get("discriminator", {}).get("propertyName") == "status"


# --------------------------------------------------------------------------
# 正常求解
# --------------------------------------------------------------------------
def test_mileage_limited_docs_example_returns_two_vehicles() -> None:
    resp = post(_MILEAGE_LIMITED_EXAMPLE["value"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["vehicle_count"] == 2
    vehicles = sorted(data["vehicles"], key=lambda v: v["vehicle"])
    assert [v["trips"] for v in vehicles] == [["T1", "T2"], ["T3"]]
    v1, v2 = vehicles
    # 车 1：出场 8000 + 班次 40000 + 转场 0 + 回场 8000 = 56000
    assert v1["mileage"] == {
        "pull_out": 8000, "between_trips": 0, "trips": 40000,
        "pull_in": 8000, "total": 56000,
    }
    assert v1["depot_departure_second"] == 25200 - 600
    assert v1["depot_return_second"] == 29400 + 600
    # 车 2：出场 8000 + 班次 20000 + 回场 11000 = 39000
    assert v2["mileage"]["total"] == 39000
    # 每个班次恰由一车执行
    all_trips = [t for v in vehicles for t in v["trips"]]
    assert sorted(all_trips) == ["T1", "T2", "T3"]
    for v in vehicles:
        m = v["mileage"]
        assert m["pull_out"] + m["between_trips"] + m["trips"] + m["pull_in"] == m["total"]
        assert m["total"] <= 60000


def test_single_trip_ok() -> None:
    data = post(BASE_BODY).json()
    assert data["status"] == "ok"
    assert data["vehicle_count"] == 1
    assert data["vehicles"][0]["trips"] == ["T1"]
    assert data["vehicles"][0]["mileage"]["total"] == 39000


# --------------------------------------------------------------------------
# 无解：说明原因，不返回部分安排
# --------------------------------------------------------------------------
def test_infeasible_returns_no_partial_plan() -> None:
    body = mutated(lambda b: b["trips"].append(
        {"id": "B1", "origin": "甲站", "destination": "乙站",
         "start": 25200, "end": 28800, "distance": 45000}
    ))
    resp = post(body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "infeasible"
    assert "vehicles" not in data and "vehicle_count" not in data  # 不给部分安排
    assert data["blocking_trips"][0]["trip_id"] == "B1"
    assert any("续航" in r for r in data["blocking_trips"][0]["reasons"])
    assert data["reason"]


def test_infeasible_when_cannot_reach_first_trip_in_time() -> None:
    body = mutated(lambda b: b["trips"].__setitem__(0, {
        "id": "T1", "origin": "乙站", "destination": "甲站",
        "start": 100, "end": 500, "distance": 1000,
    }))
    data = post(body).json()
    assert data["status"] == "infeasible"
    assert any("晚于班次开始" in r for r in data["blocking_trips"][0]["reasons"])


# --------------------------------------------------------------------------
# 非法输入：422 并定位
# --------------------------------------------------------------------------
BAD_BODIES = [
    pytest.param(mutated(lambda b: b.update(trips=[])), id="0 个班次"),
    pytest.param(
        mutated(lambda b: b.update(trips=[
            {"id": f"T{k}", "origin": "甲站", "destination": "乙站",
             "start": 25200 + k * 3600, "end": 25200 + k * 3600 + 1800,
             "distance": 1000}
            for k in range(13)
        ])),
        id="13 个班次",
    ),
    pytest.param(
        mutated(lambda b: b["trips"].append(
            {"id": "T1", "origin": "甲站", "destination": "乙站",
             "start": 30000, "end": 31000, "distance": 1000}
        )),
        id="班次 ID 重复",
    ),
    pytest.param(
        mutated(lambda b: b["trips"].__setitem__(0, {
            "id": "T1", "origin": "甲站", "destination": "乙站",
            "start": 27000, "end": 27000, "distance": 1000,
        })),
        id="结束等于开始",
    ),
    pytest.param(
        mutated(lambda b: b["trips"].__setitem__(0, {
            "id": "T1", "origin": "甲站", "destination": "乙站",
            "start": 28000, "end": 27000, "distance": 1000,
        })),
        id="结束早于开始",
    ),
    pytest.param(
        mutated(lambda b: b["trips"][0].update(start=-1)),
        id="开始时刻为负",
    ),
    pytest.param(
        mutated(lambda b: b["trips"][0].update(distance=-5)),
        id="里程为负",
    ),
    pytest.param(
        mutated(lambda b: b["trips"][0].update(start=25200.5)),
        id="时间为小数",
    ),
    pytest.param(
        mutated(lambda b: b["trips"][0].update(start=True)),
        id="时间为布尔",
    ),
    pytest.param(
        mutated(lambda b: b["trips"][0].update(origin="未知站")),
        id="起点未登记",
    ),
    pytest.param(
        mutated(lambda b: b["trips"][0].update(destination="未知站")),
        id="终点未登记",
    ),
    pytest.param(
        mutated(lambda b: b.update(depot="未知车场")),
        id="车场未登记",
    ),
    pytest.param(
        mutated(lambda b: b["locations"].append("甲站")),
        id="地点名重复",
    ),
    pytest.param(
        mutated(lambda b: b.update(locations=[f"地点{k}" for k in range(9)])),
        id="9 个地点",
    ),
    pytest.param(
        mutated(lambda b: b.update(deadhead_seconds=[[0, 600], [600, 0]])),
        id="空驶秒数矩阵行数不符",
    ),
    pytest.param(
        mutated(lambda b: b["deadhead_meters"].__setitem__(1, [8000, 0])),
        id="空驶米数矩阵列数不符",
    ),
    pytest.param(
        mutated(lambda b: b["deadhead_seconds"][0].__setitem__(1, -1)),
        id="空驶秒数为负",
    ),
    pytest.param(
        mutated(lambda b: b.update(range_meters=0)),
        id="续航为 0",
    ),
    pytest.param(
        mutated(lambda b: b.update(range_meters=-100)),
        id="续航为负",
    ),
    pytest.param(
        mutated(lambda b: b.update(range_meters=60000.5)),
        id="续航为小数",
    ),
]


@pytest.mark.parametrize("body", BAD_BODIES)
def test_invalid_input_returns_422_with_location(body: dict) -> None:
    resp = post(body)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail
    # 每个错误都带定位信息
    for err in detail:
        assert err.get("loc")
        assert err.get("msg")


def test_error_location_points_to_offending_trip() -> None:
    body = mutated(lambda b: b["trips"].append(
        {"id": "T2", "origin": "甲站", "destination": "乙站",
         "start": 30000, "end": 29999, "distance": 1000}
    ))
    resp = post(body)
    assert resp.status_code == 422
    locs = [err["loc"] for err in resp.json()["detail"]]
    # trips[1] 的 end<=start，定位应指向第二个班次
    assert any("trips" in loc and 1 in loc for loc in locs)
