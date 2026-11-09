"""Check loss reporting without starting LingYi, Laya, or Codex."""

import unittest

from engine.LingYi.entitys.gameenvs.utils import UNIT_DESTROYED_TYPE
from unit_loss_monitor import UnitLossMonitor, with_loss_reporting


class FakeAgent:
    def __init__(self, name, side_id):
        self.Name = name
        self.ID = side_id
        self.KeyMsg = {}
        self.attempts = 3
        self.calls = []

    def initial(self, situation):
        self.calls.append("initial")

    def step(self, time_elapse, situation):
        self.calls.append("step")

    def deduction_end(self):
        self.calls.append("end")


class UnitLossMonitorTest(unittest.TestCase):
    def test_reports_each_destroyed_unit_in_the_observation_step(self):
        events = []
        monitor = UnitLossMonitor(lambda event, **details: events.append((event, details)))
        reporting_agent = with_loss_reporting(FakeAgent, monitor)
        red = reporting_agent("红方", "red-id")
        blue = reporting_agent("蓝方", "blue-id")
        red.initial(({}, {}))
        blue.initial(({}, {}))
        red.KeyMsg[UNIT_DESTROYED_TYPE] = [
            {"UnitID": "damaged", "UnitName": "受损飞机", "SideID": "blue-id",
             "UnitClass": "Aircraft", "IsDamaged": True},
            {"UnitID": "missile", "UnitName": "导弹", "SideID": "blue-id",
             "UnitClass": "Weapon", "IsDamaged": False},
            {"UnitID": "jet-1", "UnitName": "战斗机", "SideID": "blue-id",
             "UnitClass": "Aircraft", "IsDamaged": False},
            {"UnitID": "jet-2", "UnitName": "战斗机", "SideID": "blue-id",
             "UnitClass": "Aircraft", "IsDamaged": False},
        ]
        red.step(1200, ({}, {}))
        blue.KeyMsg[str(UNIT_DESTROYED_TYPE)] = red.KeyMsg[UNIT_DESTROYED_TYPE]
        blue.step(1200, ({}, {}))
        red.step(1260, ({}, {}))

        losses = [details for event, details in events if event == "unit_loss"]
        self.assertEqual([item["unit_id"] for item in losses], ["jet-1", "jet-2"])
        self.assertEqual([item["side"] for item in losses], ["蓝方", "蓝方"])
        self.assertEqual([item["simulation_seconds"] for item in losses], [1200, 1200])
        self.assertTrue(all(item["round"] == 3 for item in losses))
        self.assertEqual(red.calls, ["initial", "step", "step"])

    def test_missing_side_unit_is_reported_once_then_confirmed(self):
        events = []
        monitor = UnitLossMonitor(lambda event, **details: events.append((event, details)))
        agent = with_loss_reporting(FakeAgent, monitor)("红方", "red-id")
        unit = {"jet": {"category": "Aircraft", "name": "苏-25 #2"},
                "group": {"category": "Group", "name": "临时编队"}}
        agent.initial((unit, {}))
        agent.step(60, ({"group": unit["group"]}, {}))
        agent.step(120, ({}, {}))
        agent.KeyMsg[UNIT_DESTROYED_TYPE] = [
            {"UnitID": "jet", "UnitName": "苏-25 #2", "SideID": "red-id",
             "UnitClass": "Aircraft", "IsDamaged": False}
        ]
        agent.step(180, ({}, {}))
        agent.step(240, ({}, {}))

        self.assertEqual([event for event, _ in events], ["unit_loss", "unit_loss_confirmed"])
        self.assertEqual([details["status"] for _, details in events],
                         ["unconfirmed", "confirmed"])
        self.assertEqual([details["simulation_seconds"] for _, details in events], [60, 180])
        self.assertTrue(all(details["unit_id"] == "jet" for _, details in events))

    def test_reappearance_retracts_an_unconfirmed_loss(self):
        events = []
        monitor = UnitLossMonitor(lambda event, **details: events.append((event, details)))
        agent = with_loss_reporting(FakeAgent, monitor)("红方", "red-id")
        unit = {"ship": {"category": "Ship", "name": "护卫舰"}}
        agent.initial((unit, {}))
        agent.step(60, ({}, {}))
        agent.step(120, (unit, {}))
        self.assertEqual([event for event, _ in events], ["unit_loss", "unit_loss_retracted"])


if __name__ == "__main__":
    unittest.main()
