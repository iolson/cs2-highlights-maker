from pathlib import Path

import click.testing
import pytest
from conftest import make_demo, make_kill, make_round

import highlights.cli as cli
from highlights.config import Config


def _demo_for(name: str, kills, rounds, map_name):
    demo = make_demo(kills, rounds)
    demo.path = Path(name)
    demo.map_name = map_name
    return demo


def _demos():
    # make_kill prefixes names with "player". Expected scores:
    # alice 3.4 (round 0, two headshots) > carl 3.2 (round 0) > bob 2.8
    # (final round, lost, 0.85 * 1.3 = 1.105).
    alice, bob, carl = "alice", "bob", "carl"
    v1, v2 = "v1", "v2"
    rounds_a = [make_round(0, 0, 100000, winner="CT"), make_round(1, 100000, 200000, winner="T")]
    kills_a = [
        make_kill(2000, round_index=0, attacker=alice, victim=v1, headshot=True),
        make_kill(3000, round_index=0, attacker=alice, victim=v2, headshot=True),
        make_kill(110000, round_index=1, attacker=bob, victim=v1),
        make_kill(111000, round_index=1, attacker=bob, victim=v2),
    ]
    rounds_b = [make_round(0, 0, 100000, winner="T")]
    kills_b = [
        make_kill(2000, round_index=0, attacker=carl, victim=v1),
        make_kill(3000, round_index=0, attacker=carl, victim=v2),
    ]
    return (
        {
            "a.dem": _demo_for("a.dem", kills_a, rounds_a, "de_mirage"),
            "b.dem": _demo_for("b.dem", kills_b, rounds_b, "de_inferno"),
        },
        (alice, bob, carl),
    )


@pytest.fixture
def runner_and_demos(tmp_path, monkeypatch):
    demos, names = _demos()
    monkeypatch.setattr(cli, "parse_demo", lambda path: demos[Path(path).name])
    monkeypatch.setattr(cli, "ensure_dem_file", lambda path, work_dir: path)
    monkeypatch.setattr(cli, "load_config", lambda: Config())
    monkeypatch.setattr(cli, "_resolve_work_dir", lambda config, demo_path: tmp_path)
    files = []
    for name in demos:
        dem_file = tmp_path / name
        dem_file.write_bytes(b"")
        files.append(dem_file)
    return click.testing.CliRunner(), files, names


def _top(runner, files, *extra):
    result = runner.invoke(cli.main, ["top", *[str(f) for f in files], *extra])
    return result


def _table_rows(output):
    """Table rows as (rank, demo, map, round, player, highlight, score) tuples."""
    rows = []
    for line in output.splitlines():
        parts = [p for p in (c.strip() for c in line.split("│")) if p]
        if len(parts) == 7 and parts[0].isdigit():
            rows.append(tuple(parts))
    return rows


def _players(output):
    return [row[4] for row in _table_rows(output)]


def _scores(output):
    return [float(row[6]) for row in _table_rows(output)]


def test_top_pools_moments_across_demos_by_score(runner_and_demos):
    runner, files, (alice, bob, carl) = runner_and_demos
    result = _top(runner, files)
    assert result.exit_code == 0, result.output
    assert _players(result.output) == [f"player{alice}", f"player{carl}", f"player{bob}"]
    assert _scores(result.output) == sorted(_scores(result.output), reverse=True)
    assert "de_mirage" in result.output and "de_inferno" in result.output


def test_top_option_keeps_single_best(runner_and_demos):
    runner, files, (alice, bob, carl) = runner_and_demos
    result = _top(runner, files, "--top", "1")
    assert result.exit_code == 0, result.output
    rows = _table_rows(result.output)
    assert len(rows) == 1
    assert rows[0][0] == "1" and rows[0][4] == f"player{alice}"


def test_top_players_skips_demo_without_player(runner_and_demos):
    runner, files, (alice, bob, carl) = runner_and_demos
    result = _top(runner, files, "--players", "alice")
    assert result.exit_code == 0, result.output
    assert _players(result.output) == [f"player{alice}"]
    assert "b.dem" in result.output  # warning naming the skipped demo
    assert "Warning" in result.output


def test_top_unknown_player_fails(runner_and_demos):
    runner, files, _ = runner_and_demos
    result = _top(runner, files, "--players", "nobody")
    assert result.exit_code != 0
    assert "nobody" in result.output


def test_top_writes_no_files(runner_and_demos, tmp_path):
    runner, files, _ = runner_and_demos
    result = _top(runner, files)
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.dem", "b.dem"]


def test_default_group_still_dispatches_to_run(tmp_path, monkeypatch):
    # A double kill in a won round qualifies with the default min_kills=2.
    alice, v1, v2 = "alice", "v1", "v2"
    rounds = [make_round(0, 0, 100000, winner="CT")]
    kills = [
        make_kill(2000, round_index=0, attacker=alice, victim=v1),
        make_kill(3000, round_index=0, attacker=alice, victim=v2),
    ]
    demo = _demo_for("m.dem", kills, rounds, "de_test")
    monkeypatch.setattr(cli, "parse_demo", lambda path: demo)
    monkeypatch.setattr(cli, "ensure_dem_file", lambda path, work_dir: path)
    monkeypatch.setattr(cli, "load_config", lambda: Config())
    monkeypatch.setattr(cli, "_resolve_work_dir", lambda config, demo_path: tmp_path)
    dem_file = tmp_path / "m.dem"
    dem_file.write_bytes(b"")
    result = click.testing.CliRunner().invoke(
        cli.main,
        [str(dem_file), "--json-only", "-o", str(tmp_path / "out.mp4")],
    )
    assert result.exit_code == 0, result.output


def test_top_max_duration_limits_total_length(runner_and_demos):
    runner, files, _ = runner_and_demos
    everything = _table_rows(_top(runner, files).output)
    assert len(everything) >= 2
    tiny = _top(runner, files, "--max-duration", "1")
    assert tiny.exit_code != 0 and "No clip fits" in tiny.output
    full = _top(runner, files, "--max-duration", "100000")
    assert full.exit_code == 0, full.output
    assert len(_table_rows(full.output)) == len(everything)
    assert "of 100000s" in full.output
