"""最少配车数的精确求解。

问题：给定 1～12 个固定班次（起终点、起止秒、行驶米数）、含车场的地点间
直达空驶秒数 / 米数矩阵与统一续航，求覆盖全部班次所需的最少车辆数，
并给出任一最优方案（逐车班次顺序与总里程）。

约束：
- 车辆 0 秒起可自同一车场满电出车，允许等待、不补能，末班后必须回场；
- 班次衔接不得迟到：上一班结束时刻 + 终点到下一班起点的空驶秒数
  ≤ 下一班开始时刻（等号可接上）；
- 每车总里程（出场 + 班次行驶 + 班次间转场 + 回场）≤ 续航。

关键观察：同一车辆执行的班次，其开始时刻必然严格递增——衔接要求
start_b ≥ end_a + 空驶 ≥ end_a > start_a。因此任一班次集合若能由一车完成，
执行顺序唯一（按开始时刻升序），可行性 O(n) 可判定。问题等价于
「把班次集合划分成最少个一车可行子集」，用子集 DP 精确求解：

    f[∅] = 0，f[S] = min{ f[S \\ T] + 1 | ∅ ≠ T ⊆ S 且 T 一车可行 }

状态 2^n、转移枚举子集，总复杂度 O(3^n)，n ≤ 12 时约 53 万次转移。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schemas import ScheduleRequest


@dataclass(frozen=True)
class BlockingTrip:
    """任何车辆都无法单独完成的班次。"""

    trip_id: str
    reasons: list[str]


class InfeasibleError(Exception):
    """存在任何车都接不了的班次，整体无解（不产出部分安排）。"""

    def __init__(self, blocking: list[BlockingTrip]) -> None:
        self.blocking = blocking
        ids = "、".join(b.trip_id for b in blocking)
        super().__init__(f"班次 {ids} 无法由任何一辆车单独完成，配车无解")


@dataclass(frozen=True)
class VehiclePlan:
    trips: list[str]                # 按执行顺序的班次 ID
    depot_departure_second: int     # 最晚出场时刻
    depot_return_second: int        # 回场时刻
    pull_out_meters: int            # 出场空驶
    between_trips_meters: int       # 班次间转场空驶
    trips_meters: int               # 班次行驶里程
    pull_in_meters: int             # 回场空驶
    total_meters: int               # 总里程 ≤ 续航


@dataclass(frozen=True)
class Plan:
    vehicles: list[VehiclePlan] = field(default_factory=list)

    @property
    def vehicle_count(self) -> int:
        return len(self.vehicles)


def compute_plan(request: ScheduleRequest) -> Plan:
    """精确求最少车辆数与任一最优方案；无解时抛出 :class:`InfeasibleError`。"""
    trips = request.trips
    n = len(trips)
    loc_index = {name: i for i, name in enumerate(request.locations)}
    depot = loc_index[request.depot]
    tmat = request.deadhead_seconds
    dmat = request.deadhead_meters
    rng = request.range_meters

    origin = [loc_index[t.origin] for t in trips]
    destination = [loc_index[t.destination] for t in trips]

    # ---- 单班次可行性：任何车都单独接不了的班次 -> 整体无解 ----
    blocking: list[BlockingTrip] = []
    for i, trip in enumerate(trips):
        reasons: list[str] = []
        pull_out_seconds = tmat[depot][origin[i]]
        if pull_out_seconds > trip.start:
            reasons.append(
                f"车辆 0 秒自车场 {request.depot!r} 出场，空驶 {pull_out_seconds} 秒 "
                f"到起点 {trip.origin!r} 已晚于班次开始时刻 {trip.start} 秒"
            )
        solo_meters = (
            dmat[depot][origin[i]] + trip.distance + dmat[destination[i]][depot]
        )
        if solo_meters > rng:
            reasons.append(
                f"单班次最小总里程 {solo_meters} 米（出场 {dmat[depot][origin[i]]} "
                f"+ 班次 {trip.distance} + 回场 {dmat[destination[i]][depot]}）"
                f"超过续航 {rng} 米"
            )
        if reasons:
            blocking.append(BlockingTrip(trip_id=trip.id, reasons=reasons))
    if blocking:
        raise InfeasibleError(blocking)

    # ---- 子集一车可行性：顺序唯一（按开始时刻升序），逐一检查衔接与总里程 ----
    order = sorted(range(n), key=lambda i: (trips[i].start, trips[i].end, trips[i].id))
    size = 1 << n
    feasible = bytearray(size)
    mileage = [0] * size

    for mask in range(1, size):
        seq = [i for i in order if mask & (1 << i)]
        chainable = all(
            trips[a].end + tmat[destination[a]][origin[b]] <= trips[b].start
            for a, b in zip(seq, seq[1:])
        )
        if not chainable:
            continue
        total = dmat[depot][origin[seq[0]]] + dmat[destination[seq[-1]]][depot]
        total += sum(trips[i].distance for i in seq)
        total += sum(
            dmat[destination[a]][origin[b]] for a, b in zip(seq, seq[1:])
        )
        if total <= rng:
            feasible[mask] = 1
            mileage[mask] = total

    # ---- 子集 DP：f[S] = 覆盖 S 的最少车辆数 ----
    best = [n + 1] * size
    best[0] = 0
    choice = [0] * size
    for mask in range(1, size):
        sub = mask
        while sub:
            if feasible[sub] and best[mask ^ sub] + 1 < best[mask]:
                best[mask] = best[mask ^ sub] + 1
                choice[mask] = sub
            sub = (sub - 1) & mask

    # 单班次均可行，必然有解；回溯出划分
    groups: list[int] = []
    rest = size - 1
    while rest:
        sub = choice[rest]
        groups.append(sub)
        rest ^= sub

    vehicles: list[VehiclePlan] = []
    for mask in groups:
        seq = [i for i in order if mask & (1 << i)]
        first, last = seq[0], seq[-1]
        vehicles.append(
            VehiclePlan(
                trips=[trips[i].id for i in seq],
                depot_departure_second=(
                    trips[first].start - tmat[depot][origin[first]]
                ),
                depot_return_second=(
                    trips[last].end + tmat[destination[last]][depot]
                ),
                pull_out_meters=dmat[depot][origin[first]],
                between_trips_meters=sum(
                    dmat[destination[a]][origin[b]] for a, b in zip(seq, seq[1:])
                ),
                trips_meters=sum(trips[i].distance for i in seq),
                pull_in_meters=dmat[destination[last]][depot],
                total_meters=mileage[mask],
            )
        )
    # 输出顺序确定化：按出场时刻、首班 ID 排序
    vehicles.sort(key=lambda v: (v.depot_departure_second, v.trips[0]))
    return Plan(vehicles=vehicles)
