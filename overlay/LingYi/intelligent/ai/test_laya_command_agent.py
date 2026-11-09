"""Contract and simulated engine checks; no Laya or Plat process is started."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from engine.LingYi.entitys.global_util import MissionPatrolType, MissionStrikeType
from laya_command_agent import apply_settings, make_agent
from laya_mission_protocol import (build_pair_group_request, generate_candidates,
                                   select_laya_action, validate_plan)
from codex_mission_planner import CodexMissionPlanner, summarize_situation
from scenario_brief import read_scenario_brief


UNITS = {
    "fighter_a": {"guid": "fighter_a", "category": "Aircraft", "type": 2001, "dbid": 5001,
                  "name": "苏-27 #1", "lat": 54.0, "lon": 20.0, "mission": ""},
    "fighter_b": {"guid": "fighter_b", "category": "Aircraft", "type": 2001, "dbid": 5001,
                  "name": "苏-27 #2", "lat": 54.1, "lon": 20.1, "mission": ""},
    "base": {"guid": "base", "category": "Facility", "name": "北方基地",
             "lat": 54.2, "lon": 20.2, "mission": ""},
}
CONTACTS = {"hostile": {"Name": "敌舰", "Type": 2, "Side": "蓝方", "Lat": 54.3, "Lon": 20.3},
            "friend": {"Name": "友舰", "Type": 2, "Side": "红方", "Lat": 54.4, "Lon": 20.4}}
AREA = {"id": "north", "center": "selected_units", "half_span_deg": 0.1}
CODEX_THREAD_ID = "12345678-1234-4234-8234-123456789abc"
CODEX_STARTED = json.dumps({"type": "thread.started", "thread_id": CODEX_THREAD_ID})


def plan_for(family):
    task = {"family": family, "units": {"category": "Aircraft", "aircraft_type": "fighter",
                                           "counts": [2], "unassigned_only": True}, "settings": {}}
    if family == "patrol":
        task.update(subtype="air", areas=[AREA])
    elif family == "strike":
        task.update(subtype="ship", target={"type": "surface"})
    elif family == "ferry":
        task["destination"] = {"unit_ids": ["base"]}
    else:
        task["areas"] = [AREA]
    return {"version": 2, "plan_id": "mission1", "side": "红方", "objective": "测试任务",
            "valid_until_sim_seconds": 600, "task": task}


class FakeReply:
    def __init__(self, code=0):
        self.code = code


class FakeMission:
    ID = "mission-guid"

    def __init__(self, fail_second=False):
        self.calls = []
        self.fail_second = fail_second

    def __getattr__(self, name):
        def invoke(*args):
            self.calls.append((name, args))
            if name == "assign_unit" and self.fail_second and args[0] == "fighter_b":
                return FakeReply(1)
            return FakeReply()
        return invoke


class MissionBridgeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch("codex_mission_planner.DEFAULT_ROOT", Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_aircraft_group_request_uses_compact_loadout_ids(self):
        units = {"jet%d" % i: dict(UNITS["fighter_a"],
                                    name="测试飞机 #" + str(i),
                                    loadout={"loadout": {"DBID": 6693, "Name": "很长的挂载名称" * 30}})
                 for i in range(6)}
        candidate = {"id": "patrol_6_zone1_g0", "unit_ids": list(units),
                     "pair_ids": [["jet0", "jet1"], ["jet2", "jet3"], ["jet4", "jet5"]],
                     "reassigned_from": {}, "protected_reassigned": 0,
                     "loadout_id": None}
        request = build_pair_group_request(plan_for("patrol"), "红方", 0,
                                           (units, {}), [candidate], 6)
        self.assertEqual(len(request["state"]["units"]), 6)
        self.assertTrue(all(row["loadout_id"] == 6693 for row in request["state"]["units"]))
        self.assertNotIn("很长的挂载名称", json.dumps(request, ensure_ascii=False))

    def test_laya_selects_aircraft_count_then_specific_pairs(self):
        plan = plan_for("patrol")
        plan["task"]["units"].update(counts=[2, 4, 6], pair_selection=True,
                                      allow_reassignment=False, reserve_pairs=1)
        units = {"jet%d" % i: {"guid": "jet%d" % i, "category": "Aircraft", "type": 2001,
                               "dbid": 5001,
                               "name": "Jet %d" % i, "lat": 54 + i * .01, "lon": 20,
                               "mission": "old" if i < 2 else "", "airops": {"Con": 1}}
                 for i in range(8)}
        choices, events, missions = [], [], []
        cls = make_agent(plan, "http://unused", lambda event, **data: events.append((event, data)))
        with patch("laya_command_agent.RuleAgentBase.__init__",
                   lambda self, side: setattr(self, "Name", side)):
            agent = cls("红方", {})
        agent.get_unit = lambda guid: units.get(guid)
        agent.get_mission_by_name = lambda name: None
        def create(*args):
            mission = FakeMission()
            mission.ID = "pair_mission_%d" % len(missions)
            missions.append(mission)
            return mission
        agent.create_patrol_mission = create
        def laya_choice(url, body, timeout):
            if body["state"].get("phase") == "aircraft_count":
                ids = list(body["questions"]["next_action"]["criteria"])
                self.assertEqual(set(ids), {"count_2", "count_4", "hold"})
                chosen = "count_4"
            else:
                self.assertEqual(body["state"]["selected_count"], 4)
                chosen = next(key for key in body["questions"]["next_action"]["criteria"]
                              if key != "hold")
                choices.append(body["questions"]["next_action"]["criteria"][chosen])
            return {"answers": {"next_action": {"choice": chosen, "answer_confidence": .9}}}
        with patch("laya_command_agent.query_laya", side_effect=laya_choice) as query:
            agent.step(0, (units, CONTACTS))
        self.assertEqual(query.call_count, 2)
        self.assertIn("预设任务调整0架", choices[0])
        self.assertEqual(len(missions), 2)
        self.assertTrue(all(len([call for call in mission.calls if call[0] == "assign_unit"]) == 2
                            for mission in missions))
        assigned_ids = [call[1][0] for mission in missions for call in mission.calls
                        if call[0] == "assign_unit"]
        self.assertEqual(len(assigned_ids), 4)
        self.assertTrue(any(event == "launch_requested" and data["requested"] == 4
                            for event, data in events))
        observed = {key: dict(value) for key, value in units.items()}
        for mission in missions:
            for call in mission.calls:
                if call[0] == "assign_unit":
                    observed[call[1][0]].update(mission=mission.ID, airops={"Con": 0})
        agent.step(60, (observed, CONTACTS))
        self.assertTrue(any(event == "command_observed" and data["matched"] and
                            len(data["mission_ids"]) == 2 for event, data in events))
        self.assertTrue(any(event == "launch_observed" and data["status"] == "complete"
                            and data["airborne"] == 4 for event, data in events))

    def test_pair_reassignment_requires_explicit_policy(self):
        plan = plan_for("patrol")
        plan["task"]["units"].update(counts=[2, 4], pair_selection=True,
                                      allow_reassignment=False, unassigned_only=False)
        units = {key: dict(value, mission="baseline", airops={"Con": 1})
                 for key, value in UNITS.items() if value["category"] == "Aircraft"}
        self.assertEqual(generate_candidates(plan, (units, CONTACTS), 0), [])
        plan["task"]["units"]["allow_reassignment"] = True
        self.assertTrue(generate_candidates(plan, (units, CONTACTS), 0))
        self.assertEqual(generate_candidates(plan, (units, CONTACTS), 0,
                                             protected_missions={"baseline"}), [])
        plan["task"]["units"]["allow_ai_reassignment"] = True
        self.assertTrue(generate_candidates(plan, (units, CONTACTS), 0,
                                            protected_missions={"baseline"}))
        units["fighter_b"]["airops"] = {"Con": 7}  # Readying: cannot launch yet.
        self.assertEqual(generate_candidates(plan, (units, CONTACTS), 0), [])

    def test_aircraft_pairs_require_same_platform_dbid(self):
        plan = plan_for("patrol")
        plan["task"]["units"].update(pair_selection=True, counts=[2])
        units = {key: dict(value, airops={"Con": 1}) for key, value in UNITS.items()
                 if value["category"] == "Aircraft"}
        self.assertTrue(generate_candidates(plan, (units, CONTACTS), 0))
        units["fighter_b"]["dbid"] = 5002
        self.assertEqual(generate_candidates(plan, (units, CONTACTS), 0), [])
        units["fighter_b"].pop("dbid")
        self.assertEqual(generate_candidates(plan, (units, CONTACTS), 0), [])

    def test_mixed_loadouts_need_explicit_observed_rearm_plan(self):
        plan = plan_for("strike")
        plan["task"]["subtype"] = "land"
        plan["task"]["units"]["pair_selection"] = True
        units = {key: dict(value, airops={"Con": 1},
                           loadout={"x": {"DBID": dbid, "Name": name}})
                 for key, value, dbid, name in (
                     ("fighter_a", UNITS["fighter_a"], 27107, "Land weapon"),
                     ("fighter_b", UNITS["fighter_b"], 27106, "Ship weapon"))}
        self.assertEqual(generate_candidates(plan, (units, CONTACTS), 0), [])
        plan["task"]["loadout"] = {"role": "land", "allowed_ids": [27107], "allow_change": True}
        candidates = generate_candidates(plan, (units, CONTACTS), 0)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["loadout_changes"], ["fighter_b"])
        self.assertEqual(candidates[0]["loadout_id"], 27107)
        plan["task"]["loadout"]["allowed_ids"] = [99999]
        self.assertEqual(generate_candidates(plan, (units, CONTACTS), 0), [])
        plan["task"]["loadout"] = {"role": "ship", "allowed_ids": [27107], "allow_change": True}
        with self.assertRaises(ValueError):
            validate_plan(plan)

    def test_rearm_waits_for_reported_loadout_and_parked_state(self):
        plan = plan_for("ferry")
        plan["task"]["units"].update(pair_selection=True, allow_reassignment=True,
                                      unassigned_only=False)
        plan["task"]["loadout"] = {"role": "any", "allowed_ids": [27107], "allow_change": True}
        units = {key: dict(value, airops={"Con": 1},
                           loadout={"x": {"DBID": dbid, "Name": "Loadout %d" % dbid}})
                 for key, value, dbid in (("fighter_a", UNITS["fighter_a"], 27107),
                                          ("fighter_b", UNITS["fighter_b"], 27106))}
        units["base"] = dict(UNITS["base"])
        for guid in ("fighter_a", "fighter_b"):
            units[guid]["mission"] = "old_mission"
        events, missions, rearmed, withdrawn = [], [], [], []
        cls = make_agent(plan, "http://unused", lambda event, **data: events.append((event, data)))
        with patch("laya_command_agent.RuleAgentBase.__init__",
                   lambda self, side: setattr(self, "Name", side)):
            agent = cls("红方", {})
        class LiveAircraft:
            def __init__(self, unit):
                self.unit = unit
                self.AssignedMission = unit["mission"]
            def get_summary_info(self):
                return self.unit
            def set_loadout(self, dbid):
                rearmed.append(dbid)
                return FakeReply()
            def withdraw_from_mission(self):
                withdrawn.append(self.unit["guid"])
                self.unit["mission"] = ""
                return FakeReply()
        agent.get_unit = lambda guid: LiveAircraft(units[guid]) if guid in units else None
        agent.get_mission_by_name = lambda name: None
        def create(*args):
            mission = FakeMission()
            missions.append(mission)
            return mission
        agent.create_ferry_mission = create
        def select(url, body, timeout):
            choice = ("count_2" if body["state"].get("phase") == "aircraft_count" else
                      next(key for key in body["questions"]["next_action"]["criteria"] if key != "hold"))
            return {"answers": {"next_action": {"choice": choice, "answer_confidence": .9}}}
        with patch("laya_command_agent.query_laya", side_effect=select):
            agent.step(0, (units, CONTACTS))
            self.assertEqual(rearmed, [27107])
            self.assertEqual(set(withdrawn), {"fighter_a", "fighter_b"})
            self.assertEqual(missions, [])
            waiting = copy.deepcopy(units)
            waiting["fighter_b"]["loadout"]["x"]["DBID"] = 27107
            waiting["fighter_b"]["airops"] = {"Con": 7}
            agent.step(60, (waiting, CONTACTS))
            self.assertEqual(missions, [])
            waiting["fighter_b"]["airops"] = {"Con": 1}
            agent.step(120, (waiting, CONTACTS))
        self.assertEqual(len(missions), 1)
        self.assertTrue(any(event == "loadout_ready" for event, _ in events))
        self.assertTrue(any(event == "command_applied" for event, _ in events))

    def test_new_patrol_area_is_not_mistaken_for_duplicate(self):
        units = {key: dict(value, airops={"Con": 1}) for key, value in UNITS.items()}
        events, missions = [], []
        def provider(side, seconds, situation):
            result = plan_for("patrol")
            result["plan_id"] = "turn_%d" % seconds
            result["valid_until_sim_seconds"] = seconds + 600
            result["task"]["units"].update(pair_selection=True, counts=[2],
                                            allow_reassignment=True, allow_ai_reassignment=True,
                                            unassigned_only=False)
            result["task"]["areas"] = [dict(AREA, offset_deg=[seconds / 600, 0])]
            return result
        cls = make_agent(provider, "http://unused",
                         lambda event, **data: events.append((event, data)), max_actions=None)
        with patch("laya_command_agent.RuleAgentBase.__init__",
                   lambda self, side: setattr(self, "Name", side)):
            agent = cls("红方", {})
        agent.get_unit = lambda guid: units.get(guid)
        agent.get_mission_by_name = lambda name: None
        def create(*args):
            mission = FakeMission()
            mission.ID = "mission_%d" % len(missions)
            missions.append(mission)
            return mission
        agent.create_patrol_mission = create
        def select(url, body, timeout):
            choice = ("count_2" if body["state"]["phase"] == "aircraft_count" else
                      next(key for key in body["questions"]["next_action"]["criteria"]
                           if key != "hold"))
            return {"answers": {"next_action": {"choice": choice, "answer_confidence": .9}}}
        with patch("laya_command_agent.query_laya", side_effect=select):
            agent.step(0, (units, CONTACTS))
            for key in ("fighter_a", "fighter_b"):
                units[key]["mission"] = missions[0].ID
            agent.step(600, (units, CONTACTS))
        self.assertEqual(len(missions), 2)
        self.assertEqual(sum(event == "command_applied" for event, _ in events), 2)

    def test_all_seven_families_create_and_assign(self):
        for family in ("patrol", "strike", "support", "ferry", "cargo", "mine", "mine_clear"):
            with self.subTest(family=family):
                plan = plan_for(family)
                events, mission, creation = [], FakeMission(), []
                cls = make_agent(plan, "http://unused", lambda event, **details: events.append((event, details)))
                with patch("laya_command_agent.RuleAgentBase.__init__",
                           lambda self, side: setattr(self, "Name", side)):
                    agent = cls("红方", {})
                agent.get_unit = lambda guid: UNITS.get(guid)
                agent.get_mission_by_name = lambda name: None
                method = "create_%s_mission" % ("mineclear" if family == "mine_clear" else family)
                setattr(agent, method, lambda *args: (creation.append(args), mission)[1])
                candidates = generate_candidates(plan, (UNITS, CONTACTS), 20)
                self.assertEqual(len(candidates), 1)
                chosen = candidates[0]["id"]
                answer = {"answers": {"next_action": {"choice": chosen, "answer_confidence": 0.9}}}
                with patch("laya_command_agent.query_laya", return_value=answer):
                    agent.step(20, (UNITS, CONTACTS))
                self.assertEqual(len(creation), 1)
                self.assertEqual([x[1][0] for x in mission.calls if x[0] == "assign_unit"],
                                 ["fighter_a", "fighter_b"])
                self.assertTrue(any(e == "command_applied" for e, _ in events))
                if family == "strike":
                    self.assertEqual(creation[0][1], MissionStrikeType.SHIP)
                    self.assertIn(("strike_add_target", (["hostile"],)), mission.calls)
                elif family == "patrol":
                    self.assertEqual(creation[0][1], MissionPatrolType.AIR)
                elif family == "ferry":
                    self.assertEqual(creation[0][1], "base")
                else:
                    self.assertEqual(len(creation[0][1]), 4)
                observed = (dict(UNITS), CONTACTS)
                observed[0]["fighter_a"] = dict(UNITS["fighter_a"], mission=mission.ID)
                observed[0]["fighter_b"] = dict(UNITS["fighter_b"], mission=mission.ID)
                agent.step(40, observed)
                self.assertTrue(next(d["matched"] for e, d in events if e == "command_observed"))

    def test_settings_and_partial_failure(self):
        plan = plan_for("strike")
        plan["task"]["settings"] = {"active": True, "flight_size": 2, "preplanned_only": True}
        events, mission = [], FakeMission(fail_second=True)
        cls = make_agent(plan, "http://unused", lambda event, **details: events.append((event, details)))
        with patch("laya_command_agent.RuleAgentBase.__init__",
                   lambda self, side: setattr(self, "Name", side)):
            agent = cls("红方", {})
        agent.get_unit = lambda guid: UNITS.get(guid)
        agent.get_mission_by_name = lambda name: None
        agent.create_strike_mission = lambda *args: mission
        candidate = generate_candidates(plan, (UNITS, CONTACTS), 20)[0]["id"]
        answer = {"answers": {"next_action": {"choice": candidate, "answer_confidence": 0.9}}}
        with patch("laya_command_agent.query_laya", return_value=answer):
            agent.step(20, (UNITS, CONTACTS))
        self.assertTrue(any(e == "command_failed" and d["partial"] and
                            d["assigned_unit_ids"] == ["fighter_a"] for e, d in events))
        self.assertIn(("mission_active", (True,)), mission.calls)
        self.assertIn(("strike_setPreplan", (True,)), mission.calls)
        self.assertTrue(any(e == "strike_setStrikeFlightSize" for e, _ in mission.calls))

    def test_settings_map_to_all_family_methods(self):
        examples = {
            "patrol": {"check_outside_area": False, "flight_size": 2},
            "strike": {"preplanned_only": True},
            "support": {"station_count": 1, "one_third_rule": True},
            "ferry": {"behavior": "one_way", "flight_size": 1},
            "cargo": {"unload_mounts": [{"dbid": 65, "count": 5}]},
            "mine": {"arming_delay": "0:1:0:0", "group_size": 2},
            "mine_clear": {"group_size": 1},
        }
        expected = {
            "patrol": "patrol_checkOPA", "strike": "strike_setPreplan",
            "support": "station_count", "ferry": "set_behavior",
            "cargo": "set_unload_mount", "mine": "set_armingdelay",
            "mine_clear": "set_group_size",
        }
        for family, settings in examples.items():
            with self.subTest(family=family):
                plan = plan_for(family)
                plan["task"]["settings"] = settings
                validate_plan(plan)
                mission = FakeMission()
                apply_settings(mission, family, settings)
                self.assertIn(expected[family], [name for name, _ in mission.calls])

    def test_reject_bad_plan_and_response(self):
        plan = plan_for("patrol")
        self.assertEqual(generate_candidates(plan, (UNITS, CONTACTS), 601), [])
        bad = copy.deepcopy(plan)
        bad["task"]["family"] = "delete"
        with self.assertRaises(ValueError):
            validate_plan(bad)
        bad = copy.deepcopy(plan)
        bad["task"]["units"]["aircraft_type"] = "nonexistent"
        with self.assertRaises(ValueError):
            validate_plan(bad)
        bad = copy.deepcopy(plan)
        bad["task"]["areas"][0] = {"id": "bad", "points": [[91, 0], [91, 1], [90, 1]]}
        with self.assertRaises(ValueError):
            validate_plan(bad)
        response = {"answers": {"next_action": {"choice": "foreign", "answer_confidence": 0.9}}}
        with self.assertRaises(ValueError):
            select_laya_action(response, {"patrol_2_north"}, 0)
        response["answers"]["next_action"].update(choice="patrol_2_north", answer_confidence=0.2)
        with self.assertRaises(ValueError):
            select_laya_action(response, {"patrol_2_north"}, 0.5)
        response["usage"] = {"truncated": True}
        with self.assertRaises(ValueError):
            select_laya_action(response, {"patrol_2_north"}, 0)

    def test_no_target_or_units_means_no_candidate(self):
        strike = plan_for("strike")
        self.assertEqual(generate_candidates(strike, (UNITS, {"friend": CONTACTS["friend"]}), 0), [])
        units = {"fighter_a": dict(UNITS["fighter_a"], mission="busy"), "base": UNITS["base"]}
        self.assertEqual(generate_candidates(plan_for("ferry"), (units, CONTACTS), 0), [])

    def test_expired_or_wrong_side_does_not_query_laya(self):
        plan = plan_for("patrol")
        cls = make_agent(plan, "http://unused", lambda *args, **kwargs: None)
        with patch("laya_command_agent.RuleAgentBase.__init__",
                   lambda self, side: setattr(self, "Name", side)):
            agent = cls("红方", {})
        with patch("laya_command_agent.query_laya") as query:
            agent.step(601, (UNITS, CONTACTS))
            query.assert_not_called()
        with patch("laya_command_agent.RuleAgentBase.__init__",
                   lambda self, side: setattr(self, "Name", side)):
            agent = cls("蓝方", {})
        with patch("laya_command_agent.query_laya") as query:
            agent.step(20, (UNITS, CONTACTS))
            query.assert_not_called()

    def test_live_codex_plan_provider_reaches_laya(self):
        plan = plan_for("patrol")
        events, mission = [], FakeMission()
        called = []
        def provider(side, seconds, situation):
            called.append((side, seconds))
            return plan
        cls = make_agent(provider, "http://unused", lambda event, **details: events.append((event, details)))
        with patch("laya_command_agent.RuleAgentBase.__init__",
                   lambda self, side: setattr(self, "Name", side)):
            agent = cls("红方", {})
        agent.get_unit = lambda guid: UNITS.get(guid)
        agent.get_mission_by_name = lambda name: None
        agent.create_patrol_mission = lambda *args: mission
        answer = {"answers": {"next_action": {"choice": "patrol_2_north", "answer_confidence": 0.9}}}
        with patch("laya_command_agent.query_laya", return_value=answer):
            agent.step(20, (UNITS, CONTACTS))
        self.assertEqual(called, [("红方", 20)])
        self.assertTrue(any(e == "codex_plan_ready" for e, _ in events))
        self.assertTrue(any(e == "command_applied" for e, _ in events))

    def test_ooda_schedule_replans_and_avoids_duplicate_assignment(self):
        events, called, mission = [], [], FakeMission()
        def provider(side, seconds, situation):
            called.append(seconds)
            if seconds == 620:
                raise RuntimeError("temporary Codex failure")
            result = plan_for("patrol")
            result["plan_id"] = "round_%d" % seconds
            result["valid_until_sim_seconds"] = seconds + 600
            return result
        cls = make_agent(provider, "http://unused",
                         lambda event, **details: events.append((event, details)),
                         max_actions=None, replan_interval=600)
        with patch("laya_command_agent.RuleAgentBase.__init__",
                   lambda self, side: setattr(self, "Name", side)):
            agent = cls("红方", {})
        agent.get_unit = lambda guid: UNITS.get(guid)
        agent.get_mission_by_name = lambda name: None
        agent.create_patrol_mission = lambda *args: mission
        answer = {"answers": {"next_action": {"choice": "patrol_2_north", "answer_confidence": 0.9}}}
        with patch("laya_command_agent.query_laya", return_value=answer) as query:
            agent.step(0, (UNITS, CONTACTS))
            observed = (dict(UNITS), CONTACTS)
            for guid in ("fighter_a", "fighter_b"):
                observed[0][guid] = dict(UNITS[guid], mission=mission.ID)
            for second in (590, 620, 1190, 1210, 1500):
                agent.step(second, observed)
        self.assertEqual(called, [0, 620, 1210])
        self.assertEqual(query.call_count, 1)
        self.assertEqual(sum(event == "codex_plan_failed" for event, _ in events), 1)
        self.assertEqual(sum(event == "codex_plan_ready" for event, _ in events), 2)

    def test_scenario_brief_is_readable(self):
        import base64
        html = '<html><body><p>红方主要任务</p><p>推演150分钟</p></body></html>'
        encoded = base64.b64encode(html.encode('utf-8')).decode('ascii')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.scen'
            path.write_text('<ScenContainer><ScenDescription>' + encoded +
                            '</ScenDescription></ScenContainer>', encoding='utf-8')
            brief = read_scenario_brief(path)
        self.assertIn("红方", brief)
        self.assertIn("主要任务", brief)
        self.assertIn("150分钟", brief)
        self.assertNotIn("<html", brief)

    def test_codex_cli_output_is_validated_against_live_situation(self):
        summary = summarize_situation("红方", "test", 20, (UNITS, CONTACTS))
        self.assertEqual(summary["unit_counts"]["Aircraft"], 2)
        with tempfile.TemporaryDirectory() as directory:
            events = []
            with patch("codex_mission_planner.shutil.which", return_value="codex.cmd"):
                planner = CodexMissionPlanner("test", "红方", directory,
                                             lambda event, **details: events.append((event, details)))
            def fake_codex(command, **kwargs):
                output = Path(command[command.index("--output-last-message") + 1])
                proposed = plan_for("patrol")
                proposed.pop("version")
                proposed.pop("plan_id")
                proposed.pop("side")
                output.write_text(json.dumps({"plan_json": json.dumps(proposed)}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=CODEX_STARTED)
            with patch("codex_mission_planner.run_codex", side_effect=fake_codex):
                accepted = planner("红方", 20, (UNITS, CONTACTS))
            self.assertEqual(accepted["side"], "红方")
            self.assertEqual(accepted["scenario"], "test")
            self.assertTrue(any(e == "codex_plan_accepted" for e, _ in events))
            self.assertTrue((Path(directory) / "codex_plan_round_001_accepted.json").is_file())
            self.assertEqual(planner.session_id, CODEX_THREAD_ID)
            self.assertEqual(json.loads((Path(directory) / "codex_session.json").read_text(encoding="utf-8"))["thread_id"],
                             CODEX_THREAD_ID)

    def test_codex_invalid_plan_retries_then_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            with patch("codex_mission_planner.shutil.which", return_value="codex.cmd"):
                planner = CodexMissionPlanner("test", "红方", directory,
                                             lambda event, **details: events.append((event, details)))
            def fake_codex(command, **kwargs):
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"plan_json": "{}"}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=CODEX_STARTED)
            with patch("codex_mission_planner.run_codex", side_effect=fake_codex) as invoke:
                with self.assertRaisesRegex(ValueError, "after two attempts"):
                    planner("红方", 20, (UNITS, CONTACTS))
            self.assertEqual(invoke.call_count, 2)
            self.assertNotIn("--ephemeral", invoke.call_args_list[0].args[0])
            self.assertIn("resume", invoke.call_args_list[1].args[0])
            self.assertIn(CODEX_THREAD_ID, invoke.call_args_list[1].args[0])
            self.assertIn("上一份计划未通过本地校验", invoke.call_args_list[1].kwargs["input"])
            self.assertEqual(sum(e == "codex_plan_rejected" for e, _ in events), 2)

    def test_codex_rounds_resume_one_session_with_incremental_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("codex_mission_planner.shutil.which", return_value="codex.cmd"):
                planner = CodexMissionPlanner("test", "红方", directory,
                                             lambda event, **details: None,
                                             mission_brief="FIRST ROUND BRIEF")
            calls = []
            def fake_codex(command, **kwargs):
                calls.append((command, kwargs["input"]))
                self.assertIn("notify=[]", command)
                output = Path(command[command.index("--output-last-message") + 1])
                proposed = plan_for("patrol")
                proposed["valid_until_sim_seconds"] = 1800
                for key in ("version", "plan_id", "side"):
                    proposed.pop(key)
                output.write_text(json.dumps({"plan_json": json.dumps(proposed)}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=CODEX_STARTED if len(calls) == 1 else "")
            with patch("codex_mission_planner.run_codex", side_effect=fake_codex):
                planner("红方", 0, (UNITS, CONTACTS))
                planner.set_context({"last_result": {"event": "launch_observed", "airborne": 2}})
                planner("红方", 600, (UNITS, CONTACTS))
            self.assertIn("FIRST ROUND BRIEF", calls[0][1])
            self.assertNotIn("FIRST ROUND BRIEF", calls[1][1])
            self.assertIn('"airborne":2', calls[1][1])
            self.assertIn("resume", calls[1][0])
            self.assertIn(CODEX_THREAD_ID, calls[1][0])

    def test_codex_requires_a_resumable_session(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("codex_mission_planner.shutil.which", return_value="codex.cmd"):
                planner = CodexMissionPlanner("test", "红方", directory,
                                             lambda event, **details: None)
            with patch("codex_mission_planner.run_codex",
                       return_value=SimpleNamespace(returncode=0, stdout="")) as invoke:
                with self.assertRaisesRegex(RuntimeError, "resumable thread ID"):
                    planner("红方", 0, (UNITS, CONTACTS))
            self.assertEqual(invoke.call_count, 1)
            self.assertIsNone(planner.session_id)
            self.assertFalse((Path(directory) / "codex_session.json").exists())

    def test_codex_resume_failure_does_not_start_a_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("codex_mission_planner.shutil.which", return_value="codex.cmd"):
                planner = CodexMissionPlanner("test", "红方", directory,
                                             lambda event, **details: None)
            planner.session_id = CODEX_THREAD_ID
            with patch("codex_mission_planner.run_codex",
                       return_value=SimpleNamespace(returncode=1, stdout="")) as invoke:
                with self.assertRaisesRegex(RuntimeError, "exited with code 1"):
                    planner("红方", 600, (UNITS, CONTACTS))
            self.assertEqual(invoke.call_count, 1)
            self.assertIn("resume", invoke.call_args.args[0])
            self.assertIn(CODEX_THREAD_ID, invoke.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
