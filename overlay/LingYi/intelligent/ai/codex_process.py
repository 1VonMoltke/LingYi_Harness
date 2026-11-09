"""Run Codex with a bounded wait, including Windows batch launchers and children."""

import subprocess
import tempfile
from pathlib import Path

import psutil


def _kill_tree(process):
    """Capture descendants before killing their parent so they cannot be orphaned."""
    try:
        parent = psutil.Process(process.pid)
        targets = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    # Stop the root first to prevent the launcher creating more descendants.
    for target in [targets[-1]] + list(reversed(targets[:-1])):
        try:
            target.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=3)
    if alive:
        raise RuntimeError("Could not stop Codex process tree: %s" % [item.pid for item in alive])


def run_codex(command, *, input, timeout, cwd, log_path):
    """Use files for all streams: descendants cannot hold a communicate() pipe open.

    subprocess.run(timeout=...) kills only its immediate process. On Windows it
    then drains inherited pipes without a deadline, which can wait on a living
    grandchild forever. File-backed streams plus wait() avoid that second wait.
    """
    log_path = Path(log_path)
    stdout_path = log_path.with_suffix(".stdout.log")
    stderr_path = log_path.with_suffix(".stderr.log")
    with tempfile.TemporaryFile(mode="w+b") as stdin, \
         stdout_path.open("w+b") as stdout, stderr_path.open("w+b") as stderr:
        stdin.write(input.encode("utf-8"))
        stdin.seek(0)
        process = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr,
                                   cwd=str(cwd),
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            process.wait(timeout=timeout)
        except BaseException:
            _kill_tree(process)
            process.wait(timeout=3)
            raise
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(command, process.returncode,
                                           stdout.read().decode("utf-8", errors="replace"),
                                           stderr.read().decode("utf-8", errors="replace"))
