"""Turn Laya choices into the seven whitelisted LingYi mission types."""

import json
import math

from engine.LingYi.entitys.agent_base import RuleAgentBase
from engine.LingYi.entitys.global_util import FerryBehavior, FlightSize, GroupSize
from laya_mission_protocol import (PATROL_TYPES, STRIKE_TYPES, build_laya_request,
                                   build_pair_count_request, build_pair_group_request,
                                   air_availability,
                                   current_loadout, unit_condition,
                                   generate_candidates, query_laya, select_laya_action,
                                   validate_plan)

FLIGHT_SIZES = {1: FlightSize.One, 2: FlightSize.Two, 3: FlightSize.Three,
                4: FlightSize.Four, 6: FlightSize.Six}
GROUP_SIZES = {1: GroupSize.Single, 2: GroupSize.Double, 3: GroupSize.Three,
               4: GroupSize.Four, 6: GroupSize.Six}
FERRY_BEHAVIORS = {"one_way": FerryBehavior.OneWay,
                   "cycle": FerryBehavior.Cycle, "random": FerryBehavior.Random}


def _checked(reply, action):
    code = getattr(reply, "code", None)
    if code != 0:
        raise RuntimeError("%s reply code %r" % (action, code))


def create_selected_mission(agent, selected):
    """Translate only bound candidates into engine calls."""
    family, name = selected["family"], selected["mission_name"]
    if agent.get_mission_by_name(name) is not None:
        raise RuntimeError("mission name already exists")
    if family == "patrol":
        mission = agent.create_patrol_mission(name, PATROL_TYPES[selected["subtype"]], selected["points"])
    elif family == "strike":
        mission = agent.create_strike_mission(name, STRIKE_TYPES[selected["subtype"]])
    elif family == "support":
        mission = agent.create_support_mission(name, selected["points"])
    elif family == "ferry":
        mission = agent.create_ferry_mission(name, selected["destination_id"])
    elif family == "cargo":
        mission = agent.create_cargo_mission(name, selected["points"])
    elif family == "mine":
        mission = agent.create_mine_mission(name, selected["points"])
    elif family == "mine_clear":
        mission = agent.create_mineclear_mission(name, selected["points"])
    else:
        raise ValueError("unsupported mission family")
    if mission is None:
        raise RuntimeError("engine did not create mission")
    return mission


def apply_settings(mission, family, settings):
    """Explicit method mapping prevents arbitrary engine calls."""
    methods = {"active": "mission_active", "start_time": "set_start", "end_time": "set_end"}
    methods.update({
        "patrol": {"one_third_rule": "set_one_third_rule", "flight_size": "patrol_flight_size",
                   "check_outside_area": "patrol_checkOPA"},
        "strike": {"preplanned_only": "strike_setPreplan", "flight_size": "strike_setStrikeFlightSize"},
        "support": {"one_third_rule": "set_one_third_rule", "flight_size": "support_flightSize",
                    "station_count": "station_count"},
        "ferry": {"behavior": "set_behavior", "flight_size": "set_flight_size"},
        "cargo": {},
        "mine": {"one_third_rule": "set_one_third_rule", "arming_delay": "set_armingdelay",
                 "group_size": "set_group_size"},
        "mine_clear": {"one_third_rule": "set_one_third_rule", "group_size": "set_group_size"},
    }[family])
    for key, value in settings.items():
        if key == "unload_mounts":
            for mount in value:
                _checked(mission.set_unload_mount(mount["dbid"], mount["count"]), key)
            continue
        if key in ("flight_size", "group_size"):
            value = (FLIGHT_SIZES if key == "flight_size" else GROUP_SIZES)[value]
        elif key == "behavior":
            value = FERRY_BEHAVIORS[value]
        _checked(getattr(mission, methods[key])(value), key)


def make_agent(plan, url, record, timeout=30, max_actions=1, replan_interval=600):
    """Build the class expected by LingYi's rule-agent registry."""
    if not callable(plan):
        validate_plan(plan)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Laya timeout must be positive")
    if max_actions is not None and (type(max_actions) is not int or max_actions < 1):
        raise ValueError("max_actions must be positive")
    if max_actions is None and not callable(plan):
        raise ValueError("static plans need an action limit")
    if type(replan_interval) is not int or replan_interval < 1:
        raise ValueError("replan_interval must be a positive integer")
    dynamic = callable(plan)
    def checked_confidence(current_plan):
        value = current_plan.get("min_confidence", 0)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("min_confidence outside [0,1]")
        return value

    if not callable(plan):
        checked_confidence(plan)

    class LayaCommandAgent(RuleAgentBase):
        def __init__(self, side_name, params):
            super().__init__(side_name)
            self.attempts = 0
            self.actions = 0
            self.completed = set()
            self.pending_verify = []
            self.plan = None if callable(plan) else plan
            self.plan_requested = False
            self.next_replan_at = 0
            self.plan_context = None
            self.last_result = None
            self.applied = {}
            self.launch_watches = []
            self.pending_loadout = None

        def step(self, time_elapse, situation):
            if self.pending_loadout:
                pending = self.pending_loadout
                selected = pending["selected"]
                target = selected["loadout_id"]
                ids = selected["unit_ids"]
                states = {guid: situation[0].get(guid) for guid in ids}
                if (time_elapse > pending["deadline"] or
                    any(unit is None or (unit.get("mission") or "") != pending["missions"].get(guid)
                        for guid, unit in states.items()) or
                    (selected["contact_id"] and selected["contact_id"] not in situation[1]) or
                    (selected["destination_id"] and selected["destination_id"] not in situation[0])):
                    record("loadout_failed", candidate=selected["id"], reason="timeout or situation changed")
                    self.last_result = {"event": "loadout_failed", "candidate": selected["id"]}
                    self.pending_loadout = None
                    return
                if all(current_loadout(unit) and current_loadout(unit)[0] == target and
                       unit_condition(unit) == 1 for unit in states.values()):
                    self.pending_loadout = None
                    record("loadout_ready", candidate=selected["id"], loadout_id=target,
                           simulation_seconds=time_elapse)
                    self._execute_mission(selected, situation, time_elapse, pending["pair_mode"],
                                          pending["signature"])
                return
            if self.pending_verify:
                expected = {guid: mission_id for mission_id, unit_ids in self.pending_verify
                            for guid in unit_ids}
                observed = {guid: situation[0].get(guid, {}).get("mission") for guid in expected}
                matched = all(observed[guid] == mission_id for guid, mission_id in expected.items())
                mission_ids = [mission_id for mission_id, _ in self.pending_verify]
                record("command_observed", mission_id=mission_ids[0], mission_ids=mission_ids,
                       assignments=observed, matched=matched)
                self.last_result = {"event": "command_observed", "mission_ids": mission_ids,
                                    "matched": matched}
                self.pending_verify.clear()
            for watch in list(self.launch_watches):
                airborne = [guid for guid in watch["unit_ids"]
                            if air_availability(situation[0].get(guid, {})) == "airborne"]
                unknown = [guid for guid in watch["unit_ids"]
                           if air_availability(situation[0].get(guid, {})) == "unknown"]
                if len(airborne) != watch["last_airborne"] or time_elapse >= watch["deadline"]:
                    status = ("complete" if len(airborne) == watch["requested"] else
                              "unverified" if time_elapse >= watch["deadline"] and unknown else
                              "shortfall" if time_elapse >= watch["deadline"] else "pending")
                    record("launch_observed", mission_id=watch["mission_ids"][0],
                           mission_ids=watch["mission_ids"],
                           simulation_seconds=time_elapse, requested=watch["requested"],
                           airborne=len(airborne), airborne_unit_ids=airborne,
                           unknown_unit_ids=unknown, status=status)
                    watch["last_airborne"] = len(airborne)
                    if status in ("complete", "shortfall", "unverified"):
                        self.last_result = {"event": "launch_observed", "mission_ids": watch["mission_ids"],
                                            "requested": watch["requested"], "airborne": len(airborne),
                                            "status": status}
                        self.launch_watches.remove(watch)
            if max_actions is not None and self.actions >= max_actions:
                return
            if dynamic:
                if time_elapse < self.next_replan_at:
                    return
                while self.next_replan_at <= time_elapse:
                    self.next_replan_at += replan_interval
                self.attempts += 1
                try:
                    if hasattr(plan, "set_context"):
                        plan.set_context({"previous_plan": self.plan_context,
                                          "last_result": self.last_result})
                    proposed = plan(self.Name, time_elapse, situation)
                    validate_plan(proposed)
                    if proposed["side"] != self.Name:
                        raise ValueError("Codex plan side mismatch")
                    checked_confidence(proposed)
                    self.plan = proposed
                    self.completed.clear()
                    self.plan_context = {"plan_id": proposed["plan_id"],
                                         "objective": proposed["objective"], "task": proposed["task"]}
                    record("codex_plan_ready", simulation_seconds=time_elapse, plan=proposed)
                except Exception as error:
                    self.plan = None
                    self.last_result = {"event": "codex_plan_failed", "reason": repr(error)}
                    record("codex_plan_failed", simulation_seconds=time_elapse, reason=repr(error))
                    return
            else:
                if self.attempts >= max(3, max_actions * 3):
                    return
                self.attempts += 1
            current_plan = self.plan
            if self.Name != current_plan["side"]:
                record("decision_skipped", reason="side mismatch")
                return
            try:
                def signature(item):
                    key = {name: item.get(name) for name in
                           ("family", "subtype", "area_spec", "settings",
                            "contact_id", "destination_id", "loadout_id")}
                    key["unit_ids"] = sorted(item["unit_ids"])
                    return json.dumps(key, sort_keys=True)
                def still_active(item):
                    assignments = self.applied.get(signature(item))
                    return bool(assignments and set(assignments) == set(item["unit_ids"]) and
                                all(situation[0].get(guid, {}).get("mission") == assignments[guid]
                                                    for guid in item["unit_ids"]))
                protected = ({mission_id for assignments in self.applied.values()
                              for mission_id in assignments.values()} if dynamic else set())
                pair_mode = current_plan["task"]["units"].get("pair_selection", False)
                if pair_mode:
                    pools = {}
                    for count in current_plan["task"]["units"]["counts"]:
                        options = generate_candidates(current_plan, situation, time_elapse,
                                                      count_filter=count, protected_missions=protected)
                        options = [item for item in options if item["id"] not in self.completed
                                   and not (dynamic and still_active(item))]
                        if options:
                            pools[count] = options
                    available = [item for options in pools.values() for item in options]
                else:
                    available = [item for item in generate_candidates(current_plan, situation, time_elapse)
                                 if item["id"] not in self.completed
                                 and not (dynamic and still_active(item))]
            except (KeyError, TypeError, ValueError) as error:
                record("decision_skipped", reason=str(error))
                return
            if not available:
                record("decision_skipped", reason="no legal actions or plan expired")
                self.last_result = {"event": "decision_skipped", "reason": "no legal actions or plan expired"}
                return
            if pair_mode:
                count_body = build_pair_count_request(current_plan, self.Name, time_elapse,
                                                      situation, available)
                record("laya_count_request", simulation_seconds=time_elapse, body=count_body)
                try:
                    count_response = query_laya(url, count_body, timeout)
                    count_ids = {"count_%d" % count for count in pools} | {"hold"}
                    count_id, count_confidence = select_laya_action(
                        count_response, count_ids, checked_confidence(current_plan))
                except Exception as error:
                    record("laya_hold", phase="aircraft_count", reason=repr(error))
                    self.last_result = {"event": "laya_hold", "phase": "aircraft_count", "reason": repr(error)}
                    return
                record("laya_count_choice", candidate=count_id, confidence=count_confidence,
                       probabilities=count_response["answers"]["next_action"].get("probabilities"))
                if count_id == "hold":
                    record("laya_hold", phase="aircraft_count", reason="model selected hold")
                    self.last_result = {"event": "laya_hold", "phase": "aircraft_count"}
                    return
                chosen_count = int(count_id.split("_", 1)[1])
                available = pools[chosen_count]
            if pair_mode:
                body = build_pair_group_request(current_plan, self.Name, time_elapse,
                                                situation, available, chosen_count)
            else:
                body = build_laya_request(current_plan, self.Name, time_elapse, situation,
                                          available + [{"id": "hold", "description": "保持现有任务，不下达新指令",
                                                        "unit_ids": [], "family": "hold", "subtype": None,
                                                        "points": None, "contact_id": None,
                                                        "destination_id": None}])
            record("laya_request", simulation_seconds=time_elapse, body=body)
            try:
                response = query_laya(url, body, timeout)
                selected_id, confidence = select_laya_action(response, {item["id"] for item in available} | {"hold"},
                                                              checked_confidence(current_plan))
            except Exception as error:
                record("laya_hold", reason=repr(error))
                self.last_result = {"event": "laya_hold", "reason": repr(error)}
                return
            record("laya_choice", candidate=selected_id, confidence=confidence,
                   probabilities=response["answers"]["next_action"].get("probabilities"),
                   routing=response.get("routing"), usage=response.get("usage"))
            if selected_id == "hold":
                record("laya_hold", reason="model selected hold")
                self.last_result = {"event": "laya_hold", "reason": "model selected hold"}
                return
            selected = next(item for item in available if item["id"] == selected_id)
            self.completed.add(selected_id)
            self.actions += 1  # Never repeat a partially applied command.
            live_units = {guid: self.get_unit(guid) for guid in selected["unit_ids"]}
            if any(guid not in situation[0] or live_units[guid] is None for guid in selected["unit_ids"]):
                record("command_rejected", candidate=selected_id, reason="unit missing from controlled side")
                self.last_result = {"event": "command_rejected", "reason": "unit missing from controlled side"}
                return
            if pair_mode:
                changed = [guid for guid, unit in live_units.items()
                           if getattr(unit, "AssignedMission", situation[0][guid].get("mission")) !=
                           situation[0][guid].get("mission")]
                if changed:
                    record("command_rejected", candidate=selected_id,
                           reason="unit mission changed before assignment", unit_ids=changed)
                    self.last_result = {"event": "command_rejected", "reason": "unit mission changed"}
                    return
            if selected["contact_id"] and selected["contact_id"] not in situation[1]:
                record("command_rejected", candidate=selected_id, reason="target no longer visible")
                self.last_result = {"event": "command_rejected", "reason": "target no longer visible"}
                return
            if selected["destination_id"] and selected["destination_id"] not in situation[0]:
                record("command_rejected", candidate=selected_id, reason="destination no longer controlled")
                self.last_result = {"event": "command_rejected", "reason": "destination no longer controlled"}
                return
            if selected.get("loadout_changes"):
                target = selected["loadout_id"]
                changes = selected["loadout_changes"]
                if any(current_loadout(situation[0][guid]) != current_loadout(live_units[guid].get_summary_info())
                       or unit_condition(situation[0][guid]) not in (1, 7)
                       for guid in changes):
                    record("loadout_failed", candidate=selected_id, reason="aircraft state changed")
                    self.last_result = {"event": "loadout_failed", "reason": "aircraft state changed"}
                    return
                try:
                    for guid in selected["unit_ids"]:
                        old_mission = situation[0][guid].get("mission")
                        if old_mission:
                            _checked(live_units[guid].withdraw_from_mission(), "withdraw_from_mission")
                            record("loadout_withdrawn", candidate=selected_id, unit_id=guid,
                                   old_mission=old_mission)
                    for guid in changes:
                        _checked(live_units[guid].set_loadout(target), "set_loadout")
                        record("loadout_requested", candidate=selected_id, unit_id=guid,
                               loadout_id=target, simulation_seconds=time_elapse)
                except Exception as error:
                    record("loadout_failed", candidate=selected_id, reason=repr(error))
                    self.last_result = {"event": "loadout_failed", "reason": repr(error)}
                    return
                self.pending_loadout = {"selected": selected, "pair_mode": pair_mode,
                                        "signature": signature,
                                        "deadline": min(time_elapse + 3600,
                                                        current_plan.get("valid_until_sim_seconds", float("inf"))),
                                        "missions": {guid: "" for guid in selected["unit_ids"]}}
                return
            self._execute_mission(selected, situation, time_elapse, pair_mode, signature)


        def _execute_mission(self, selected, situation, time_elapse, pair_mode, signature):
            selected_id = selected["id"]
            assigned = []
            created = []
            assignments = {}
            try:
                groups = selected["pair_ids"] if pair_mode else [selected["unit_ids"]]
                for index, group in enumerate(groups, 1):
                    part = dict(selected)
                    if pair_mode:
                        part["mission_name"] = "%s_p%d" % (selected["mission_name"], index)
                    if len(part["mission_name"]) > 60:
                        raise ValueError("mission name exceeds 60 characters")
                    mission = create_selected_mission(self, part)
                    created.append((mission.ID, []))
                    record("mission_ready", candidate=selected_id, mission=part["mission_name"],
                           mission_id=mission.ID, family=selected["family"], pair_index=index if pair_mode else None)
                    if selected["contact_id"]:
                        _checked(mission.strike_add_target([selected["contact_id"]]), "strike_add_target")
                    apply_settings(mission, selected["family"], selected["settings"])
                    for guid in group:
                        reply = mission.assign_unit(guid)
                        code = getattr(reply, "code", None)
                        record("unit_assignment", candidate=selected_id, unit_id=guid,
                               mission_id=mission.ID, reply_code=code)
                        if code != 0:
                            raise RuntimeError("assignment reply code %r" % code)
                        assigned.append(guid)
                        created[-1][1].append(guid)
                        assignments[guid] = mission.ID
                record("command_applied", candidate=selected_id, mission=selected["mission_name"],
                       mission_ids=[mission_id for mission_id, _ in created],
                       unit_ids=assigned, reassigned_from=selected.get("reassigned_from", {}))
                self.last_result = {"event": "command_applied", "mission": selected["mission_name"],
                                    "mission_ids": [mission_id for mission_id, _ in created],
                                    "unit_ids": assigned}
                if pair_mode:
                    mission_ids = [mission_id for mission_id, _ in created]
                    record("launch_requested", mission_id=mission_ids[0], mission_ids=mission_ids,
                           requested=len(assigned), unit_ids=assigned, simulation_seconds=time_elapse)
                    self.launch_watches.append({"mission_ids": mission_ids, "unit_ids": assigned,
                                                "requested": len(assigned), "last_airborne": 0,
                                                "deadline": time_elapse + 600})
            except Exception as error:
                record("command_failed", candidate=selected_id, reason=repr(error),
                       assigned_unit_ids=assigned, partial=bool(assigned))
                self.last_result = {"event": "command_failed", "reason": repr(error),
                                    "assigned_unit_ids": assigned}
            finally:
                if assignments:
                    self.applied[signature(selected)] = assignments
                self.pending_verify.extend((mission_id, group) for mission_id, group in created if group)

        def deduction_end(self):
            if self.pending_loadout:
                record("loadout_unverified", candidate=self.pending_loadout["selected"]["id"],
                       reason="scenario ended before rearm confirmation")
            for watch in self.launch_watches:
                record("launch_unverified", mission_ids=watch["mission_ids"], requested=watch["requested"],
                       reason="scenario ended before launch observation completed")
            record("laya_agent_end", attempts=self.attempts, actions=self.actions)

    return LayaCommandAgent
