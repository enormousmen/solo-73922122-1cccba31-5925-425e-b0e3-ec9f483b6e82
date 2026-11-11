"""核心求解器单元测试。"""

from __future__ import annotations

import itertools
import random
import time

import pytest

from app.schemas import ScheduleRequest
from app.solver import InfeasibleError, compute_plan

# 标准三地点：车场(0)、甲站(1)、乙站(2)
LOCATIONS = ["车场", "甲站", "乙站"]
SECONDS = [
    [0, 600, 900],
    [600, 0, 240],
    [900, 240, 0],
]
METERS = [
    [0, 8000, 11000],
    [8000, 0, 4000],
    [11000, 4000, 0],
]


def make_request(
    trips: list[dict],
    *,
    range_meters: int = 60000,
    locations: list[str] | None = None,
    depot: str = "车场",
    seconds: list[list[int]] | None = None,
    meters: list[list[int]] | None = None,
) -> ScheduleRequest:
    return ScheduleRequest(
        depot=depot,
        locations=locations or LOCATIONS,
        deadhead_seconds=seconds or SECONDS,
        deadhead_meters=meters or METERS,
        range_meters=range_meters,
        trips=trips,
    )


def trip(
    tid: str,
    origin: str,
    destination: str,
    start: int,
    end: int,
    distance: int,
) -> dict:
    return {
        "id": tid,
        "origin": origin,
        "destination": destination,
        "start": start,
        "end": end,
        "distance": distance,
    }


# --------------------------------------------------------------------------
# 基础衔接
# --------------------------------------------------------------------------
def test_single_trip_one_vehicle() -> None:
    req = make_request([trip("T1", "甲站", "乙站", 25200, 27000, 20000)])
    plan = compute_plan(req)
    assert plan.vehicle_count == 1
    v = plan.vehicles[0]
    assert v.trips == ["T1"]
    assert v.depot_departure_second == 25200 - 600
    assert v.depot_return_second == 27000 + 900
    assert v.pull_out_meters == 8000
    assert v.between_trips_meters == 0
    assert v.trips_meters == 20000
    assert v.pull_in_meters == 11000
    assert v.total_meters == 39000


def test_chain_with_exact_equality_connects() -> None:
    """等号可接上：上一班结束 + 空驶 == 下一班开始。"""
    req = make_request(
        [
            trip("T1", "甲站", "乙站", 600, 1000, 5000),
            # 乙站 -> 甲站空驶 240 秒，1000 + 240 = 1240 恰好接上
            trip("T2", "甲站", "乙站", 1240, 2000, 5000),
        ]
    )
    plan = compute_plan(req)
    assert plan.vehicle_count == 1
    assert plan.vehicles[0].trips == ["T1", "T2"]
    assert plan.vehicles[0].between_trips_meters == 4000  # 乙->甲 4000 米


def test_one_second_late_forces_second_vehicle() -> None:
    req = make_request(
        [
            trip("T1", "甲站", "乙站", 600, 1000, 5000),
            trip("T2", "甲站", "乙站", 1239, 2000, 5000),  # 差 1 秒接不上
        ]
    )
    plan = compute_plan(req)
    assert plan.vehicle_count == 2
    assert sorted(v.trips for v in plan.vehicles) == [["T1"], ["T2"]]


def test_overlapping_trips_need_two_vehicles() -> None:
    req = make_request(
        [
            trip("A1", "甲站", "乙站", 25200, 27000, 15000),
            trip("A2", "乙站", "甲站", 25800, 27600, 15000),
        ]
    )
    plan = compute_plan(req)
    assert plan.vehicle_count == 2


def test_same_start_trips_never_share_vehicle() -> None:
    req = make_request(
        [
            trip("A1", "甲站", "乙站", 25200, 27000, 1000),
            trip("A2", "甲站", "乙站", 25200, 27000, 1000),
        ]
    )
    assert compute_plan(req).vehicle_count == 2


# --------------------------------------------------------------------------
# 续航约束
# --------------------------------------------------------------------------
def test_mileage_equal_to_range_is_allowed() -> None:
    """总里程恰好等于续航：不得超过，等号允许。"""
    # 出场 8000 + 班次 20000 + 回场 11000 = 39000，续航设为 39000
    req = make_request(
        [trip("T1", "甲站", "乙站", 25200, 27000, 20000)],
        range_meters=39000,
    )
    assert compute_plan(req).vehicle_count == 1


def test_chain_mileage_boundary() -> None:
    """链总里程 56000：续航 56000 时 1 车，55999 时被迫 2 车。"""
    trips = [
        trip("T1", "甲站", "乙站", 25200, 27000, 20000),
        trip("T2", "乙站", "甲站", 27600, 29400, 20000),
    ]
    # 8000 + 40000 + 0 + 8000 = 56000，等号允许
    assert compute_plan(make_request(trips, range_meters=56000)).vehicle_count == 1
    # 差 1 米 -> 拆 2 车（各自单班次 39000 米仍可行）
    plan = compute_plan(make_request(trips, range_meters=55999))
    assert plan.vehicle_count == 2


def test_single_trip_over_range_is_infeasible() -> None:
    req = make_request(
        [trip("T1", "甲站", "乙站", 25200, 27000, 20000)],
        range_meters=38999,  # 单班次最小里程 39000，差 1 米 -> 无解
    )
    with pytest.raises(InfeasibleError):
        compute_plan(req)


def test_mileage_limited_forces_split() -> None:
    """OpenAPI 示例：时间上一车可衔接，续航迫使拆成 2 车。"""
    req = make_request(
        [
            trip("T1", "甲站", "乙站", 25200, 27000, 20000),
            trip("T2", "乙站", "甲站", 27600, 29400, 20000),
            trip("T3", "甲站", "乙站", 30000, 31800, 20000),
        ]
    )
    plan = compute_plan(req)
    assert plan.vehicle_count == 2
    groups = sorted(v.trips for v in plan.vehicles)
    assert groups == [["T1", "T2"], ["T3"]]
    by_first = {v.trips[0]: v for v in plan.vehicles}
    assert by_first["T1"].total_meters == 8000 + 40000 + 0 + 8000  # 56000
    assert by_first["T3"].total_meters == 8000 + 20000 + 11000    # 39000
    for v in plan.vehicles:
        assert v.total_meters <= 60000
        assert (
            v.pull_out_meters + v.between_trips_meters + v.trips_meters + v.pull_in_meters
            == v.total_meters
        )


def test_range_large_enough_allows_single_vehicle() -> None:
    req = make_request(
        [
            trip("T1", "甲站", "乙站", 25200, 27000, 20000),
            trip("T2", "乙站", "甲站", 27600, 29400, 20000),
            trip("T3", "甲站", "乙站", 30000, 31800, 20000),
        ],
        range_meters=79000,  # 恰好够一车跑完
    )
    assert compute_plan(req).vehicle_count == 1


# --------------------------------------------------------------------------
# 矩阵语义：直接采用矩阵值（对角线、非对称）
# --------------------------------------------------------------------------
def test_diagonal_deadhead_values_are_used() -> None:
    seconds = [row[:] for row in SECONDS]
    meters = [row[:] for row in METERS]
    seconds[2][2] = 100   # 乙站 -> 乙站 100 秒
    meters[2][2] = 500    # 乙站 -> 乙站 500 米
    req = make_request(
        [
            trip("T1", "甲站", "乙站", 600, 1000, 5000),
            trip("T2", "乙站", "甲站", 1100, 2000, 5000),  # 1000+100 恰好接上
        ],
        seconds=seconds,
        meters=meters,
    )
    plan = compute_plan(req)
    assert plan.vehicle_count == 1
    v = plan.vehicles[0]
    assert v.between_trips_meters == 500
    assert v.total_meters == 8000 + 500 + 10000 + 8000


def test_diagonal_deadhead_can_break_connection() -> None:
    seconds = [row[:] for row in SECONDS]
    seconds[2][2] = 101   # 差 1 秒接不上
    req = make_request(
        [
            trip("T1", "甲站", "乙站", 600, 1000, 5000),
            trip("T2", "乙站", "甲站", 1100, 2000, 5000),
        ],
        seconds=seconds,
    )
    assert compute_plan(req).vehicle_count == 2


def test_asymmetric_matrix_respected() -> None:
    meters = [row[:] for row in METERS]
    meters[0][1] = 20000   # 车场 -> 甲站 去程 20000
    meters[1][0] = 3000    # 甲站 -> 车场 回程 3000
    req = make_request(
        [trip("T1", "甲站", "甲站", 25200, 27000, 10000)],
        meters=meters,
    )
    plan = compute_plan(req)
    v = plan.vehicles[0]
    assert v.pull_out_meters == 20000
    assert v.pull_in_meters == 3000
    assert v.total_meters == 33000


# --------------------------------------------------------------------------
# 无解
# --------------------------------------------------------------------------
def test_infeasible_when_single_trip_exceeds_range() -> None:
    req = make_request(
        [trip("B1", "甲站", "乙站", 25200, 28800, 45000)],
    )
    with pytest.raises(InfeasibleError) as exc:
        compute_plan(req)
    assert exc.value.blocking[0].trip_id == "B1"
    assert any("续航" in r for r in exc.value.blocking[0].reasons)


def test_infeasible_when_depot_unreachable_in_time() -> None:
    # 车场 -> 甲站 600 秒，班次 500 秒就要开始
    req = make_request([trip("B2", "甲站", "乙站", 500, 1000, 1000)])
    with pytest.raises(InfeasibleError) as exc:
        compute_plan(req)
    assert exc.value.blocking[0].trip_id == "B2"
    assert any("晚于班次开始" in r for r in exc.value.blocking[0].reasons)


def test_infeasible_lists_all_blocking_trips() -> None:
    req = make_request(
        [
            trip("OK", "甲站", "乙站", 25200, 27000, 1000),
            trip("BAD1", "甲站", "乙站", 25200, 28800, 45000),
            trip("BAD2", "乙站", "甲站", 100, 200, 1000),  # 车场->乙站 900 秒赶不上
        ]
    )
    with pytest.raises(InfeasibleError) as exc:
        compute_plan(req)
    assert {b.trip_id for b in exc.value.blocking} == {"BAD1", "BAD2"}


# --------------------------------------------------------------------------
# 确定性与规模
# --------------------------------------------------------------------------
def test_deterministic_output() -> None:
    req = make_request(
        [
            trip("T1", "甲站", "乙站", 25200, 27000, 20000),
            trip("T2", "乙站", "甲站", 27600, 29400, 20000),
            trip("T3", "甲站", "乙站", 30000, 31800, 20000),
        ]
    )
    assert compute_plan(req) == compute_plan(req)


def test_twelve_trips_solves_quickly() -> None:
    trips = []
    for k in range(12):
        o, d = ("甲站", "乙站") if k % 2 == 0 else ("乙站", "甲站")
        start = 25200 + k * 2400
        trips.append(trip(f"T{k}", o, d, start, start + 1800, 12000))
    req = make_request(trips, range_meters=50000)
    t0 = time.perf_counter()
    plan = compute_plan(req)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0
    # 每车里程：出场 8000/11000 + 2*12000 + 转场 4000 + 回场 8000/11000
    assert plan.vehicle_count >= 2
    assert sorted(
        itertools.chain.from_iterable(v.trips for v in plan.vehicles),
        key=lambda tid: int(tid[1:]),
    ) == [f"T{k}" for k in range(12)]


# --------------------------------------------------------------------------
# 与暴力枚举所有划分的对拍（随机小实例）
# --------------------------------------------------------------------------
def _brute_force_min_vehicles(req: ScheduleRequest) -> int:
    """独立实现：枚举全部集合划分，取可行划分的最少块数。"""
    trips = req.trips
    n = len(trips)
    loc = {name: i for i, name in enumerate(req.locations)}
    depot = loc[req.depot]
    tmat, dmat, rng = req.deadhead_seconds, req.deadhead_meters, req.range_meters
    orig = [loc[t.origin] for t in trips]
    dest = [loc[t.destination] for t in trips]

    def block_feasible(block: list[int]) -> bool:
        seq = sorted(block, key=lambda i: (trips[i].start, trips[i].end, trips[i].id))
        if tmat[depot][orig[seq[0]]] > trips[seq[0]].start:
            return False
        total = dmat[depot][orig[seq[0]]] + dmat[dest[seq[-1]]][depot]
        for k, i in enumerate(seq):
            total += trips[i].distance
            if k:
                a = seq[k - 1]
                if trips[a].end + tmat[dest[a]][orig[i]] > trips[i].start:
                    return False
                total += dmat[dest[a]][orig[i]]
        return total <= rng

    best = n + 1

    def rec(remaining: list[int], blocks: int) -> None:
        nonlocal best
        if blocks >= best:
            return
        if not remaining:
            best = blocks
            return
        first, rest = remaining[0], remaining[1:]
        # first 单独成块，或并入其余元素构成的任意子集
        for r in range(len(rest) + 1):
            for extra in itertools.combinations(rest, r):
                block = [first, *extra]
                if block_feasible(block):
                    others = [x for x in rest if x not in extra]
                    rec(others, blocks + 1)

    rec(list(range(n)), 0)
    return best


def _random_request(rng: random.Random, n: int) -> ScheduleRequest:
    locations = ["车场", "甲站", "乙站", "丙站"]
    m = len(locations)
    seconds = [[rng.randint(0, 1200) for _ in range(m)] for _ in range(m)]
    meters = [[rng.randint(0, 15000) for _ in range(m)] for _ in range(m)]
    range_meters = rng.choice([50000, 80000, 120000])
    trips = []
    for k in range(n):
        start = rng.randint(1200, 20000)
        end = start + rng.randint(60, 1800)
        o, d = rng.sample(locations, 2)
        oi, di = locations.index(o), locations.index(d)
        # 保证单班次可行（否则整体无解，不属于本对拍范围）
        max_dist = range_meters - meters[0][oi] - meters[di][0]
        if max_dist < 0 or seconds[0][oi] > start:
            continue
        trips.append(
            trip(f"R{k}", o, d, start, end, rng.randint(0, max_dist))
        )
    if not trips:
        trips = [trip("R0", "甲站", "乙站", 5000, 6000, 1000)]
    return make_request(
        trips,
        range_meters=range_meters,
        locations=locations,
        seconds=seconds,
        meters=meters,
    )


def test_against_brute_force_on_random_instances() -> None:
    rng = random.Random(20261111)
    for _ in range(40):
        n = rng.randint(1, 7)
        req = _random_request(rng, n)
        plan = compute_plan(req)
        assert plan.vehicle_count == _brute_force_min_vehicles(req)
        # 方案自检：每个班次恰出现一次，每车里程不超续航
        all_trips = list(itertools.chain.from_iterable(v.trips for v in plan.vehicles))
        assert sorted(all_trips) == sorted(t.id for t in req.trips)
        for v in plan.vehicles:
            assert v.total_meters <= req.range_meters
