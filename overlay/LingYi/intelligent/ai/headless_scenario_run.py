"""Run one LingYi scenario with observer, Laya, or Codex + Laya."""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import psutil

from agent.config import LingYiConfig
from engine.LingYi.entitys.agent_base import RuleAgentBase
from engine.LingYi.entitys.deduce_RL import Deduction
from unit_loss_monitor import UnitLossMonitor, with_loss_reporting
from score_report import build_score_report, find_engine_room, save_score_report


RUN_LOG = None


def record(event, **details):
    item = {"wall_time": datetime.now().isoformat(timespec="seconds"), "event": event}
    item.update(details)
    line = json.dumps(item, ensure_ascii=False, default=str)
    print(line, flush=True)
    if RUN_LOG is not None:
        with RUN_LOG.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")


class Observer(RuleAgentBase):
    """Observe scenario progress and leave all tactical decisions to the scenario."""

    def __init__(self, side_name, params):
        super().__init__(side_name)
        self.steps = 0

    def initial(self, situation):
        record("initial_situation", side=self.Name,
               units=len(situation[0]), contacts=len(situation[1]))

    def step(self, time_elapse, situation):
        self.steps += 1
        if self.steps == 1 or self.steps % 10 == 0:
            record(
                "situation_step",
                side=self.Name,
                step=self.steps,
                simulation_seconds=time_elapse,
                units=len(situation[0]),
                contacts=len(situation[1]),
            )

    def is_done(self):
        return False

    def deduction_end(self):
        record("observer_end", side=self.Name, steps=self.steps)


def selected_sides(args, parser):
    sides = args.sides if args.sides is not None else [args.side]
    if len(set(sides)) != len(sides):
        parser.error("controlled sides must be unique")
    if len(sides) > 1 and args.laya_plan:
        parser.error("--laya-plan controls one side; use --codex-plan for multiple sides")
    return sides


def side_brief_paths(values, sides, parser):
    paths = {}
    for value in values:
        side, separator, path = value.partition("=")
        if not separator or not side or not path or side not in sides:
            parser.error("--side-brief must be SIDE=PATH for a controlled side")
        if side in paths:
            parser.error("--side-brief repeated for %s" % side)
        paths[side] = Path(path)
    return paths


def make_side_record(side):
    def side_record(event, **details):
        record(event, side=side, **details)
    return side_record


def configure_rule_agents(args, sides, run_dir, scenario_path, brief_paths, planners=None):
    """Create one independent planner/bridge per side, sharing the Laya endpoint."""
    rule_config = {}
    loss_monitor = UnitLossMonitor(record)
    common_brief = None
    common_source = None
    if args.codex_plan:
        from scenario_brief import read_scenario_brief
        if args.mission_brief:
            common_brief = args.mission_brief.read_text(encoding="utf-8")
            common_source = str(args.mission_brief)
        elif any(side not in brief_paths for side in sides):
            common_brief = read_scenario_brief(scenario_path)
            common_source = str(scenario_path)
    for index, side in enumerate(sides, 1):
        side_record = make_side_record(side)
        side_dir = run_dir if len(sides) == 1 else run_dir / ("side_%02d" % index)
        side_dir.mkdir(parents=True, exist_ok=True)
        agent_class = Observer
        if args.laya_plan or args.codex_plan:
            from laya_command_agent import make_agent
            if args.codex_plan:
                from codex_mission_planner import CodexMissionPlanner
                source = brief_paths.get(side)
                brief = source.read_text(encoding="utf-8") if source else common_brief
                brief_source = str(source) if source else common_source
                (side_dir / "mission_brief.txt").write_text(brief, encoding="utf-8")
                side_record("mission_brief_loaded", source=brief_source,
                            characters=len(brief), directory=str(side_dir))
                plan = CodexMissionPlanner(args.scenario, side, side_dir, side_record,
                                           timeout=args.codex_timeout, model=args.codex_model,
                                           mission_brief=brief,
                                           knowledge_root=getattr(args, "knowledge_root", None))
                if planners is not None:
                    planners.append(plan)
                side_record("codex_workspace_ready", workspace=str(plan.workspace))
            else:
                with args.laya_plan.open(encoding="utf-8") as stream:
                    plan = json.load(stream)
                if plan.get("side") not in (None, side):
                    raise ValueError("Laya plan side does not match the controlled side")
                if plan.get("scenario") not in (None, args.scenario):
                    raise ValueError("Laya plan scenario does not match the loaded scenario")
            max_actions = args.laya_max_actions if args.laya_max_actions is not None else (
                None if args.codex_plan else 1)
            agent_class = make_agent(plan, args.laya_url, side_record, args.laya_timeout,
                                     max_actions, args.codex_interval)
            side_record("laya_configured", plan="codex_live" if args.codex_plan else str(args.laya_plan),
                        url=args.laya_url, max_actions=max_actions,
                        codex_interval=args.codex_interval if args.codex_plan else None,
                        directory=str(side_dir))
        rule_config[side] = {
            "headless_laya" if (args.laya_plan or args.codex_plan) else "headless_observer": {
                "class": with_loss_reporting(agent_class, loss_monitor),
                "unit": [],
                "contact": [],
                "point": [],
            }
        }
    return rule_config


def finish_reflections(planners, events_path, ending):
    """A failed reflection must neither mask a run error nor skip the other side."""
    for planner in planners:
        try:
            planner.reflect(events_path, ending)
        except Exception as error:
            planner.record("codex_reflection_failed", reason=repr(error), ending=ending)


def finish_scores(data_root, room_id, scenario, ending, run_dir):
    try:
        if not room_id:
            raise ValueError("未取得本局引擎房间 ID，未使用其他局的比分")
        report = save_score_report(build_score_report(data_root, room_id, scenario, ending), run_dir)
        record("score_summary", report=report)
    except Exception as error:
        record("score_unavailable", reason=str(error))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="琥珀残帆_2025湖南省赛想定（人机版）")
    sides_group = parser.add_mutually_exclusive_group()
    sides_group.add_argument("--side", default="红方")
    sides_group.add_argument("--sides", nargs="+", help="controlled sides; each gets its own Codex agent")
    parser.add_argument("--max-wall-minutes", type=int, default=90,
                        help="wall-clock limit in minutes; 0 waits for the scenario to finish")
    parser.add_argument("--compression", type=int, default=10001)
    parser.add_argument("--decision-interval", type=int,
                        help="engine callback interval in simulation seconds; Codex defaults to 60, others to 300")
    planning = parser.add_mutually_exclusive_group()
    planning.add_argument("--laya-plan", type=Path, help="v2 JSON mission plan; bridge generates live candidates")
    planning.add_argument("--codex-plan", action="store_true", help="ask Codex for a plan from the first live situation")
    parser.add_argument("--codex-timeout", type=float, default=180)
    parser.add_argument("--codex-model", help="optional Codex CLI model override")
    parser.add_argument("--knowledge-root", type=Path,
                        help="directory containing human skills and generated experience")
    parser.add_argument("--no-reflection", action="store_true",
                        help="disable the final Codex reflection turn")
    parser.add_argument("--codex-interval", type=int, default=600,
                        help="simulation seconds between Codex OODA cycles")
    parser.add_argument("--mission-brief", type=Path,
                        help="UTF-8 text briefing; overrides the scenario description")
    parser.add_argument("--side-brief", action="append", default=[], metavar="SIDE=PATH",
                        help="side-specific UTF-8 briefing; repeat for multiple sides")
    parser.add_argument("--laya-url", default="http://127.0.0.1:18080")
    parser.add_argument("--laya-timeout", type=float, default=30)
    parser.add_argument("--laya-max-actions", type=int,
                        help="optional total action cap; static plans default to one")
    args = parser.parse_args()
    sides = selected_sides(args, parser)
    brief_paths = side_brief_paths(args.side_brief, sides, parser)
    if brief_paths and not args.codex_plan:
        parser.error("--side-brief requires --codex-plan")
    decision_interval = args.decision_interval if args.decision_interval is not None else (
        60 if args.codex_plan else 300)
    if decision_interval < 1:
        parser.error("--decision-interval must be positive")

    global RUN_LOG
    run_dir = Path(__file__).resolve().parent / "headless_runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True)
    RUN_LOG = run_dir / "events.jsonl"
    start_details = {"scenario": args.scenario, "sides": sides, "run_dir": str(run_dir)}
    if len(sides) == 1:
        start_details["side"] = sides[0]
    record("start", **start_details)

    config = LingYiConfig.get_server_config({})
    game = config["game_server"]
    if game["mode"] != 4:
        raise RuntimeError("LingYi is not configured for local single-player mode")
    engine_path = Path(game["server"]["lypath"]) / "Plat.exe"
    scenario_path = engine_path.parent.parent / "Scenarios" / (args.scenario + ".scen")
    if not engine_path.is_file() or not scenario_path.is_file():
        raise FileNotFoundError(f"Engine or scenario missing: {engine_path}, {scenario_path}")

    game["RL_agents"] = {}
    planners = []
    config["rule_config"] = configure_rule_agents(
        args, sides, run_dir, scenario_path, brief_paths, planners)
    game["simulate"].update(
        scenario=args.scenario,
        episodes=1,
        compression=args.compression,
        decision_interval=decision_interval,
        training=False,
        testing=True,
        need_all_side=False,
        save_situation_type=0,
    )
    record("configuration_ready", engine=str(engine_path), scenario_file=str(scenario_path),
           sides=sides, decision_interval=decision_interval)

    started = time.monotonic()
    parent = psutil.Process()
    ending = "error"
    room_id = None
    try:
        env = Deduction(config)
        room_id = find_engine_room(parent, engine_path)
        result = env.reset(1)
        if not room_id:
            room_id = find_engine_room(parent, engine_path)
        if room_id:
            record("engine_room", room_id=room_id)
        record("reset_result", value=result)
        if result != 1:
            raise RuntimeError(f"LingYi reset returned {result}; scenario did not start")

        calls = 0
        while not env.is_done():
            if args.max_wall_minutes > 0 and time.monotonic() - started > args.max_wall_minutes * 60:
                record("wall_timeout", step_calls=calls, minutes=args.max_wall_minutes)
                ending = "wall_timeout"
                return 2
            env.step({})
            calls += 1
            if calls == 1 or calls % 10 == 0:
                record("step_completed", step_calls=calls, wall_seconds=round(time.monotonic() - started, 1))

        record("scenario_complete", step_calls=calls, wall_seconds=round(time.monotonic() - started, 1))
        ending = "scenario_complete"
        return 0
    except KeyboardInterrupt:
        ending = "interrupted"
        record("run_interrupted")
        raise
    except Exception as error:
        record("run_error", type=type(error).__name__, message=str(error))
        raise
    finally:
        if not room_id:
            room_id = find_engine_room(parent, engine_path)
        # The vendor framework leaves its Plat.exe child running after the round ends.
        for child in parent.children(recursive=True):
            try:
                if child.name().lower() != "plat.exe":
                    continue
                if Path(child.exe()).resolve() != engine_path.resolve():
                    continue
                child.terminate()
                try:
                    child.wait(timeout=5)
                except psutil.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                record("engine_stopped", pid=child.pid)
            except psutil.NoSuchProcess:
                continue
        finish_scores(engine_path.parent.parent, room_id, args.scenario, ending, run_dir)
        if args.codex_plan and not args.no_reflection:
            finish_reflections(planners, RUN_LOG, ending)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        record("error", type=type(error).__name__, message=str(error))
        raise
