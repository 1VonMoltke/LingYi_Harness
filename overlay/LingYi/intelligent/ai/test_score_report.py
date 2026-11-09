"""Score reconciliation checks using replay records, without launching the engine."""

import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from engine.LingYi.entitys.gameenvs.common import Struct_pb2
from score_report import build_score_report, save_score_report


ROOM = "12345678-1234-4234-8234-123456789abc"


class ScoreReportTest(unittest.TestCase):
    def make_run(self, root, *, final=True, duplicate=False, truncated=False, gap=0):
        logs = root / "Logs" / ("wargame-py#" + ROOM)
        logs.mkdir(parents=True)
        state = {"RoomID": ROOM, "ScenarioName": "test.scen", "StartTime": 1000000,
                 "Currentime": 1000000, "State": 3, "TotalScore": {"red": 10, "blue": -10}}
        initial = json.dumps(state)
        state.update(Currentime=1060000, State=4 if final else 3,
                     TotalScore={"red": 110 + gap, "blue": -110})
        (logs / "Plat.log").write_text("prefix " + initial + "\nprefix " + json.dumps(state) + "\n", encoding="utf-8")
        replay = root / "Recordings" / "NORMAL" / "date" / ROOM / "scenario" / "start"
        replay.mkdir(parents=True)
        (replay / "simulation.ini").write_text(
            "[Topic]\nSITUATION_DYNAMIC_red=2\nSITUATION_DYNAMIC_blue=4\n"
            "[Scenario]\nOppositon=红方,red,0;蓝方,blue,0;\n", encoding="utf-8")
        rows = []
        for side, topic, points in (("red", 2, 100), ("blue", 4, -100)):
            message = Struct_pb2.THScoreChangeReason(Time=1060000, Name="登陆得分", ID="event1",
                                                    Type=4, Para1="登陆舰一号", PointChange=points)
            data = zlib.compress(message.SerializeToString())
            block = struct.pack("<I", len(data)) + data
            (replay / ("SITUATION_DYNAMIC_" + side + ".dat")).write_bytes(
                bytes(32) + (block[:8] if truncated else block))
            row = "0,60000,1,1,%d,136,32,%d\n" % (topic, len(block))
            rows.append(row)
            if duplicate:
                rows.append(row)
        (replay / "display.index").write_text("".join(rows), encoding="ascii")
        return replay

    def test_totals_sources_and_initial_score_reconcile_without_duplicate_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_run(root, duplicate=True)
            report = build_score_report(root, ROOM, "test", "scenario_complete")
            self.assertTrue(report["final"])
            self.assertEqual([(side["side"], side["total"]) for side in report["sides"]],
                             [("红方", 110), ("蓝方", -110)])
            for side in report["sides"]:
                self.assertEqual(side["unattributed"], 0)
                self.assertEqual(side["sources"][0]["count"], 1)
                self.assertEqual(side["details"][0]["related_unit_1"], "登陆舰一号")
                self.assertEqual(side["details"][0]["simulation_seconds"], 60)
            report = save_score_report(report, root)
            markdown = Path(report["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("00:01:00", markdown)
            self.assertIn("登陆得分", markdown)
            self.assertTrue(Path(report["json_path"]).is_file())

    def test_partial_run_and_unexplained_score_difference_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_run(root, gap=7)
            report = build_score_report(root, ROOM, "test", "wall_timeout")
            self.assertFalse(report["final"])
            self.assertEqual(report["sides"][0]["unattributed"], 7)

    def test_engine_must_confirm_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_run(root, final=False)
            self.assertFalse(build_score_report(root, ROOM, "test", "scenario_complete")["final"])

    def test_incomplete_replay_still_reports_authoritative_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_run(root, truncated=True)
            report = build_score_report(root, ROOM, "test", "scenario_complete")
            self.assertEqual(report["sides"][0]["total"], 110)
            self.assertEqual(report["sides"][0]["unattributed"], 100)
            self.assertTrue(report["notes"])

    def test_distinct_records_with_same_time_and_event_are_not_collapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay = self.make_run(root)
            path = replay / "SITUATION_DYNAMIC_red.dat"
            data = path.read_bytes()
            block = data[32:]
            path.write_bytes(data + block)
            with (replay / "display.index").open("a", encoding="ascii") as stream:
                stream.write("0,60000,1,1,2,136,%d,%d\n" % (len(data), len(block)))
            side = build_score_report(root, ROOM, "test", "scenario_complete")["sides"][0]
            self.assertEqual(side["sources"][0]["count"], 2)
            self.assertEqual(side["detail_total"], 200)
            self.assertEqual(side["unattributed"], -100)

    def test_other_room_or_scenario_is_never_used_as_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_run(root)
            with self.assertRaises(ValueError):
                build_score_report(root, ROOM, "different", "scenario_complete")
            with self.assertRaises(ValueError):
                build_score_report(root, "aaaaaaaa-1234-4234-8234-123456789abc", "test", "scenario_complete")


if __name__ == "__main__":
    unittest.main()
