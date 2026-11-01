import math

from test_campaths import _sequence, _spatial, write_path

from highlights.recording.campaths import load_campaths, pose_at
from highlights.recording.cinematic import add_cinematic_sequences
from highlights.recording.splice_plan import coverage, plan_splice
from highlights.video.splice import splice_filter


def write_oriented(folder, name, points):
    """points: (t, x, y, z, pitch, yaw) written as Euler-only keyframes."""
    folder.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f'<p t="{t}" x="{x}" y="{y}" z="{z}" fov="90" rx="0" ry="{p}" rz="{yw}"/>'
        for t, x, y, z, p, yw in points
    )
    file = folder / f"{name}.dz"
    file.write_text(f"<campath><points>{body}</points></campath>")
    return file


def static_path(tmp_path, name="cam", yaw=0.0, duration=30.0, pos=(0, 0, 0)):
    write_oriented(tmp_path / "Nuke", name, [(100, *pos, 0, yaw), (100 + duration, *pos, 0, yaw)])
    return load_campaths(tmp_path, "de_nuke")[0]


def test_euler_orientation_conventions(tmp_path):
    write_oriented(tmp_path / "Nuke", "p", [(0, 0, 0, 0, 90, 0), (1, 0, 0, 0, 0, 90)])
    path = load_campaths(tmp_path, "de_nuke")[0]
    down = path.keyframes[0].forward
    left = path.keyframes[1].forward
    assert down[2] < -0.99  # positive pitch looks down
    assert left[1] > 0.99  # positive yaw turns toward +y (left)
    assert path.keyframes[0].t == 0 and path.duration == 1


def test_quaternion_wins_over_euler(tmp_path):
    folder = tmp_path / "Nuke"
    folder.mkdir()
    half = math.sqrt(0.5)  # 90 degrees of yaw about z
    point = lambda t: (  # noqa: E731
        f'<p t="{t}" x="0" y="0" z="0" fov="90" rx="0" ry="0" rz="0" '
        f'qw="{half}" qx="0" qy="0" qz="{half}"/>'
    )
    (folder / "q.dz").write_text(f"<campath><points>{point(0)}{point(1)}</points></campath>")
    forward = load_campaths(tmp_path, "de_nuke")[0].keyframes[0].forward
    assert abs(forward[1] - 1) < 1e-6  # euler said straight ahead (+x); quaternion says +y


def test_pose_interpolates_and_is_none_outside(tmp_path):
    write_oriented(tmp_path / "Nuke", "m", [(0, 0, 0, 0, 0, 0), (10, 100, 0, 0, 0, 0)])
    path = load_campaths(tmp_path, "de_nuke")[0]
    assert pose_at(path, 5)[0][0] == 50
    assert pose_at(path, -0.1) is None and pose_at(path, 10.1) is None


def test_plan_cuts_in_during_down_time_only(tmp_path):
    path = static_path(tmp_path)  # at the origin looking along +x
    target = lambda t: (500.0, 0.0, 0.0)  # noqa: E731  player straight ahead, always
    segments = plan_splice(20.0, path, kill_times=[4.0, 15.0], target_at=target)
    # POV is kept from 1.5 s before to 1.5 s after each kill (at 4.0 s and 15.0 s).
    assert segments == [(0.0, 2.5), (5.6, 13.5), (16.6, 20.0)]


def test_plan_ignores_player_out_of_view_or_too_far_and_short_gaps(tmp_path):
    path = static_path(tmp_path)
    assert plan_splice(10.0, path, [], lambda t: (-500.0, 0.0, 0.0)) == []  # behind the camera
    assert plan_splice(10.0, path, [], lambda t: (500.0, 2000.0, 0.0)) == []  # far off to the side
    assert plan_splice(10.0, path, [], lambda t: (9000.0, 0.0, 0.0)) == []  # too far away
    assert plan_splice(10.0, path, [], lambda t: None) == []  # position unknown
    # A visible stretch shorter than MIN_SEGMENT is dropped.
    brief = lambda t: (500.0, 0.0, 0.0) if 3.0 <= t < 4.0 else None  # noqa: E731
    assert plan_splice(10.0, path, [], brief) == []


def test_plan_is_limited_to_the_path_duration(tmp_path):
    path = static_path(tmp_path, duration=6.0)
    segments = plan_splice(20.0, path, [], lambda t: (500.0, 0.0, 0.0))
    assert segments and max(b for _, b in segments) <= 6.2
    assert coverage(segments) >= 5.5


def test_splice_mode_picks_longest_coverage_and_skips_uncoverable_plays(tmp_path):
    # "near" passes right by the action but faces away; "far" sees the player.
    write_oriented(tmp_path / "Nuke", "near", [(0, 480, 0, 0, 0, 180), (30, 480, 0, 0, 0, 180)])
    write_oriented(tmp_path / "Nuke", "far", [(0, 0, 0, 0, 0, 0), (30, 0, 0, 0, 0, 0)])
    paths = load_campaths(tmp_path, "de_nuke")
    sequence = _sequence(1, 5000, 5000 + 64 * 20)
    from highlights.models import CameraAction

    sequence = type(sequence)(**{**sequence.__dict__, "cameras": [CameraAction(5000, "111")]})
    samples = [
        {"seconds_from_clip_start": float(t), "xyz": {"x": 500.0, "y": 0.0, "z": 0.0}} for t in range(0, 21)
    ]
    spatial = {
        1: {**_spatial(500, 0, 0), "pov_trajectories": [{"steamid": "111", "samples": samples}]},
    }
    result, new_spatial = add_cinematic_sequences([sequence], paths, spatial, 64.0, splice=True)
    assert [s.cinematic_of for s in result] == [None, 1]
    assert result[1].campath.stem == "far"  # coverage beats proximity
    assert result[1].splice_segments and coverage(result[1].splice_segments) > 15
    assert set(new_spatial) == {1, 2}

    # Nothing sees the player: the play stays POV-only (no pointless recording).
    spatial_blind = {1: {**spatial[1], "pov_trajectories": []}}
    result, _ = add_cinematic_sequences([sequence], paths, spatial_blind, 64.0, splice=True)
    assert len(result) == 1


def test_splice_filter_overlays_cinematic_inside_segments_only():
    graph = splice_filter([(0.0, 2.5), (5.5, 13.5)])
    assert "overlay=enable='between(t,0.000,2.500)+between(t,5.500,13.500)'" in graph
    assert graph.startswith("[0:v][1:v]") and graph.endswith("[v]")
