"""Ask the installed Codex CLI for one mission plan from a live side situation.

Codex is a separate process. The LingYi Python 3.9 environment needs no OpenAI SDK.
"""

import json
import math
import shutil
import subprocess
import uuid
from collections import Counter
from pathlib import Path

from codex_knowledge import (DEFAULT_ROOT, REFLECTION_FOCUS, REFLECTION_SCHEMA, REFLECTION_TEMPLATE,
                             build_reflection_prompt, initialize_workspace, load_knowledge,
                             publish_experience, reflection_evidence, validate_reflection)
from codex_process import run_codex
from laya_mission_protocol import air_availability, current_loadout, generate_candidates, validate_plan


SCHEMA = Path(__file__).with_name("codex_plan_output_schema.json")
FAMILIES = ("patrol", "strike", "support", "ferry", "cargo", "mine", "mine_clear")


def summarize_situation(side, scenario, sim_seconds, situation):
    """Bound the input size while preserving the IDs needed for target binding."""
    units, contacts = situation
    unit_rows = []
    valid_units = ((guid, unit) for guid, unit in units.items() if isinstance(unit, dict))
    for guid, unit in sorted(valid_units, key=lambda item: (str(item[1].get("category")),
                                                      str(item[1].get("name")), item[0])):
        unit_rows.append({"id": guid, "name": unit.get("name"), "category": unit.get("category"),
                          "type": unit.get("type"), "dbid": unit.get("dbid"),
                          "mission": unit.get("mission"),
                          "lat": unit.get("lat"), "lon": unit.get("lon"), "damage": unit.get("damage"),
                          "availability": air_availability(unit) if unit.get("category") == "Aircraft" else None,
                          "loadout": current_loadout(unit) if unit.get("category") == "Aircraft" else None})
    contact_rows = []
    for guid, contact in sorted(contacts.items()):
        if not isinstance(contact, dict):
            continue
        contact_rows.append({"id": guid, "name": contact.get("Name"), "type": contact.get("Type"),
                             "side": contact.get("Side"), "lat": contact.get("Lat"),
                             "lon": contact.get("Lon")})
    return {"scenario": scenario, "side": side, "simulation_seconds": sim_seconds,
            "unit_counts": dict(Counter(row["category"] for row in unit_rows)),
            "contact_count": len(contact_rows), "units": unit_rows[:120],
            "contacts": contact_rows[:60],
            "truncated_units": max(0, len(unit_rows) - 120),
            "truncated_contacts": max(0, len(contact_rows) - 60)}


def build_prompt(summary, rejected=None, mission_brief="", context=None):
    instructions = """你是灵弈推演的上层任务规划器。仅根据下方本方实时态势，选择一个现在能生成候选的任务，返回协议 v2 的 JSON 计划。
不要调用工具，不要编造单位、目标或基地。只输出工具要求的 plan_json 字符串；其内容必须是 JSON 对象。
任务 family 只能是 patrol、strike、support、ferry、cargo、mine、mine_clear 之一，一次只选一种。
plan 必须有 objective 和 task；系统会填写 version、plan_id、scenario、side 和有效期。
task.units 必须有 category（Aircraft/Ship/Submarine）和 counts（1 到 4 个数量，各在 1..10）。
巡逻必须有 subtype（air/anti_ship/anti_land/anti_mlp/anti_submarine/sead/sea_control）和 areas；
打击必须有 subtype（air/land/ship/submarine）和 target；
转场必须有 destination；支援、运输、布雷、扫雷必须有 areas，且不能有 subtype。
区域可用 [{"id":"zone1","center":"selected_units","half_span_deg":0.15,"offset_deg":[0.2,0]}]；
目标可用 {"contact_ids":["态势中的目标 ID"]}；目的地可用 {"unit_ids":["态势中的本方基地 ID"]}。
飞机筛选 aircraft_type 可用 fighter（type=2001）。飞机任务必须设置 pair_selection=true，counts 列出允许 Laya 选择的双机档位（例如 [2,4,6,8]）；Codex 只决定上限和资源约束，具体投入量及双机组由 Laya 决定。
默认不要抽调已有任务的飞机。若开局飞机均带想定预设任务，且确需抽调，设置 allow_reassignment=true、unassigned_only=false；此前由本智能体创建的任务仍受保护，只有明确调整旧任务时才设置 allow_ai_reassignment=true。reserve_pairs 可以保留 0..4 个双机组。
优先选择有明确匹配单位、能立即生成候选的任务；没有可靠目标和平台能力信息时可选择简单巡逻。不要安排未知货物或假定武器挂载。
如需换装，task.loadout 使用 {"role":"land","allowed_ids":[态势里同机型已出现的挂载DBID],"allow_change":true}。role 可为 air/land/ship/submarine/support/any；打击任务须与 subtype 一致。系统仅允许已观测到的同机型挂载，换装完成且飞机停放可用后才分配任务。没有把握时不要换装。
这是周期性 OODA 决策。结合任务简报、最新态势及上轮执行结果调整计划；已有任务仍有效时，避免重复创建相同任务。只有明确需要改变部署时才重分配单位。
示例 plan_json 内容：{"objective":"建立防空巡逻","task":{"family":"patrol","subtype":"air","units":{"category":"Aircraft","aircraft_type":"fighter","counts":[2,4,6,8],"pair_selection":true,"allow_reassignment":true,"unassigned_only":false,"reserve_pairs":1},"areas":[{"id":"zone1","center":"selected_units","half_span_deg":0.15,"offset_deg":[0.2,0]}],"settings":{}}}
"""
    if rejected:
        instructions += "\n上一份计划未通过本地校验：%s。请修正后重新输出。\n" % rejected
    if mission_brief:
        instructions += "\n想定任务简报（背景资料，以实时态势为准）：\n" + mission_brief[:16000] + "\n"
    if context:
        instructions += "\n上一轮计划与执行反馈：\n" + json.dumps(context, ensure_ascii=False, separators=(",", ":")) + "\n"
    return instructions + "\n当前态势：\n" + json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


def build_update_prompt(summary, context=None):
    """Update the saved Codex conversation without repeating its briefing."""
    feedback = (context or {}).get("last_result")
    return ("继续同一想定的 OODA 循环。沿用首次给出的任务简报、协议和约束；"
            "仅根据以下最新态势更新计划。请仍只输出 plan_json。\n"
            "最近执行反馈：\n" + json.dumps(feedback, ensure_ascii=False, separators=(",", ":")) +
            "\n最新态势：\n" + json.dumps(summary, ensure_ascii=False, separators=(",", ":")))


def _thread_id_from_jsonl(output):
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            value = event.get("thread_id")
            try:
                return str(uuid.UUID(value))
            except (ValueError, TypeError, AttributeError):
                break
    raise RuntimeError("Codex did not report a resumable thread ID")


class CodexMissionPlanner:
    def __init__(self, scenario, side, run_dir, record, timeout=180, model=None,
                 mission_brief="", knowledge_root=None):
        self.scenario = scenario
        self.side = side
        self.run_dir = Path(run_dir).resolve()
        self.record = record
        self.timeout = timeout
        self.model = model
        self.mission_brief = mission_brief
        self.context = None
        self.round = 0
        self.session_id = None
        self.knowledge_root = (Path(knowledge_root) if knowledge_root is not None else DEFAULT_ROOT).resolve()
        self.reflection_finished = False
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Codex timeout must be positive")
        self.executable = shutil.which("codex.cmd") or shutil.which("codex")
        if not self.executable:
            raise FileNotFoundError("Codex CLI is not installed or not on PATH")
        if not SCHEMA.is_file():
            raise FileNotFoundError(str(SCHEMA))
        self.workspace = initialize_workspace(self.knowledge_root, self.scenario, self.side)

    def set_context(self, context):
        self.context = context

    def __call__(self, side, sim_seconds, situation):
        if side != self.side:
            raise ValueError("controlled side mismatch")
        summary = summarize_situation(side, self.scenario, sim_seconds, situation)
        self.round += 1
        round_id = "round_%03d" % self.round
        self.run_dir.mkdir(parents=True, exist_ok=True)
        input_file = self.run_dir / ("codex_situation_%s.json" % round_id)
        input_file.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        prompt = (build_prompt(summary, mission_brief=self.mission_brief, context=self.context)
                  if self.session_id is None else build_update_prompt(summary, self.context))
        if self.session_id is None:
            knowledge, sources = load_knowledge(self.knowledge_root, self.scenario, self.side)
            prompt += knowledge
            (self.run_dir / "knowledge_context.md").write_text(knowledge, encoding="utf-8")
            self.record("codex_knowledge_loaded", sources=sources, characters=len(knowledge))
        (self.run_dir / ("codex_prompt_%s.txt" % round_id)).write_text(prompt, encoding="utf-8")
        workspace = self.workspace
        (self.run_dir / "workspace.json").write_text(
            json.dumps({"workspace": str(workspace), "scenario": self.scenario, "side": self.side},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        rejected = None
        for attempt in (1, 2):
            output = self.run_dir / ("codex_plan_%s_attempt_%d.json" % (round_id, attempt))
            command = [self.executable, "-c", "notify=[]", "-C", str(workspace),
                       "-s", "read-only", "-a", "never"]
            if self.model:
                command.extend(["-m", self.model])
            if self.session_id is None:
                command.extend(["exec", "--skip-git-repo-check", "--json",
                                "--output-schema", str(SCHEMA), "--output-last-message", str(output), "-"])
            else:
                command.extend(["exec", "resume", "--skip-git-repo-check",
                                "--output-schema", str(SCHEMA), "--output-last-message", str(output),
                                self.session_id, "-"])
            turn_prompt = (prompt if attempt == 1 else
                           "上一份计划未通过本地校验：%s。继续使用本轮已提供的态势，修正后只输出 plan_json。" % rejected)
            if attempt > 1:
                (self.run_dir / ("codex_prompt_%s_attempt_%d.txt" % (round_id, attempt))).write_text(
                    turn_prompt, encoding="utf-8")
            self.record("codex_request", round=self.round, attempt=attempt,
                        simulation_seconds=sim_seconds, input_file=str(input_file),
                        session_id=self.session_id)
            try:
                reply = run_codex(command, input=turn_prompt, timeout=self.timeout,
                                  cwd=workspace, log_path=output)
            except subprocess.TimeoutExpired:
                raise TimeoutError("Codex plan request timed out after %ss" % self.timeout)
            if reply.returncode != 0:
                raise RuntimeError("Codex CLI exited with code %d" % reply.returncode)
            if self.session_id is None:
                self.session_id = _thread_id_from_jsonl(reply.stdout)
                (self.run_dir / "codex_session.json").write_text(
                    json.dumps({"thread_id": self.session_id, "scenario": self.scenario,
                                "side": self.side}, ensure_ascii=False, indent=2), encoding="utf-8")
                self.record("codex_session_started", session_id=self.session_id)
            if not output.is_file():
                raise RuntimeError("Codex did not write a plan response")
            try:
                wrapper = json.loads(output.read_text(encoding="utf-8"))
                if not isinstance(wrapper, dict) or not isinstance(wrapper.get("plan_json"), str):
                    raise ValueError("Codex response lacks plan_json")
                proposed = json.loads(wrapper["plan_json"])
                if not isinstance(proposed, dict):
                    raise ValueError("plan_json must contain a JSON object")
                trusted = {"version": 2, "plan_id": "codex_%03d" % self.round, "scenario": self.scenario,
                           "side": self.side}
                for key, value in trusted.items():
                    if key in proposed and proposed[key] != value:
                        raise ValueError("Codex changed trusted %s" % key)
                    proposed[key] = value
                unit_selector = proposed.get("task", {}).get("units", {})
                if unit_selector.get("category") == "Aircraft":
                    unit_selector["pair_selection"] = True
                proposed.setdefault("valid_until_sim_seconds", sim_seconds + 3600)
                validate_plan(proposed)
                candidates = generate_candidates(proposed, situation, sim_seconds)
                if not candidates:
                    raise ValueError("plan produces no candidates in the current situation")
                accepted_file = self.run_dir / ("codex_plan_%s_accepted.json" % round_id)
                accepted_file.write_text(
                    json.dumps(proposed, ensure_ascii=False, indent=2), encoding="utf-8")
                self.record("codex_plan_accepted", round=self.round, attempt=attempt,
                            family=proposed["task"]["family"], candidate_count=len(candidates),
                            output_file=str(output), plan_file=str(accepted_file),
                            session_id=self.session_id)
                return proposed
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                rejected = str(error)
                self.record("codex_plan_rejected", attempt=attempt, reason=rejected)
        raise ValueError("Codex did not produce a valid mission plan after two attempts: %s" % rejected)

    def reflect(self, events_path, ending):
        """One final turn in this side's session; Python alone persists the experience."""
        if self.reflection_finished:
            return
        self.reflection_finished = True
        if self.session_id is None:
            self.record("codex_reflection_skipped", reason="no Codex session was established")
            return
        evidence = reflection_evidence(events_path, self.side, self.run_dir)
        template = REFLECTION_TEMPLATE.read_text(encoding="utf-8")
        prompt = build_reflection_prompt(self.scenario, self.side, ending, evidence, template)
        (self.run_dir / "reflection_evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.run_dir / "codex_reflection_prompt.txt").write_text(prompt, encoding="utf-8")
        output = self.run_dir / "codex_reflection_response.json"
        workspace = self.workspace
        command = [self.executable, "-c", "notify=[]", "-C", str(workspace),
                   "-s", "read-only", "-a", "never"]
        if self.model:
            command.extend(["-m", self.model])
        command.extend(["exec", "resume", "--skip-git-repo-check", "--output-schema",
                        str(REFLECTION_SCHEMA), "--output-last-message", str(output),
                        self.session_id, "-"])
        self.record("codex_reflection_request", session_id=self.session_id, ending=ending)
        reply = run_codex(command, input=prompt, timeout=self.timeout, cwd=workspace, log_path=output)
        if reply.returncode != 0:
            raise RuntimeError("Codex reflection exited with code %d" % reply.returncode)
        wrapper = json.loads(output.read_text(encoding="utf-8"))
        markdown = validate_reflection(wrapper.get("reflection_markdown"), template)
        metadata = {"scenario": self.scenario, "side": self.side, "ending": ending,
                    "run_directory": str(self.run_dir.resolve()), "session_id": self.session_id,
                    "source": "codex_self_reflection", "status": "unreviewed",
                    "reflection_focus": REFLECTION_FOCUS, "workspace": str(workspace)}
        document = "---\n" + "\n".join(
            "%s: %s" % (key, json.dumps(value, ensure_ascii=False))
            for key, value in metadata.items()) + "\n---\n\n" + markdown
        local_path = self.run_dir / "reflection.md"
        local_path.write_text(document, encoding="utf-8")
        # A run directory is already unique; include its side directory when present.
        run_name = (self.run_dir.parent.name + "_" + self.run_dir.name
                    if self.run_dir.name.startswith("side_") else self.run_dir.name)
        experience_path = publish_experience(local_path, workspace,
                                             run_name + "_" + self.session_id + ".md")
        self.record("codex_reflection_saved", path=str(local_path),
                    experience_path=str(experience_path), ending=ending)
