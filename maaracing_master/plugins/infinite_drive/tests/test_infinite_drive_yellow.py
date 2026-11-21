from __future__ import annotations

import cv2
import numpy as np
import pytest


def test_yellow_tracks_keep_solid_road_edge_and_reject_yellow_wall() -> None:
    from maaracing_master.plugins.infinite_drive.yellow_line import detect_yellow_edges

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.line(frame, (655, 380), (1225, 665), (228, 202, 24), 6)
    cv2.rectangle(frame, (140, 400), (220, 610), (228, 202, 24), -1)
    edges = detect_yellow_edges(frame)
    assert len(edges) == 1
    assert edges[0].slope == pytest.approx(2.0, abs=0.1)


def test_yellow_rejects_short_and_washed_out_traces() -> None:
    from maaracing_master.plugins.infinite_drive.yellow_line import detect_yellow_edges

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.line(frame, (880, 500), (990, 555), (228, 202, 24), 6)
    cv2.line(frame, (620, 380), (210, 600), (228, 202, 160), 6)
    assert not detect_yellow_edges(frame)


def test_dim_wet_curb_can_anchor_only_with_two_inward_white_lines() -> None:
    from maaracing_master.plugins.infinite_drive.yellow_line import YellowAnchor, detect_yellow_edges

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.line(frame, (691, 378), (1216, 503), (148, 124, 32), 7)
    edges = detect_yellow_edges(frame)
    assert len(edges) == 1

    def line(offset):
        slope = offset * 3.5 / 1.25
        return {"L": offset, "fits": [{"s": slope, "a": 640 - slope * 366,
                                       "y0": 390., "y1": 520., "rms": 2.}]}

    anchor = YellowAnchor()
    for frame_no in range(3):
        assert anchor.update(edges, [line(0.5)], 1.25, frame_no * 0.1, y_v=366.) is None
    for frame_no in range(3, 5):
        assert anchor.update(edges, [line(0.5), line(-0.5)], 1.25, frame_no * 0.1, y_v=366.) is None
    result = anchor.update(edges, [line(0.5), line(-0.5)], 1.25, 0.5, y_v=366.)
    assert result == pytest.approx(0.5)


@pytest.mark.parametrize("side", [-1, 1])
def test_yellow_anchor_counts_lane_widths_and_skips_hidden_white_line(side) -> None:
    from maaracing_master.plugins.infinite_drive.yellow_line import YellowAnchor, YellowEdge

    anchor = YellowAnchor()
    def line(offset):
        slope = side * offset * 3.5 / 1.25
        return {"local": True, "fits": [{"s": slope, "a": 640 - slope * 365,
                                        "y0": 410., "y1": 540., "rms": 2.}], "L": side * offset}

    curb = line(0.75)
    white = line(-1.25)
    fit = curb["fits"][0]
    edge = YellowEdge(fit["s"], fit["a"], 400., 540.)
    for n in range(3):
        assert anchor.update([edge], [curb], 1.25, n * 0.1, y_v=365.) is None
    for n in range(3, 5):
        assert anchor.update([edge], [curb, white], 1.25, n * 0.1, y_v=365.) is None
    assert anchor.update([edge], [curb, white], 1.25, 0.5, y_v=365.) == pytest.approx(side * 1.25)
    assert anchor.update([], [curb, white], 1.25, 0.6, y_v=365.) is None
    assert anchor.update([edge], [curb, white], 1.25, 1.4, y_v=365.) is None
