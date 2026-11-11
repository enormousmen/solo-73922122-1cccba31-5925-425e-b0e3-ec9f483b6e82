"""FastAPI 应用入口。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
文档：/docs（Swagger UI）、/redoc、/openapi.json
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from .schemas import (
    BlockingTripOut,
    MileageBreakdown,
    PlanInfeasible,
    PlanSuccess,
    ScheduleRequest,
    ScheduleResponse,
    VehiclePlanOut,
)
from .solver import InfeasibleError, compute_plan

app = FastAPI(
    title="公交电动车配车测算 API",
    version="1.0.0",
    description=(
        "核算一批固定班次（1～12 个）最少需要几辆电动车。车辆 0 秒起自同一车场"
        "满电出车，允许等待、不补能，末班后必须回场；班次衔接不得迟到（等号可接上），"
        "空驶时间与里程直接采用输入矩阵；每车含出场、转场、回场的总里程不得超过统一续航。"
        "精确求最少车辆数并给出任一最优方案（逐车班次顺序与总里程）；无解时说明原因，"
        "不返回部分安排；输入非法时返回 422 并定位出错字段。"
    ),
)

# ---------------------------------------------------------------------------
# OpenAPI 示例
# ---------------------------------------------------------------------------
# 累计里程受限：三个班次时间上完全可由一辆车衔接，但一车跑完全程需
# 8000+20000*3+11000=79000 米，超过续航 60000 米，只能拆成 2 车。
_MILEAGE_LIMITED_EXAMPLE: dict[str, Any] = {
    "summary": "累计里程受限：时间可衔接，但续航迫使 1 车变 2 车",
    "value": {
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
            {"id": "T2", "origin": "乙站", "destination": "甲站",
             "start": 27600, "end": 29400, "distance": 20000},
            {"id": "T3", "origin": "甲站", "destination": "乙站",
             "start": 30000, "end": 31800, "distance": 20000},
        ],
    },
}

# 时间冲突：两个班次时段重叠，必须 2 车。
_TIME_OVERLAP_EXAMPLE: dict[str, Any] = {
    "summary": "时段重叠：两个班次时间冲突，需 2 车",
    "value": {
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
            {"id": "A1", "origin": "甲站", "destination": "乙站",
             "start": 25200, "end": 27000, "distance": 15000},
            {"id": "A2", "origin": "乙站", "destination": "甲站",
             "start": 25800, "end": 27600, "distance": 15000},
        ],
    },
}

# 无解：单班次最小总里程 8000+45000+11000=64000 米 > 续航 60000 米。
_INFEASIBLE_EXAMPLE: dict[str, Any] = {
    "summary": "无解：单班次里程已超续航，整体无解且不返回部分安排",
    "value": {
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
            {"id": "B1", "origin": "甲站", "destination": "乙站",
             "start": 25200, "end": 28800, "distance": 45000},
        ],
    },
}


@app.get("/health", tags=["运维"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["运维"])
def root() -> dict[str, Any]:
    return {
        "service": "bus-ev-vehicle-scheduling",
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
    }


@app.post(
    "/api/v1/vehicle-schedules/compute",
    response_model=ScheduleResponse,
    tags=["配车测算"],
    summary="固定班次最少配车数测算",
    responses={
        200: {
            "description": "有解返回 ok（最少车辆数与任一最优方案）；无解返回 "
            "infeasible（原因说明，不含部分安排）。均为 HTTP 200，用 status "
            "字段区分；请求参数本身非法时返回 422 并定位出错字段。",
        }
    },
)
def compute(
    request: ScheduleRequest = Body(
        ...,
        openapi_examples={
            "mileage_limited": _MILEAGE_LIMITED_EXAMPLE,
            "time_overlap": _TIME_OVERLAP_EXAMPLE,
            "infeasible": _INFEASIBLE_EXAMPLE,
        },
    )
) -> JSONResponse:
    try:
        plan = compute_plan(request)
    except InfeasibleError as exc:
        payload = PlanInfeasible(
            reason=str(exc),
            blocking_trips=[
                BlockingTripOut(trip_id=b.trip_id, reasons=b.reasons)
                for b in exc.blocking
            ],
        )
        return JSONResponse(content=payload.model_dump())

    payload = PlanSuccess(
        vehicle_count=plan.vehicle_count,
        vehicles=[
            VehiclePlanOut(
                vehicle=k,
                trips=v.trips,
                depot_departure_second=v.depot_departure_second,
                depot_return_second=v.depot_return_second,
                mileage=MileageBreakdown(
                    pull_out=v.pull_out_meters,
                    between_trips=v.between_trips_meters,
                    trips=v.trips_meters,
                    pull_in=v.pull_in_meters,
                    total=v.total_meters,
                ),
            )
            for k, v in enumerate(plan.vehicles, start=1)
        ],
    )
    return JSONResponse(content=payload.model_dump())
