"""Real subprocess regression checks, without invoking Codex, Laya, or Plat."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import psutil

from codex_process import run_codex


class CodexProcessTest(unittest.TestCase):
    def test_utf8_input_output_and_nonzero_exit_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = ("import sys; text=sys.stdin.buffer.read().decode('utf-8'); "
                      "sys.stdout.buffer.write(text.encode('utf-8')); "
                      "sys.stderr.write('diagnostic'); sys.exit(7)")
            reply = run_codex([sys.executable, "-c", script], input="中文态势",
                              timeout=10, cwd=root, log_path=root / "response.json")
            self.assertEqual(reply.returncode, 7)
            self.assertEqual(reply.stdout, "中文态势")
            self.assertEqual(reply.stderr, "diagnostic")
            self.assertEqual((root / "response.stdout.log").read_text(encoding="utf-8"), "中文态势")

    def test_timeout_kills_descendant_that_inherits_output_handles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "child.json"
            script = root / "spawn.py"
            script.write_text(
                "import json, pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid': child.pid}))\n"
                "time.sleep(120)\n", encoding="utf-8")
            started = time.monotonic()
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_codex([sys.executable, str(script), str(ready)], input="",
                              timeout=2, cwd=root, log_path=root / "timeout.json")
                self.assertLess(time.monotonic() - started, 9)
                self.assertTrue(ready.exists(), "test child did not start")
                child_id = json.loads(ready.read_text())["pid"]
                self.assertFalse(psutil.pid_exists(child_id))
            finally:
                if ready.exists():
                    try:
                        psutil.Process(json.loads(ready.read_text())["pid"]).kill()
                    except psutil.NoSuchProcess:
                        pass

    @unittest.skipUnless(os.name == "nt", "Windows batch launcher regression")
    def test_batch_launcher_timeout_cleans_its_node_like_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "launcher.cmd"
            ready = root / "pid.txt"
            script = root / "sleep.py"
            script.write_text("import os,pathlib,time\n"
                              "pathlib.Path('pid.txt').write_text(str(os.getpid()))\n"
                              "time.sleep(120)\n", encoding="utf-8")
            launcher.write_text('@echo off\n"%s" "%s"\n' % (sys.executable, script), encoding="utf-8")
            started = time.monotonic()
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_codex([str(launcher)], input="", timeout=2, cwd=root,
                              log_path=root / "batch.json")
                self.assertLess(time.monotonic() - started, 9)
                self.assertTrue(ready.exists())
                self.assertFalse(psutil.pid_exists(int(ready.read_text())))
            finally:
                if ready.exists():
                    try:
                        psutil.Process(int(ready.read_text())).kill()
                    except psutil.NoSuchProcess:
                        pass


if __name__ == "__main__":
    unittest.main()
