"""Knowledge and reflection checks using fake Codex and engine processes only."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_knowledge import (REFLECTION_FOCUS, REFLECTION_TEMPLATE, experience_directory, initialize_workspace,
                             load_knowledge, reflection_evidence, workspace_directory)
from codex_mission_planner import CodexMissionPlanner
from headless_scenario_run import finish_reflections, main


class KnowledgeReflectionTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch("codex_mission_planner.DEFAULT_ROOT", Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_workspace_is_shared_between_runs_but_not_sides_or_scenarios(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("codex_mission_planner.shutil.which", return_value="codex.cmd"):
                first = CodexMissionPlanner("scene", "red", root / "run1", lambda *a, **k: None,
                                            knowledge_root=root / "knowledge")
                second = CodexMissionPlanner("scene", "red", root / "run2", lambda *a, **k: None,
                                             knowledge_root=root / "knowledge")
                enemy = CodexMissionPlanner("scene", "blue", root / "run2", lambda *a, **k: None,
                                            knowledge_root=root / "knowledge")
                other = CodexMissionPlanner("other", "red", root / "run2", lambda *a, **k: None,
                                            knowledge_root=root / "knowledge")
            self.assertEqual(first.workspace, second.workspace)
            self.assertNotEqual(first.workspace, enemy.workspace)
            self.assertNotEqual(first.workspace, other.workspace)
            self.assertIsNone(second.session_id)
            self.assertNotEqual(first.run_dir, second.run_dir)

    def test_workspace_preserves_human_edits_and_imports_eligible_legacy_experience(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "skills" / "human" / "SKILL.md"
            source.parent.mkdir(parents=True)
            source.write_text("initial guidance", encoding="utf-8")
            old = experience_directory(root, "scene", "red")
            old.mkdir(parents=True)
            (old / "good.md").write_text('---\nreflection_focus: "%s"\n---\ncombat lesson' % REFLECTION_FOCUS,
                                         encoding="utf-8")
            (old / "legacy.md").write_text("engineering legacy", encoding="utf-8")
            workspace = initialize_workspace(root, "scene", "red")
            local = workspace / "skills" / "human" / "SKILL.md"
            local.write_text("HUMAN EDIT", encoding="utf-8")
            source.write_text("changed seed", encoding="utf-8")
            native = workspace / ".agents" / "skills" / "extra" / "SKILL.md"
            native.parent.mkdir(parents=True)
            native.write_text("NATIVE GUIDANCE", encoding="utf-8")
            (workspace / "experience" / "MEMORY.md").write_text("MANUAL MEMORY", encoding="utf-8")
            initialize_workspace(root, "scene", "red")
            context, paths = load_knowledge(root, "scene", "red")
            for value in ("HUMAN EDIT", "NATIVE GUIDANCE", "MANUAL MEMORY", "combat lesson"):
                self.assertIn(value, context)
            self.assertNotIn("changed seed", context)
            self.assertNotIn("engineering legacy", context)
            self.assertTrue((old / "good.md").exists())
            self.assertTrue(all(workspace in Path(path).parents for path in paths))

    def test_concurrent_initialization_does_not_replace_guidance_or_leave_partial_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "skills" / "human" / "SKILL.md"
            source.parent.mkdir(parents=True)
            text = "long human guidance\n" * 1000
            source.write_text(text, encoding="utf-8")
            with ThreadPoolExecutor(max_workers=4) as pool:
                paths = list(pool.map(lambda _: initialize_workspace(root, "scene", "red"), range(8)))
            self.assertEqual(len(set(paths)), 1)
            self.assertEqual((paths[0] / "skills" / "human" / "SKILL.md").read_text(encoding="utf-8"), text)
            self.assertEqual(list(paths[0].rglob("*.tmp")), [])
            self.assertEqual(json.loads((paths[0] / "workspace.json").read_text(encoding="utf-8"))["side"], "red")
    def test_guidance_is_injected_into_the_first_planning_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "knowledge" / "skills" / "human" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("HUMAN GUIDANCE SENTINEL", encoding="utf-8")
            with patch("codex_mission_planner.shutil.which", return_value="codex.cmd"):
                planner = CodexMissionPlanner("scene", "red", root / "run", lambda *a, **k: None,
                                             knowledge_root=root / "knowledge")
            units = {name: {"category": "Aircraft", "dbid": 1, "lat": 1, "lon": 1,
                            "name": name, "mission": ""} for name in ("a", "b")}
            proposed = {"objective": "test", "task": {"family": "patrol", "subtype": "air",
                        "units": {"category": "Aircraft", "counts": [2]},
                        "areas": [{"id": "zone", "center": "selected_units", "half_span_deg": 0.1}]}}

            def fake_codex(command, **kwargs):
                self.assertIn("HUMAN GUIDANCE SENTINEL", kwargs["input"])
                self.assertEqual(Path(command[command.index("-C") + 1]), planner.workspace)
                self.assertEqual(kwargs["cwd"], planner.workspace)
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"plan_json": json.dumps(proposed)}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=json.dumps({"type": "thread.started",
                    "thread_id": "12345678-1234-4234-8234-123456789abc"}))

            with patch("codex_mission_planner.run_codex", side_effect=fake_codex):
                accepted = planner("red", 0, (units, {}))
            self.assertEqual(accepted["side"], "red")
            self.assertIn("HUMAN GUIDANCE SENTINEL",
                          (root / "run" / "knowledge_context.md").read_text(encoding="utf-8"))

    def test_loads_guidance_and_only_latest_same_scenario_side_experience(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skills" / "human" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("human guidance", encoding="utf-8")
            for scenario, side in (("scene", "red"), ("other", "red"), ("scene", "blue")):
                library = experience_directory(root, scenario, side)
                library.mkdir(parents=True)
                for index in range(4):
                    (library / ("run_%d.md" % index)).write_text(
                        '---\nreflection_focus: "%s"\n---\n%s/%s/%d' %
                        (REFLECTION_FOCUS, scenario, side, index), encoding="utf-8")
                (library / "run_9_legacy.md").write_text("OLD ENGINEERING REFLECTION", encoding="utf-8")
            context, paths = load_knowledge(root, "scene", "red")
            self.assertIn("human guidance", context)
            self.assertIn("scene/red/3", context)
            self.assertNotIn("scene/red/0", context)
            self.assertNotIn("scene/blue", context)
            self.assertNotIn("other/red", context)
            self.assertNotIn("OLD ENGINEERING REFLECTION", context)
            self.assertEqual(len(paths), 4)

    def test_evidence_filters_private_observations_and_keeps_source_line_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            events = [
                {"event": "command_observed", "side": "red", "matched": False},
                {"event": "command_observed", "side": "blue", "matched": True},
                {"event": "unit_loss", "side": "blue", "observed_by": "red", "unit_id": "seen"},
                {"event": "unit_loss", "side": "red", "observed_by": "blue", "unit_id": "private"},
            ]
            path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
            evidence = reflection_evidence(path, "red")
            self.assertEqual(evidence["assignment_checks"], {"unmatched": 1})
            self.assertEqual([row["evidence_id"] for row in evidence["details"]],
                             ["events.jsonl:L1", "events.jsonl:L3"])
            self.assertNotIn("private", json.dumps(evidence))

    def test_gameplay_evidence_keeps_units_and_battle_actions_without_engineering_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = [
                {"event": "codex_plan_rejected", "side": "red", "reason": "schema error sentinel"},
                {"event": "laya_hold", "side": "red", "reason": "truncation sentinel"},
                {"event": "command_applied", "side": "red", "unit_ids": ["fighter"],
                 "mission_ids": ["m1"], "reassigned_from": {"fighter": "m0"}},
                {"event": "launch_observed", "side": "red", "requested": 2, "airborne": 1,
                 "status": "shortfall", "simulation_seconds": 600},
                {"event": "run_error", "message": "Python exception sentinel"},
            ]
            path = root / "events.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in events), encoding="utf-8")
            summary = {"side": "red", "simulation_seconds": 0,
                       "units": [{"id": "fighter", "name": "战斗机一号", "category": "Aircraft"}],
                       "contacts": [{"id": "visible", "name": "本方可见目标"}]}
            (root / "codex_situation_round_001.json").write_text(json.dumps(summary), encoding="utf-8")
            (root / "codex_situation_round_002.json").write_text(
                json.dumps({"side": "blue", "units": [{"id": "private", "name": "ENEMY PRIVATE"}]}),
                encoding="utf-8")
            evidence = reflection_evidence(path, "red", root)
            serialized = json.dumps(evidence, ensure_ascii=False)
            self.assertIn("战斗机一号", serialized)
            self.assertIn("本方可见目标", serialized)
            self.assertEqual(evidence["details"][1]["reassigned_units"], ["战斗机一号"])
            self.assertEqual(evidence["details"][2]["airborne"], 1)
            self.assertEqual(evidence["details"][2]["evidence_id"], "events.jsonl:L4")
            for private in ("schema error sentinel", "truncation sentinel", "Python exception sentinel", "ENEMY PRIVATE"):
                self.assertNotIn(private, serialized)

    def test_deployment_resolves_game_targets_and_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = {"event": "codex_plan_ready", "side": "red", "simulation_seconds": 600,
                     "plan": {"objective": "阻止敌舰接近登陆区", "task": {
                         "family": "strike", "subtype": "ship",
                         "units": {"category": "Aircraft", "counts": [2]},
                         "target": {"contact_ids": ["target1"]},
                         "settings": {"flight_size": 2, "active": True}}}}
            path = root / "events.jsonl"
            path.write_text(json.dumps(event), encoding="utf-8")
            (root / "codex_situation_round_001.json").write_text(json.dumps({
                "side": "red", "units": [], "contacts": [{"id": "target1", "name": "本方识别的敌登陆舰"}]}),
                encoding="utf-8")
            deployment = reflection_evidence(path, "red", root)["details"][0]["deployment"]
            self.assertEqual(deployment["打击目标"]["名称"], ["本方识别的敌登陆舰"])
            self.assertEqual(deployment["任务设置"], {"飞行编组数量": 2, "任务启用": True})

    def test_reflection_receives_only_its_own_authoritative_score_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            own = {"side": "red", "total": 100, "initial": 0, "detail_total": 100,
                   "unattributed": 0, "sources": [{"reason": "登陆得分", "points": 100}]}
            other = dict(own, side="blue", sources=[{"reason": "OPPONENT PRIVATE SOURCE"}])
            event = {"event": "score_summary", "report": {"final": True, "sides": [own, other],
                     "json_path": "score_summary.json", "notes": []}}
            path = root / "events.jsonl"
            path.write_text(json.dumps(event), encoding="utf-8")
            evidence = reflection_evidence(path, "red")
            self.assertEqual(evidence["outcome"]["own_score"]["total"], 100)
            self.assertNotIn("OPPONENT PRIVATE SOURCE", json.dumps(evidence))

    def test_reflection_resumes_session_and_saves_markdown_for_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run_001" / "side_01"
            (run / "codex_workspace").mkdir(parents=True)
            events = run.parent / "events.jsonl"
            events.write_text(json.dumps({"event": "command_observed", "side": "red",
                                          "matched": False}), encoding="utf-8")
            with patch("codex_mission_planner.shutil.which", return_value="codex.cmd"):
                planner = CodexMissionPlanner("scene", "red", run, lambda *a, **k: None,
                                             knowledge_root=root / "knowledge")
            planner.session_id = "12345678-1234-4234-8234-123456789abc"
            markdown = "# 推演反思\n\n" + "\n\n".join(
                line + "\n本局证据 events.jsonl:L1：任务归属未匹配，原因待验证。"
                for line in REFLECTION_TEMPLATE.read_text(encoding="utf-8").splitlines()
                if line.startswith("## "))

            def fake_codex(command, **kwargs):
                self.assertIn("resume", command)
                self.assertIn("notify=[]", command)
                self.assertIn(planner.session_id, command)
                self.assertEqual(Path(command[command.index("-C") + 1]), planner.workspace)
                self.assertIn("wall_timeout", kwargs["input"])
                self.assertIn("events.jsonl:L1", kwargs["input"])
                self.assertIn("codex_reflection_output_schema.json",
                              command[command.index("--output-schema") + 1])
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"reflection_markdown": markdown}), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with patch("codex_mission_planner.run_codex", side_effect=fake_codex) as invoke:
                planner.reflect(events, "wall_timeout")
                planner.reflect(events, "wall_timeout")
            self.assertEqual(invoke.call_count, 1)
            document = (run / "reflection.md").read_text(encoding="utf-8")
            self.assertEqual(len(list((planner.workspace / "experience").glob("*.md"))), 1)
            self.assertIn('ending: "wall_timeout"', document)
            self.assertIn('status: "unreviewed"', document)
            context, _ = load_knowledge(root / "knowledge", "scene", "red")
            self.assertIn("## 值得复用的经验", context)
            other_context, _ = load_knowledge(root / "knowledge", "scene", "blue")
            self.assertEqual(other_context, "")

    def test_reflection_failure_does_not_prevent_other_side(self):
        with patch("codex_mission_planner.shutil.which", return_value="codex.cmd"):
            with tempfile.TemporaryDirectory() as directory:
                events = []
                planner = CodexMissionPlanner("s", "red", directory,
                                             lambda name, **kw: events.append(name))
                planner.reflect(Path(directory) / "missing.jsonl", "error")
                self.assertEqual(events, ["codex_reflection_skipped"])
        from unittest.mock import Mock
        failed, other = Mock(), Mock()
        failed.reflect.side_effect = TimeoutError("timeout")
        finish_reflections([failed, other], "events.jsonl", "error")
        failed.record.assert_called_once()
        other.reflect.assert_called_once_with("events.jsonl", "error")

    def test_invalid_response_is_not_published_as_experience(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "codex_workspace").mkdir()
            events = root / "events.jsonl"
            events.write_text("", encoding="utf-8")
            with patch("codex_mission_planner.shutil.which", return_value="codex.cmd"):
                planner = CodexMissionPlanner("s", "red", root, lambda *a, **k: None,
                                             knowledge_root=root / "knowledge")
            planner.session_id = "session"

            def fake_codex(command, **kwargs):
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text('{"reflection_markdown":"missing sections"}', encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with patch("codex_mission_planner.run_codex", side_effect=fake_codex):
                with self.assertRaises(ValueError):
                    planner.reflect(events, "scenario_complete")
            self.assertFalse((root / "reflection.md").exists())
            self.assertFalse((root / "knowledge" / "experience").exists())

    def test_runner_reflects_after_engine_cleanup_for_each_ending(self):
        from unittest.mock import Mock
        for ending in ("scenario_complete", "wall_timeout", "error"):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                engine = root / "lyengine" / "Plat.exe"
                engine.parent.mkdir()
                engine.touch()
                scenario = root / "Scenarios" / "test.scen"
                scenario.parent.mkdir()
                scenario.touch()
                config = {"game_server": {"mode": 4, "server": {"lypath": str(engine.parent)},
                                          "simulate": {}}}
                game = Mock()
                game.reset.return_value = 1
                game.is_done.return_value = ending == "scenario_complete"
                game.step.side_effect = RuntimeError("engine failure") if ending == "error" else None
                child = Mock(pid=123)
                child.name.return_value = "Plat.exe"
                child.exe.return_value = str(engine)
                parent = Mock()
                parent.children.return_value = [child]
                order = []
                child.terminate.side_effect = lambda: order.append("cleanup")

                def configure(*args):
                    args[-1].append("planner")
                    return {}

                clock = [0, 61 if ending == "wall_timeout" else 0]
                argv = ["runner", "--scenario", "test", "--codex-plan", "--max-wall-minutes", "1"]
                # Mock run-directory creation to keep test artifacts out of headless_runs.
                with patch("sys.argv", argv), \
                     patch("headless_scenario_run.LingYiConfig.get_server_config", return_value=config), \
                     patch("headless_scenario_run.configure_rule_agents", side_effect=configure), \
                     patch("headless_scenario_run.Deduction", return_value=game), \
                     patch("headless_scenario_run.psutil.Process", return_value=parent), \
                     patch("headless_scenario_run.time.monotonic", side_effect=clock), \
                     patch("headless_scenario_run.record"), \
                     patch("headless_scenario_run.__file__", str(root / "runner.py")), \
                     patch("headless_scenario_run.finish_reflections",
                           side_effect=lambda *a: order.append(a[-1])):
                    if ending == "error":
                        with self.assertRaisesRegex(RuntimeError, "engine failure"):
                            main()
                    else:
                        self.assertEqual(main(), 2 if ending == "wall_timeout" else 0)
                self.assertEqual(order, ["cleanup", ending])


if __name__ == "__main__":
    unittest.main()
