"""配车测算 API 的请求 / 响应数据模型。

输入约定：
- 时间、距离均为非负整数（严格整数，拒绝 1.5 之类的小数与布尔值）；
- 班次结束时刻必须严格晚于开始时刻；
- 续航为正整数；
- 地点（含车场）1～8 个，班次 1～12 个；
- 空驶时间 / 里程直接采用矩阵值（含对角线，不做对称性、三角不等式假设）。

校验失败由 FastAPI 返回 422，detail 中带出出错字段位置（loc）与中文说明。
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, StrictInt, model_validator

# 非负严格整数（秒、米）
NonNegInt = Annotated[StrictInt, Field(ge=0)]
# 正严格整数（续航）
PosInt = Annotated[StrictInt, Field(gt=0)]
# 非空地点名
LocationName = Annotated[str, Field(min_length=1)]


class Trip(BaseModel):
    """一个固定班次。"""

    id: str = Field(..., min_length=1, description="班次唯一 ID")
    origin: LocationName = Field(..., description="起点地点名，须出现在 locations 中")
    destination: LocationName = Field(..., description="终点地点名，须出现在 locations 中")
    start: NonNegInt = Field(..., description="班次开始时刻（秒，从 0 起计）")
    end: NonNegInt = Field(..., description="班次结束时刻（秒），必须严格大于 start")
    distance: NonNegInt = Field(..., description="班次行驶里程（米）")

    @model_validator(mode="after")
    def _check_end_after_start(self) -> "Trip":
        if self.end <= self.start:
            raise ValueError(
                f"班次 {self.id!r} 的 end（{self.end}）必须严格大于 start（{self.start}）"
            )
        return self


class ScheduleRequest(BaseModel):
    """配车测算请求。"""

    depot: LocationName = Field(..., description="车场地点名，须出现在 locations 中")
    locations: list[LocationName] = Field(
        ..., min_length=1, max_length=8, description="全部地点（含车场），1～8 个，名称唯一"
    )
    deadhead_seconds: list[list[NonNegInt]] = Field(
        ...,
        description="地点间直达空驶秒数矩阵，行=起点、列=终点，维度与 locations 一致；"
        "衔接时直接采用该矩阵值（含对角线）",
    )
    deadhead_meters: list[list[NonNegInt]] = Field(
        ...,
        description="地点间直达空驶米数矩阵，维度与 locations 一致；计入车辆总里程",
    )
    range_meters: PosInt = Field(
        ..., description="车辆统一续航（米），正整数；每车出场+班次+转场+回场总里程不得超过"
    )
    trips: list[Trip] = Field(..., min_length=1, max_length=12, description="固定班次，1～12 个，ID 唯一")

    @model_validator(mode="after")
    def _check_consistency(self) -> "ScheduleRequest":
        # 地点名唯一
        seen: set[str] = set()
        for i, name in enumerate(self.locations):
            if name in seen:
                raise ValueError(f"locations[{i}] 地点名 {name!r} 重复")
            seen.add(name)

        if self.depot not in seen:
            raise ValueError(f"depot={self.depot!r} 不在 locations 中")

        # 矩阵维度与地点数一致
        n = len(self.locations)
        for field_name, matrix in (
            ("deadhead_seconds", self.deadhead_seconds),
            ("deadhead_meters", self.deadhead_meters),
        ):
            if len(matrix) != n:
                raise ValueError(
                    f"{field_name} 应有 {n} 行（与 locations 数量一致），实际 {len(matrix)} 行"
                )
            for r, row in enumerate(matrix):
                if len(row) != n:
                    raise ValueError(
                        f"{field_name}[{r}] 应有 {n} 列，实际 {len(row)} 列"
                    )

        # 班次 ID 唯一，起终点必须已登记
        ids: set[str] = set()
        for i, trip in enumerate(self.trips):
            if trip.id in ids:
                raise ValueError(f"trips[{i}] 的班次 ID {trip.id!r} 重复")
            ids.add(trip.id)
            for field_name, loc in (("origin", trip.origin), ("destination", trip.destination)):
                if loc not in seen:
                    raise ValueError(
                        f"trips[{i}]（班次 {trip.id!r}）的 {field_name}={loc!r} 不在 locations 中"
                    )
        return self


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------
class MileageBreakdown(BaseModel):
    """单车总里程构成（米），四项之和等于 total。"""

    pull_out: int = Field(..., description="出场空驶（车场 -> 首个班次起点）")
    between_trips: int = Field(..., description="班次间转场空驶合计")
    trips: int = Field(..., description="班次行驶里程合计")
    pull_in: int = Field(..., description="回场空驶（末班终点 -> 车场）")
    total: int = Field(..., description="总里程 = 出场 + 转场 + 班次 + 回场，不超过续航")


class VehiclePlanOut(BaseModel):
    """一辆车的执行方案。"""

    vehicle: int = Field(..., description="车辆序号（从 1 开始，按出场时刻排序）")
    trips: list[str] = Field(..., description="按执行顺序排列的班次 ID")
    depot_departure_second: int = Field(
        ..., description="最晚出场时刻（秒）= 首班开始 - 车场到首班起点空驶秒数"
    )
    depot_return_second: int = Field(
        ..., description="回场时刻（秒）= 末班结束 + 末班终点到车场空驶秒数"
    )
    mileage: MileageBreakdown


class PlanSuccess(BaseModel):
    """有解：返回最少车辆数与任一最优方案。"""

    status: Literal["ok"] = "ok"
    vehicle_count: int = Field(..., description="覆盖全部班次所需的最少车辆数")
    vehicles: list[VehiclePlanOut] = Field(..., description="逐车班次顺序与总里程")


class BlockingTripOut(BaseModel):
    """任何车辆都无法单独完成的班次（导致整体无解）。"""

    trip_id: str
    reasons: list[str]


class PlanInfeasible(BaseModel):
    """无解：说明原因，不返回任何部分安排。"""

    status: Literal["infeasible"] = "infeasible"
    reason: str = Field(..., description="人类可读的中文原因说明")
    blocking_trips: list[BlockingTripOut] = Field(
        ..., description="无法由任何一辆车单独完成的班次及具体原因"
    )


ScheduleResponse = Annotated[
    Union[PlanSuccess, PlanInfeasible],
    Field(discriminator="status"),
]
