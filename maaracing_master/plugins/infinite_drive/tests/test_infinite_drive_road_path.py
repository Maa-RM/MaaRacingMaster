from __future__ import annotations

import pytest


@pytest.mark.parametrize(("line", "side"), [(line, side) for line in (-1, 0, 1) for side in (-1, 1)])
def test_pass_aims_at_its_side_of_the_shared_white_line(line, side) -> None:
    from maaracing_master.plugins.infinite_drive.road_path import plan_road_path

    home = line + side * 0.5
    traffic = line - side * 0.45
    path = plan_road_path(home, home, [(traffic, 32.0)])
    assert path.x_at(32.0) == pytest.approx(line + side * 0.2)
    assert path.throttle == 1.0


@pytest.mark.parametrize("mirror", [-1, 1])
def test_path_changes_lanes_early_and_unwinds_before_reaching_pass_offset(mirror) -> None:
    from maaracing_master.plugins.infinite_drive.driving import HeadingControl
    from maaracing_master.plugins.infinite_drive.road_path import plan_road_path

    start = 0.5 * mirror
    path = plan_road_path(start, start, [(start, 34.0)])
    side = 1 if path.x_at(34) > start else -1
    assert (path.x_at(8) - start) * side >= 0.10 - 1e-9
    assert 0.60 <= abs(path.x_at(34) - start) <= 0.85

    controller = HeadingControl(3.5)
    x = path.x_at(25)
    preview = path.x_at(25 + 12)
    assert (path.x_at(34) - x) * side > 0.05
    assert (preview - x) * side > 0
    assert controller.update(preview - x, side * 0.08, 1, preview_m=12) * side < 0


def test_adjacent_cars_in_sequence_share_one_pass_line() -> None:
    from maaracing_master.plugins.infinite_drive.road_path import plan_road_path

    path = plan_road_path(0.5, 0.5, [(0.5, 30.0), (0.5, 42.0)])
    destinations = [path.x_at(z) - 0.5 for z in (30, 36, 42)]
    assert all(abs(x) >= 0.60 for x in destinations)
    assert all(a * b > 0 for a, b in zip(destinations, destinations[1:]))
    assert path.throttle == 1.0


def test_staggered_traffic_in_all_four_lanes_leaves_a_through_path() -> None:
    from maaracing_master.plugins.infinite_drive.road_path import plan_road_path

    # 两边的间隙前后错开，必须先进中缝右侧，再移到左侧。
    cars = [(-1.5, 25.0), (-0.5, 25.0), (0.5, 47.0), (1.5, 42.0)]
    path = plan_road_path(-0.3, -0.5, cars)
    assert path.points[-1][0] >= 47
    assert path.throttle == 1.0
    assert path.x_at(25) > 0 > path.x_at(47)
    for x, z in cars:
        assert all(abs(path.x_at(z + dz) - x) >= 0.60 for dz in (-5, 0, 7))


def test_near_full_block_releases_throttle_and_opening_recovers() -> None:
    from maaracing_master.plugins.infinite_drive.road_path import plan_road_path

    barriers = [(x, 10.0) for x in (-1.5, -0.5, 0.5, 1.5)]
    blocked = plan_road_path(-0.5, -0.5, barriers)
    assert blocked.points == ((0.0, -0.5),)
    assert blocked.throttle == 0.0

    following = plan_road_path(-0.5, -0.5, barriers[2:])
    assert following.throttle == 1.0
    assert following.x_at(12) < 0
    assert plan_road_path(-0.5, -0.5, []).x_at(12) == pytest.approx(-0.5)


def test_path_prefers_longest_reachable_prefix_while_slowing_for_a_block_ahead() -> None:
    from maaracing_master.plugins.infinite_drive.road_path import plan_road_path

    cars = [(-0.5, 29.0), (0.5, 29.0), (-1.5, 21.0), (1.5, 22.0)]
    path = plan_road_path(-0.5, -0.5, cars)
    assert 0 < path.points[-1][0] < 29
    assert 0 < path.throttle < 1
