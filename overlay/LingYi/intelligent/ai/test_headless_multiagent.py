"""Configuration checks for multiple agents; starts neither Laya nor Plat."""

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from headless_scenario_run import configure_rule_agents, selected_sides, side_brief_paths


class MultiAgentConfigurationTest(unittest.TestCase):
    def test_two_sides_get_separate_codex_agents_and_one_laya_url(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            red_brief = run_dir / "red.txt"
            blue_brief = run_dir / "blue.txt"
            red_brief.write_text("红方任务", encoding="utf-8")
            blue_brief.write_text("蓝方任务", encoding="utf-8")
            args = SimpleNamespace(scenario="test", codex_plan=True, laya_plan=None,
                                   mission_brief=None, codex_timeout=180, codex_model=None,
                                   laya_url="http://127.0.0.1:18080", laya_timeout=30,
                                   laya_max_actions=None, codex_interval=600)
            events = []
            agent_classes = [type("RedAgent", (), {}), type("BlueAgent", (), {})]
            with patch("headless_scenario_run.record",
                       side_effect=lambda event, **details: events.append((event, details))), \
                 patch("codex_mission_planner.CodexMissionPlanner") as planner, \
                 patch("laya_command_agent.make_agent", side_effect=agent_classes) as make_agent:
                registry = configure_rule_agents(
                    args, ["红方", "蓝方"], run_dir, run_dir / "unused.scen",
                    {"红方": red_brief, "蓝方": blue_brief})
                make_agent.call_args_list[0].args[2]("probe", value=1)
                make_agent.call_args_list[1].args[2]("probe", value=2)
            self.assertEqual(set(registry), {"红方", "蓝方"})
            self.assertTrue(issubclass(registry["红方"]["headless_laya"]["class"], agent_classes[0]))
            self.assertTrue(issubclass(registry["蓝方"]["headless_laya"]["class"], agent_classes[1]))
            self.assertEqual([call.args[1] for call in planner.call_args_list], ["红方", "蓝方"])
            self.assertEqual([call.args[2] for call in planner.call_args_list],
                             [run_dir / "side_01", run_dir / "side_02"])
            self.assertEqual([call.kwargs["mission_brief"] for call in planner.call_args_list],
                             ["红方任务", "蓝方任务"])
            self.assertEqual([call.args[1] for call in make_agent.call_args_list],
                             [args.laya_url, args.laya_url])
            self.assertEqual([(event, details["side"]) for event, details in events[-2:]],
                             [("probe", "红方"), ("probe", "蓝方")])

    def test_duplicate_sides_and_foreign_brief_are_rejected(self):
        parser = argparse.ArgumentParser()
        args = SimpleNamespace(sides=["红方", "红方"], side="红方", laya_plan=None)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                selected_sides(args, parser)
            with self.assertRaises(SystemExit):
                side_brief_paths(["蓝方=blue.txt"], ["红方"], parser)


if __name__ == "__main__":
    unittest.main()
