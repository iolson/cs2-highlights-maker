from conftest import make_demo, make_kill, make_round

from highlights.models import Moment


def test_checklist_lists_what_to_look_for():
    from highlights.analysis.selection import build_sequences
    from highlights.config import Config
    from highlights.ingame_check import CheckPlan, checklist

    kills = [make_kill(5000), make_kill(5200)]
    demo = make_demo(kills, [make_round(0, 0, 20000)])
    moment = Moment("111", "player111", "CT", 0, kills)
    config = Config()
    config.recording.player_names = {"222": "Victim"}
    config.recording.extra_commands = ["r_dof_override 1"]
    [pov] = build_sequences([moment], demo, config.selection, config.recording, merge=False)
    text = checklist(demo, config, CheckPlan(moment, [pov], pov, notes=["No cam paths"]), 0.12)
    assert "01-pov.mp4" in text and "player111" in text and "+0.12 s" in text
    assert "player222 → Victim" in text and "r_dof_override 1" in text
    assert "02-campath" not in text and "- No cam paths" in text
