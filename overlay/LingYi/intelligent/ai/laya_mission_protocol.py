"""Version 2 mission plan: bind a single strategic task to legal Laya choices.

This module only reads situation dictionaries. It never calls the simulation engine.
"""

import json
import itertools
import math
import re
from urllib.request import Request, urlopen

from engine.LingYi.entitys.global_util import AircraftType, ContactType, MissionPatrolType, MissionStrikeType


MAX_CANDIDATES = 16
PATROL_TYPES = {name.lower(): value for name, value in MissionPatrolType.__members__.items()}
STRIKE_TYPES = {name.lower(): value for name, value in MissionStrikeType.__members__.items()}
AIRCRAFT_TYPES = {name.lower(): value.value for name, value in AircraftType.__members__.items()}
CONTACT_TYPES = {name.lower(): value.value for name, value in ContactType.__members__.items()}
AREA_FAMILIES = {"patrol", "support", "cargo", "mine", "mine_clear"}
FAMILIES = AREA_FAMILIES | {"strike", "ferry"}
SETTING_NAMES = {
    "patrol": {"active", "start_time", "end_time", "one_third_rule", "flight_size", "check_outside_area"},
    "strike": {"active", "start_time", "end_time", "preplanned_only", "flight_size"},
    "support": {"active", "start_time", "end_time", "one_third_rule", "flight_size", "station_count"},
    "ferry": {"active", "start_time", "end_time", "behavior", "flight_size"},
    "cargo": {"active", "start_time", "end_time", "unload_mounts"},
    "mine": {"active", "start_time", "end_time", "one_third_rule", "arming_delay", "group_size"},
    "mine_clear": {"active", "start_time", "end_time", "one_third_rule", "group_size"},
}
_TIME_FORMAT = re.compile(r"^\d{1,2}/\d{1,2}/\d{4} \d{2}:\d{2}:\d{2}$")
_DELAY_FORMAT = re.compile(r"^\d+:\d+:\d+:\d+$")


def _identifier(value, field, limit=24):
    if not isinstance(value, str) or not value.isidentifier() or not 0 < len(value) <= limit:
        raise ValueError("%s must be a short identifier" % field)
    return value


def _finite_number(value, field):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("%s must be finite numeric" % field)
    return float(value)


def _validate_settings(family, settings):
    if not isinstance(settings, dict) or set(settings) - SETTING_NAMES[family]:
        raise ValueError("unsupported %s mission setting" % family)
    for key, value in settings.items():
        if key in {"active", "one_third_rule", "check_outside_area", "preplanned_only"}:
            if type(value) is not bool:
                raise ValueError("%s must be boolean" % key)
        elif key in {"start_time", "end_time"}:
            if not isinstance(value, str) or not _TIME_FORMAT.fullmatch(value):
                raise ValueError("%s must be MM/DD/YYYY HH:MM:SS" % key)
        elif key == "flight_size":
            if type(value) is not int or value not in (1, 2, 3, 4, 6):
                raise ValueError("flight_size must be 1,2,3,4 or 6")
        elif key == "group_size":
            if type(value) is not int or value not in (1, 2, 3, 4, 6):
                raise ValueError("group_size must be 1,2,3,4 or 6")
        elif key == "station_count":
            if type(value) is not int or not 1 <= value <= 20:
                raise ValueError("station_count outside 1..20")
        elif key == "behavior":
            if value not in ("one_way", "cycle", "random"):
                raise ValueError("invalid ferry behavior")
        elif key == "arming_delay":
            if not isinstance(value, str) or not _DELAY_FORMAT.fullmatch(value):
                raise ValueError("arming_delay must be days:hours:minutes:seconds")
        elif key == "unload_mounts":
            if not isinstance(value, list) or not 1 <= len(value) <= 12:
                raise ValueError("unload_mounts must contain 1..12 entries")
            for mount in value:
                if not isinstance(mount, dict) or set(mount) != {"dbid", "count"}:
                    raise ValueError("unload mount needs dbid and count")
                if type(mount["dbid"]) is not int or mount["dbid"] <= 0 or type(mount["count"]) is not int or not 1 <= mount["count"] <= 100:
                    raise ValueError("invalid unload mount dbid/count")


def validate_plan(plan):
    """Validate the GPT-to-Laya bridge contract without talking to GPT or Laya."""
    if not isinstance(plan, dict) or type(plan.get("version")) is not int or plan["version"] != 2:
        raise ValueError("mission plan version must be 2")
    if set(plan) - {"version", "plan_id", "scenario", "side", "objective", "constraints",
                    "valid_from_sim_seconds", "valid_until_sim_seconds", "min_confidence", "task"}:
        raise ValueError("unknown plan field")
    _identifier(plan.get("plan_id"), "plan_id", 16)
    if "scenario" in plan and (not isinstance(plan["scenario"], str) or not plan["scenario"]):
        raise ValueError("scenario must be a nonempty string")
    if not isinstance(plan.get("side"), str) or not plan["side"]:
        raise ValueError("side is required")
    if not isinstance(plan.get("objective"), str) or not plan["objective"].strip():
        raise ValueError("objective is required")
    constraints = plan.get("constraints", [])
    if not isinstance(constraints, list) or len(constraints) > 12 or any(not isinstance(x, str) for x in constraints):
        raise ValueError("constraints must be a short string list")
    for name in ("valid_from_sim_seconds", "valid_until_sim_seconds"):
        if name in plan and _finite_number(plan[name], name) < 0:
            raise ValueError("%s must be nonnegative" % name)
    if plan.get("valid_from_sim_seconds", 0) > plan.get("valid_until_sim_seconds", float("inf")):
        raise ValueError("plan validity interval is reversed")
    task = plan.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("family"), str) or task["family"] not in FAMILIES:
        raise ValueError("task.family must name one of seven mission families")
    family = task["family"]
    allowed_task = {"family", "subtype", "units", "settings", "loadout"}
    allowed_task.add("areas" if family in AREA_FAMILIES else "target" if family == "strike" else "destination")
    if set(task) - allowed_task:
        raise ValueError("unknown task field")
    subtype = task.get("subtype")
    if family == "patrol" and (not isinstance(subtype, str) or subtype not in PATROL_TYPES):
        raise ValueError("unknown patrol subtype")
    if family == "strike" and (not isinstance(subtype, str) or subtype not in STRIKE_TYPES):
        raise ValueError("unknown strike subtype")
    if family not in ("patrol", "strike") and subtype is not None:
        raise ValueError("this mission family has no subtype")
    units = task.get("units")
    if not isinstance(units, dict) or units.get("category") not in ("Aircraft", "Ship", "Submarine"):
        raise ValueError("task.units.category must be Aircraft, Ship or Submarine")
    if set(units) - {"category", "counts", "aircraft_type", "name_contains", "unassigned_only",
                      "max_spread_deg", "pair_selection", "allow_reassignment",
                      "allow_ai_reassignment", "reserve_pairs"}:
        raise ValueError("unknown unit selector field")
    loadout = task.get("loadout")
    if loadout is not None:
        if units["category"] != "Aircraft" or family not in ("patrol", "strike", "support", "ferry"):
            raise ValueError("loadout requires an aircraft mission")
        if not isinstance(loadout, dict) or set(loadout) != {"role", "allowed_ids", "allow_change"}:
            raise ValueError("loadout needs role, allowed_ids and allow_change")
        if loadout["role"] not in ("air", "land", "ship", "submarine", "support", "any"):
            raise ValueError("invalid loadout role")
        if family == "strike" and loadout["role"] != task.get("subtype"):
            raise ValueError("strike loadout role must match target type")
        ids = loadout["allowed_ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 4 or any(type(x) is not int or x <= 0 for x in ids) or len(set(ids)) != len(ids):
            raise ValueError("loadout.allowed_ids needs 1..4 distinct positive DBIDs")
        if type(loadout["allow_change"]) is not bool:
            raise ValueError("loadout.allow_change must be boolean")
    counts = units.get("counts")
    if not isinstance(counts, list) or not 1 <= len(counts) <= 4 or any(type(n) is not int or not 1 <= n <= 10 for n in counts) or len(set(counts)) != len(counts):
        raise ValueError("task.units.counts needs 1..4 distinct values in 1..10")
    aircraft_type = units.get("aircraft_type")
    if aircraft_type is not None and (units["category"] != "Aircraft" or
                                      not isinstance(aircraft_type, str) or aircraft_type not in AIRCRAFT_TYPES):
        raise ValueError("unknown or inapplicable aircraft_type")
    if not isinstance(units.get("name_contains", ""), str) or type(units.get("unassigned_only", False)) is not bool:
        raise ValueError("invalid unit name or assignment filter")
    for flag in ("pair_selection", "allow_reassignment", "allow_ai_reassignment"):
        if type(units.get(flag, False)) is not bool:
            raise ValueError("%s must be boolean" % flag)
    if units.get("allow_ai_reassignment") and not units.get("allow_reassignment"):
        raise ValueError("AI reassignment requires allow_reassignment")
    reserve = units.get("reserve_pairs", 0)
    if type(reserve) is not int or not 0 <= reserve <= 4:
        raise ValueError("reserve_pairs outside 0..4")
    if units.get("pair_selection"):
        if units["category"] != "Aircraft" or any(n % 2 or n > 8 for n in counts):
            raise ValueError("pair_selection requires Aircraft counts from 2,4,6,8")
        if family in ("patrol", "strike", "support", "ferry") and task.get("settings", {}).get("flight_size", 2) != 2:
            raise ValueError("pair_selection requires flight_size 2")
    spread = _finite_number(units.get("max_spread_deg", 2), "max_spread_deg")
    if not 0 <= spread <= 20:
        raise ValueError("max_spread_deg outside 0..20")
    if family in AREA_FAMILIES:
        areas = task.get("areas")
        if not isinstance(areas, list) or not 1 <= len(areas) <= 4:
            raise ValueError("area mission needs 1..4 areas")
        ids = [_identifier(area.get("id"), "area.id", 16) for area in areas if isinstance(area, dict)]
        if len(ids) != len(areas) or len(set(ids)) != len(ids):
            raise ValueError("area ids must be distinct")
        for area in areas:
            if set(area) - {"id", "points", "center", "half_span_deg", "offset_deg"}:
                raise ValueError("unknown area field")
            if ("points" in area) == (area.get("center") == "selected_units"):
                raise ValueError("area needs either points or selected_units center")
            if "points" in area:
                if set(area) != {"id", "points"}:
                    raise ValueError("fixed area only accepts points")
                _bind_area(area, [], {})
            else:
                if "half_span_deg" not in area:
                    raise ValueError("dynamic area needs half_span_deg")
                half = _finite_number(area["half_span_deg"], "half_span_deg")
                offset = area.get("offset_deg", [0, 0])
                if not 0.05 <= half <= 5 or not isinstance(offset, list) or len(offset) != 2:
                    raise ValueError("invalid dynamic area")
                for value in offset:
                    if not -20 <= _finite_number(value, "offset_deg") <= 20:
                        raise ValueError("area offset outside 20 degrees")
    elif family == "strike":
        target = task.get("target")
        if not isinstance(target, dict) or not any(target.get(k) for k in ("contact_ids", "type", "name_contains")):
            raise ValueError("strike needs a contact selector")
        if set(target) - {"contact_ids", "type", "name_contains", "allow_unknown_side"}:
            raise ValueError("unknown target selector field")
        if type(target.get("allow_unknown_side", False)) is not bool:
            raise ValueError("allow_unknown_side must be boolean")
        if "name_contains" in target and not isinstance(target["name_contains"], str):
            raise ValueError("target.name_contains must be text")
        if target.get("type") is not None and (not isinstance(target["type"], str) or
                                               target["type"] not in CONTACT_TYPES):
            raise ValueError("unknown contact type")
        if "contact_ids" in target and (not isinstance(target["contact_ids"], list) or
                                         any(not isinstance(x, str) or not x for x in target["contact_ids"])):
            raise ValueError("contact_ids must be strings")
    else:
        destination = task.get("destination")
        if not isinstance(destination, dict) or not any(destination.get(k) for k in ("unit_ids", "name_contains")):
            raise ValueError("ferry needs a destination selector")
        if set(destination) - {"unit_ids", "name_contains", "category"}:
            raise ValueError("unknown destination selector field")
        if "name_contains" in destination and not isinstance(destination["name_contains"], str):
            raise ValueError("destination.name_contains must be text")
        if destination.get("category", "Facility") not in ("Facility", "Ship"):
            raise ValueError("ferry destination must be a facility or ship")
        if "unit_ids" in destination and (not isinstance(destination["unit_ids"], list) or
                                           any(not isinstance(x, str) or not x for x in destination["unit_ids"])):
            raise ValueError("destination unit_ids must be strings")
    _validate_settings(family, task.get("settings", {}))
    return plan


def _distance_sq(a, b):
    return (float(a[1]["lat"]) - float(b[1]["lat"])) ** 2 + (float(a[1]["lon"]) - float(b[1]["lon"])) ** 2


def _bind_units(selector, units, count):
    category = selector["category"]
    matching = [(guid, unit) for guid, unit in units.items() if isinstance(unit, dict)
                and unit.get("category") == category
                and (selector.get("aircraft_type") is None or unit.get("type") == AIRCRAFT_TYPES[selector["aircraft_type"]])
                and selector.get("name_contains", "").casefold() in str(unit.get("name", "")).casefold()
                and (not selector.get("unassigned_only", True) or not unit.get("mission"))]
    matching.sort(key=lambda pair: (str(pair[1].get("name", "")), pair[0]))
    if len(matching) < count:
        raise ValueError("not enough matching units")
    # Pick a geographically coherent group rather than the first names.
    if count > 1:
        choices = []
        for anchor in matching:
            cluster = sorted(matching, key=lambda pair: (_distance_sq(anchor, pair), pair[0]))[:count]
            choices.append((sum(_distance_sq(anchor, pair) for pair in cluster), cluster))
        selected = min(choices, key=lambda pair: pair[0])[1]
    else:
        selected = matching[:1]
    spread = max(max(abs(float(a[1][axis]) - float(b[1][axis])) for a in selected for b in selected)
                 for axis in ("lat", "lon"))
    if not math.isfinite(spread) or spread > selector.get("max_spread_deg", 2):
        raise ValueError("selected units are too far apart")
    return [guid for guid, _ in selected]


READY_AIR_OPS = {1, 2, 4, 8, 9, 18, 23, 24, 26}
AIRBORNE_AIR_OPS = {0, 13, 14, 15, 16, 19, 20, 21, 22}


def current_loadout(unit):
    """Return the single installed loadout, if the situation reports one."""
    loadouts = unit.get("loadout") or {}
    if not isinstance(loadouts, dict) or len(loadouts) != 1:
        return None
    value = next(iter(loadouts.values()))
    if not isinstance(value, dict):
        return None
    dbid = value.get("DBID")
    return (dbid, str(value.get("Name", ""))) if type(dbid) is int and dbid > 0 else None


def unit_condition(unit):
    ops = unit.get("airops") or {}
    return ops.get("Condition", ops.get("Con")) if isinstance(ops, dict) else None


def air_availability(unit):
    """Report launch readiness from the SDK's AirOps condition when present."""
    condition = unit_condition(unit)
    if condition in READY_AIR_OPS:
        return "ready"
    if condition in AIRBORNE_AIR_OPS:
        return "airborne"
    return "unknown" if condition is None else "not_ready"


def _pair_aircraft(selector, units, protected_missions):
    rows = []
    for guid, unit in units.items():
        if not isinstance(unit, dict) or unit.get("category") != "Aircraft":
            continue
        if selector.get("aircraft_type") and unit.get("type") != AIRCRAFT_TYPES[selector["aircraft_type"]]:
            continue
        if selector.get("name_contains", "").casefold() not in str(unit.get("name", "")).casefold():
            continue
        mission = unit.get("mission")
        if mission and (selector.get("unassigned_only", False) or not selector.get("allow_reassignment", False)):
            continue
        if mission in protected_missions and not selector.get("allow_ai_reassignment", False):
            continue
        if air_availability(unit) not in ("ready", "unknown"):
            continue
        if type(unit.get("damage", 0)) in (int, float) and unit.get("damage", 0) >= 100:
            continue
        if type(unit.get("lat")) not in (int, float) or type(unit.get("lon")) not in (int, float):
            continue
        rows.append((guid, unit))
    rows.sort(key=lambda row: (bool(row[1].get("mission")), air_availability(row[1]) != "ready",
                               str(row[1].get("name", "")), row[0]))
    return rows[:24]


def _pair_groups(selector, units, count, protected_missions, loadout_plan=None):
    rows = _pair_aircraft(selector, units, protected_missions)
    if len(rows) < count + 2 * selector.get("reserve_pairs", 0):
        return []
    pairs = []
    max_spread = selector.get("max_spread_deg", 2)
    for left, right in itertools.combinations(rows, 2):
        # The broad aircraft type (e.g. fighter=2001) is not a flight model.
        # A two-aircraft flight must use the same concrete platform DBID.
        dbid = left[1].get("dbid")
        if dbid in (None, "", 0) or dbid != right[1].get("dbid"):
            continue
        left_loadout, right_loadout = current_loadout(left[1]), current_loadout(right[1])
        if not loadout_plan and left_loadout and right_loadout and left_loadout[0] != right_loadout[0]:
            continue
        if max(abs(float(left[1][axis]) - float(right[1][axis])) for axis in ("lat", "lon")) > max_spread:
            continue
        pairs.append((left, right))
    pairs.sort(key=lambda pair: (sum(bool(row[1].get("mission")) for row in pair),
                                 _distance_sq(*pair), tuple(row[0] for row in pair)))
    # Include a disjoint baseline before alternatives so large counts remain possible.
    baseline, used = [], set()
    for pair in pairs:
        ids = {row[0] for row in pair}
        if not ids & used:
            baseline.append(pair)
            used.update(ids)
    pool = baseline[:5]
    pool.extend(pair for pair in pairs if pair not in pool)
    pool = pool[:16]
    groups, seen = [], set()
    for combination in itertools.combinations(pool, count // 2):
        ids = tuple(row[0] for pair in combination for row in pair)
        key = tuple(sorted(ids))
        if len(set(ids)) != count or key in seen:
            continue
        seen.add(key)
        assigned = sum(bool(units[guid].get("mission")) for guid in ids)
        score = (assigned, sum(_distance_sq(*pair) for pair in combination), key)
        groups.append((score, [list(row[0] for row in pair) for pair in combination]))
    groups.sort(key=lambda item: item[0])
    return [pair_ids for _, pair_ids in groups[:3]]


def _bind_area(area, selected_ids, units):
    if "points" in area:
        points = area["points"]
    else:
        half = _finite_number(area.get("half_span_deg"), "half_span_deg")
        offset = area.get("offset_deg", [0, 0])
        if not 0.05 <= half <= 5 or not isinstance(offset, list) or len(offset) != 2:
            raise ValueError("invalid dynamic area")
        lat = sum(float(units[guid]["lat"]) for guid in selected_ids) / len(selected_ids) + _finite_number(offset[0], "offset latitude")
        lon = sum(float(units[guid]["lon"]) for guid in selected_ids) / len(selected_ids) + _finite_number(offset[1], "offset longitude")
        points = [[lat + half, lon - half], [lat + half, lon + half],
                  [lat - half, lon + half], [lat - half, lon - half]]
    if not isinstance(points, list) or not 3 <= len(points) <= 12:
        raise ValueError("area needs 3..12 points")
    bound = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("area point must be [lat, lon]")
        lat, lon = _finite_number(point[0], "latitude"), _finite_number(point[1], "longitude")
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("area point out of earth bounds")
        bound.append((round(lat, 6), round(lon, 6)))
    if len(set(bound)) < 3:
        raise ValueError("area needs three distinct points")
    return bound


def _contacts_for_target(target, contacts, side):
    ids = set(target.get("contact_ids", []))
    name_filter = str(target.get("name_contains", "")).casefold()
    type_code = CONTACT_TYPES.get(target.get("type"))
    matching = []
    for guid, contact in contacts.items():
        if not isinstance(contact, dict) or (ids and guid not in ids):
            continue
        contact_side = contact.get("Side")
        if contact_side == side or contact_side in ("中立国", "民用", "Neutral", "Civilian"):
            continue
        if not contact_side and not target.get("allow_unknown_side", False):
            continue
        if type_code is not None and contact.get("Type") != type_code:
            continue
        if name_filter not in str(contact.get("Name", "")).casefold():
            continue
        matching.append((guid, contact))
    matching.sort(key=lambda pair: pair[0])
    return matching[:4]


def _destinations_for_ferry(selector, units):
    ids = set(selector.get("unit_ids", []))
    name_filter = str(selector.get("name_contains", "")).casefold()
    category = selector.get("category", "Facility")
    matching = [(guid, unit) for guid, unit in units.items() if isinstance(unit, dict)
                and unit.get("category") == category and (not ids or guid in ids)
                and name_filter in str(unit.get("name", "")).casefold()]
    matching.sort(key=lambda pair: pair[0])
    return matching[:4]


def generate_candidates(plan, situation, sim_seconds, count_filter=None, protected_missions=()):
    """Return executable candidates bound to live side-visible units and contacts."""
    validate_plan(plan)
    start = plan.get("valid_from_sim_seconds", 0)
    end = plan.get("valid_until_sim_seconds", float("inf"))
    if not start <= sim_seconds <= end:
        return []
    units, contacts = situation
    task, family = plan["task"], plan["task"]["family"]
    candidates = []
    for count in task["units"]["counts"]:
        if count_filter is not None and count != count_filter:
            continue
        try:
            if task["units"].get("pair_selection"):
                groups = _pair_groups(task["units"], units, count, set(protected_missions),
                                      task.get("loadout"))
            else:
                groups = [[_bind_units(task["units"], units, count)]]
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if family in AREA_FAMILIES:
            variants = [(area["id"], area, None, None) for area in task["areas"]]
        elif family == "strike":
            variants = [("target%d" % index, None, guid, None) for index, (guid, _) in
                        enumerate(_contacts_for_target(task["target"], contacts, plan["side"]))]
        else:
            variants = [("base%d" % index, None, None, guid) for index, (guid, _) in
                        enumerate(_destinations_for_ferry(task["destination"], units))]
        per_count = []
        for group_index, pair_ids in enumerate(groups):
            unit_ids = [guid for pair in pair_ids for guid in pair]
            installed = {guid: current_loadout(units[guid]) for guid in unit_ids}
            loadout_plan = task.get("loadout")
            if loadout_plan:
                platforms = {units[guid].get("dbid") for guid in unit_ids}
                if len(platforms) != 1 or None in installed.values():
                    continue
                observed = {entry[0]: entry[1] for unit in units.values()
                            if isinstance(unit, dict) and unit.get("dbid") in platforms
                            for entry in [current_loadout(unit)] if entry is not None}
                loadout_options = [(dbid, observed[dbid]) for dbid in loadout_plan["allowed_ids"]
                                   if dbid in observed and
                                   (loadout_plan["allow_change"] or all(value[0] == dbid for value in installed.values())) and
                                   all(value[0] == dbid or (unit_condition(units[guid]) in (1, 7))
                                       for guid, value in installed.items())]
            else:
                # A flight with reported, different loadouts has no safe automatic launch path.
                if any(len({installed[guid][0] for guid in pair if installed[guid]}) > 1
                       for pair in pair_ids):
                    continue
                loadout_options = [(None, None)]
            for variant_id, area, contact_id, destination_id in variants:
              for loadout_id, loadout_name in loadout_options:
                try:
                    points = _bind_area(area, unit_ids, units) if area is not None else None
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                suffix = "_g%d" % group_index if task["units"].get("pair_selection") else ""
                candidate_id = "%s_%d_%s%s%s" % (family, count, variant_id, suffix,
                                                  "_l%d" % loadout_id if loadout_id else "")
                mission_name = "LY_%s_%s" % (plan["plan_id"], candidate_id)
                if len(mission_name) > 60:
                    raise ValueError("mission name exceeds 60 characters")
                detail = ("区域 " + variant_id if area is not None else
                          "目标 " + str(contacts[contact_id].get("Name", contact_id)) if contact_id else
                          "目的地 " + str(units[destination_id].get("name", destination_id)))
                reassigned = {guid: units[guid]["mission"] for guid in unit_ids if units[guid].get("mission")}
                protected_reassigned = sum(mission in protected_missions for mission in reassigned.values())
                description = "%s：%s；使用%d个%s单位" % (
                    plan["objective"], detail, count, task["units"]["category"])
                if task["units"].get("pair_selection"):
                    names = ["+".join(str(units[guid].get("name", guid)) for guid in pair) for pair in pair_ids]
                    description += "；双机组 %s；需从旧任务抽调%d架" % (" / ".join(names), len(reassigned))
                changes = [guid for guid in unit_ids if loadout_id and installed[guid][0] != loadout_id]
                if loadout_id:
                    description += "；挂载%s #%d，需换装%d架" % (loadout_name, loadout_id, len(changes))
                settings = dict(task.get("settings", {}))
                if task["units"].get("pair_selection") and family in ("patrol", "strike", "support", "ferry"):
                    settings["flight_size"] = 2
                per_count.append({"id": candidate_id, "family": family, "subtype": task.get("subtype"),
                                  "description": description, "mission_name": mission_name,
                                  "unit_ids": unit_ids, "pair_ids": pair_ids if task["units"].get("pair_selection") else None,
                                  "launch_count": count if task["units"].get("pair_selection") else None,
                                  "reassigned_from": reassigned, "area_spec": area,
                                  "protected_reassigned": protected_reassigned,
                                  "loadout_id": loadout_id, "loadout_name": loadout_name,
                                  "loadout_changes": changes,
                                  "points": points, "contact_id": contact_id,
                                  "destination_id": destination_id, "settings": settings})
        candidates.extend(per_count[:15 if count_filter is not None else 2] if task["units"].get("pair_selection") else per_count)
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError("plan produced more than %d candidates" % MAX_CANDIDATES)
    return candidates


def build_laya_request(plan, side, sim_seconds, situation, candidates):
    units, contacts = situation
    selected_ids = {guid for item in candidates for guid in item["unit_ids"]}
    target_ids = {item["contact_id"] for item in candidates if item.get("contact_id")}
    state = {"protocol_version": 2, "plan_id": plan["plan_id"], "side": side,
             "simulation_seconds": sim_seconds, "objective": plan["objective"],
             "constraints": plan.get("constraints", []), "task_family": plan["task"]["family"],
             "task_subtype": plan["task"].get("subtype"),
             "units": [{**{key: units[guid].get(key) for key in
                        ("guid", "name", "category", "type", "mission", "lat", "lon", "damage")},
                        "availability": air_availability(units[guid]) if units[guid].get("category") == "Aircraft" else None,
                        "loadout": current_loadout(units[guid]) if units[guid].get("category") == "Aircraft" else None}
                       for guid in sorted(selected_ids)],
             "contacts": [{"id": guid, "name": contacts[guid].get("Name"), "type": contacts[guid].get("Type"),
                           "side": contacts[guid].get("Side"), "lat": contacts[guid].get("Lat"),
                           "lon": contacts[guid].get("Lon")} for guid in sorted(target_ids)],
             "candidates": [{key: item.get(key) for key in
                             ("id", "family", "subtype", "unit_ids", "pair_ids", "launch_count",
                             "reassigned_from", "points", "contact_id", "destination_id",
                              "loadout_id", "loadout_name", "loadout_changes")}
                            for item in candidates]}
    return {"state": state, "model": "multilingual", "lang": "zh",
            "questions": {"next_action": {"type": "choice",
                                          "instructions": "根据上级任务和当前态势，从合法候选行动中选择下一步。",
                                          "criteria": {item["id"]: item["description"] for item in candidates}}}}


def build_pair_count_request(plan, side, sim_seconds, situation, candidates):
    """First Laya choice: zero or an allowed number of two-aircraft groups."""
    counts = sorted({len(item["unit_ids"]) for item in candidates})
    options = {}
    for count in counts:
        matching = [item for item in candidates if len(item["unit_ids"]) == count]
        best = min(matching, key=lambda item: (item["protected_reassigned"],
                                               len(item["reassigned_from"])))
        initial_reassigned = len(best["reassigned_from"]) - best["protected_reassigned"]
        options["count_%d" % count] = "%d架执行新任务；%d组可选；调整预设任务%d架、我方新建任务%d架" % (
            count, len(matching), initial_reassigned, best["protected_reassigned"])
    options["hold"] = "投入0架；放弃本轮上级任务"
    return {"state": {"protocol_version": 2, "plan_id": plan["plan_id"], "side": side,
                      "simulation_seconds": sim_seconds, "phase": "aircraft_count",
                      "objective": plan["objective"], "available_counts": counts},
            "model": "multilingual", "lang": "zh",
            "questions": {"next_action": {"type": "choice",
                                          "instructions": "上级任务已获批准；计划若允许，可调整想定预设任务。请选择合理投入量；只有态势不再需要新任务或资源不足时才选hold。",
                                          "criteria": options}}}


def build_pair_group_request(plan, side, sim_seconds, situation, candidates, count):
    """Second Laya choice: a small, fully bound group for the selected count."""
    ids = sorted({guid for item in candidates for guid in item["unit_ids"]})
    units = situation[0]
    aliases = {guid: "u%d" % index for index, guid in enumerate(ids, 1)}
    rows = [{"id": aliases[guid], "name": str(units[guid].get("name", ""))[:18],
             "dbid": units[guid].get("dbid"),
             "loadout_id": (current_loadout(units[guid]) or (None,))[0],
             "busy": bool(units[guid].get("mission")),
             "lat": round(float(units[guid]["lat"]), 2),
             "lon": round(float(units[guid]["lon"]), 2),
             "damage": units[guid].get("damage"),
             "ready": air_availability(units[guid]) == "ready"} for guid in ids]
    options = {}
    for item in candidates:
        pairs = [[aliases[guid] for guid in pair] for pair in item["pair_ids"]]
        initial_reassigned = len(item["reassigned_from"]) - item["protected_reassigned"]
        options[item["id"]] = "编组%s；预设任务调整%d架，新建任务调整%d架" % (
            "/".join("+".join(pair) for pair in pairs),
            initial_reassigned, item["protected_reassigned"])
        if item.get("loadout_id"):
            options[item["id"]] += "；挂载%s #%d，需换装%d架" % (
                item["loadout_name"], item["loadout_id"], len(item["loadout_changes"]))
    options["hold"] = "保持现状"
    return {"state": {"protocol_version": 2, "plan_id": plan["plan_id"], "side": side,
                      "simulation_seconds": sim_seconds, "phase": "aircraft_groups",
                      "objective": plan["objective"], "task_family": plan["task"]["family"],
              "loadout_role": (plan["task"].get("loadout") or {}).get("role"),
                      "selected_count": count, "units": rows},
            "model": "multilingual", "lang": "zh",
            "questions": {"next_action": {"type": "choice",
                                          "instructions": "选具体双机组；留意已有任务抽调，可选保持现状。",
                                          "criteria": options}}}


def select_laya_action(response, candidate_ids, min_confidence):
    if response.get("usage", {}).get("truncated"):
        raise ValueError("Laya truncated the state")
    answer = response["answers"]["next_action"]
    choice = answer["choice"]
    if choice not in candidate_ids:
        raise ValueError("unknown candidate chosen")
    confidence = answer.get("answer_confidence", answer.get("probabilities", {}).get(choice))
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or confidence < min_confidence:
        raise ValueError("missing or insufficient answer confidence")
    return choice, float(confidence)


def query_laya(url, body, timeout):
    request = Request(url.rstrip("/") + "/v1/systemone",
                      data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)
