import pytest

from hpgl_utils import (
    ensure_parked, hpgl_bounds, jog_commands, outline_commands, pen_test_commands, pens_used,
    prepare_job_commands, split_command_into_pairs, split_hpgl_into_commands,
)


def test_split_hpgl_into_commands_drops_empty_parts():
    assert split_hpgl_into_commands("IN; SP1;;PU0,0;\n") == ["IN", "SP1", "PU0,0"]


def test_split_command_into_pairs_chunks():
    cmd = "PD" + ",".join(str(i) for i in range(10))  # 5 pairs
    assert split_command_into_pairs(cmd, max_pairs=2) == ["PD0,1,2,3;", "PD4,5,6,7;", "PD8,9;"]
    assert split_command_into_pairs("PU") == ["PU;"]


def test_split_command_into_pairs_rejects_odd_count():
    with pytest.raises(ValueError):
        split_command_into_pairs("PD1,2,3")


def test_prepare_job_commands_injects_speed_and_parks():
    hpgl = "IN;DF;VS99;SP1;PU0,0;PD10,10,20,20,30,30;PU;SP2;PD5,5;PA;PU0,0;SP0;IN;"
    cmds = prepare_job_commands(hpgl, pen_speeds={1: 10, 2: 6}, max_pairs=2)
    assert cmds == [
        "IN;", "DF;", "SP1;", "VS10;", "PU0,0;", "PD10,10,20,20;", "PD30,30;", "PU;",
        "SP2;", "VS6;", "PD5,5;", "PA;", "PU0,0;", "SP0;",
    ]
    assert "VS99;" not in cmds


def test_prepare_job_commands_default_speed():
    cmds = prepare_job_commands("SP3;PD1,1;", default_speed=7)
    assert cmds[:2] == ["SP3;", "VS7;"]


def test_ensure_parked_variants():
    assert ensure_parked(["PD1,1;"]) == ["PD1,1;", "PU;", "SP0;"]
    assert ensure_parked(["PD1,1;", "SP0;"]) == ["PD1,1;", "PU;", "SP0;"]
    assert ensure_parked(["PU0,0;", "SP0;", "IN;"]) == ["PU0,0;", "SP0;"]
    assert ensure_parked(["PU;", "SP0;"]) == ["PU;", "SP0;"]
    assert ensure_parked([]) == ["PU;", "SP0;"]


def test_pens_used_in_order_without_zero():
    assert pens_used(["SP2;", "PD1,1;", "SP1;", "SP2;", "SP0;"]) == [2, 1]


def test_jog_commands_are_relative_and_restore_absolute():
    assert jog_commands("up", 100) == "PR;PU0,100;PA;"
    assert jog_commands("left", 50, pen_down=True) == "PR;PD-50,0;PA;"


def test_pen_test_commands():
    cmds = pen_test_commands([1, 3], pen_speeds={1: 10})
    assert cmds[0] == "IN;" and cmds[-1] == "SP0;"
    assert "SP1;" in cmds and "VS10;" in cmds and "SP3;" in cmds
    assert pens_used(cmds) == [1, 3]


def test_hpgl_bounds_ignores_relative_moves():
    cmds = ["IN;", "SP1;", "PU100,200;", "PD300,50,150,400;", "PR;", "PU9000,9000;", "PA;", "PU;", "SP0;"]
    assert hpgl_bounds(cmds) == (100, 50, 300, 400)
    assert hpgl_bounds(["IN;", "SP0;"]) is None


def test_outline_commands_trace_corners_pen_up():
    cmds = outline_commands([(0, 0, 400, 800)], passes=1)
    assert cmds[:3] == ["IN;", "PA;", "SP0;"]
    assert cmds[3:8] == ["PU0,0;", "PU400,0;", "PU400,800;", "PU0,800;", "PU0,0;"]
    assert cmds[-2:] == ["PU0,0;", "SP0;"]
    assert not any(c.startswith("PD") for c in cmds)
    assert len(outline_commands([(0, 0, 1, 1), (5, 5, 2, 2)], passes=2)) == 3 + 2 * 2 * 5 + 2
