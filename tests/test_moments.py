from conftest import make_kill

from highlights.analysis.moments import cluster_moments


def test_kills_within_gap_form_one_moment(tickrate):
    kills = [make_kill(1000), make_kill(1500), make_kill(2000)]
    moments = cluster_moments(kills, tickrate, gap_seconds=20)
    assert len(moments) == 1
    assert moments[0].kill_count == 3


def test_large_gap_splits_moments(tickrate):
    # 20s at 64 ticks = 1280 ticks; 3000 - 1500 = 1500 > 1280
    kills = [make_kill(1000), make_kill(1500), make_kill(3000)]
    moments = cluster_moments(kills, tickrate, gap_seconds=20)
    assert [m.kill_count for m in moments] == [2, 1]


def test_players_and_rounds_are_separate(tickrate):
    kills = [
        make_kill(1000, attacker="111"),
        make_kill(1010, attacker="333"),
        make_kill(1020, round_index=1, attacker="111"),
    ]
    moments = cluster_moments(kills, tickrate)
    assert len(moments) == 3


def test_moments_sorted_by_first_tick(tickrate):
    kills = [make_kill(5000, attacker="111"), make_kill(1000, attacker="333")]
    moments = cluster_moments(kills, tickrate)
    assert [m.first_tick for m in moments] == [1000, 5000]
