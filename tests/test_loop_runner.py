import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import loop_runner


class LoopRunnerTests(unittest.TestCase):
    def make_workspace(
        self,
        task_status="todo",
        verifier_command=None,
        max_attempts=2,
        agent_command=None,
        review_on_success=False,
        allowed_paths=None,
    ):
        temp = TemporaryDirectory()
        root = Path(temp.name)
        loop_dir = root / "loop"
        loop_dir.mkdir()
        (loop_dir / "project.md").write_text(
            "# Project\n\nGoal: Build a small verified project.\n", encoding="utf-8"
        )
        (loop_dir / "tasks.md").write_text(
            f"- [{task_status}] T001: Make tests pass\n"
            "  Scope: only touch src and tests\n",
            encoding="utf-8",
        )
        command = verifier_command or f"{sys.executable} -c \"print('ok')\""
        default_agent = f"{sys.executable} -c \"print('agent ran')\""
        configured_agent = agent_command or default_agent
        configured_allowed = allowed_paths or ["src", "tests", "loop"]
        allowed_yaml = "\n".join(f"                  - {path}" for path in configured_allowed)
        (loop_dir / "config.yaml").write_text(
            textwrap.dedent(
                f"""\
                max_attempts_per_task: {max_attempts}
                max_runtime_seconds_per_task: 60
                max_changed_files: 3
                review_on_success: {str(review_on_success).lower()}
                agent_command: "{configured_agent}"
                verifier_commands:
                  - name: tests
                    command: "{command}"
                allowed_paths:
{allowed_yaml}
                forbidden_paths:
                  - .git
                """
            ),
            encoding="utf-8",
        )
        (loop_dir / "state.json").write_text("{}", encoding="utf-8")
        return temp, root, loop_dir

    def test_dry_run_selects_first_todo_and_writes_prompt_without_running_agent(self):
        temp, root, loop_dir = self.make_workspace()
        self.addCleanup(temp.cleanup)

        result = loop_runner.run_once(root=root, loop_dir=loop_dir, dry_run=True)

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.task.id, "T001")
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["current_task_id"], "T001")
        prompt_files = list((loop_dir / "runs").glob("*/prompt.md"))
        self.assertEqual(len(prompt_files), 1)
        prompt = prompt_files[0].read_text(encoding="utf-8")
        self.assertIn("Make tests pass", prompt)
        self.assertIn("Remaining attempts: 2", prompt)
        self.assertIn("- [todo] T001: Make tests pass", (loop_dir / "tasks.md").read_text())

    def test_passing_verifier_marks_task_done(self):
        temp, root, loop_dir = self.make_workspace()
        self.addCleanup(temp.cleanup)

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "done")
        self.assertIn("- [done] T001: Make tests pass", (loop_dir / "tasks.md").read_text())
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "verifiers_passed")

    def test_failing_verifier_retries_until_budget_then_blocks_task(self):
        command = f"{sys.executable} -c \"import sys; print('bad'); sys.exit(7)\""
        temp, root, loop_dir = self.make_workspace(verifier_command=command, max_attempts=2)
        self.addCleanup(temp.cleanup)

        first = loop_runner.run_once(root=root, loop_dir=loop_dir)
        second = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(first.status, "retry")
        self.assertEqual(second.status, "blocked")
        self.assertIn("- [blocked] T001: Make tests pass", (loop_dir / "tasks.md").read_text())
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["attempts"]["T001"], 2)
        self.assertEqual(state["stop_reason"], "max_attempts_exhausted")
        self.assertIn("bad", state["last_verifier_output"])

    def test_changed_file_limit_blocks_before_verifiers(self):
        command = (
            f"{sys.executable} -c \"from pathlib import Path; Path('src').mkdir(); "
            "[Path('src/extra' + str(i) + '.txt').write_text(str(i)) for i in range(4)]\""
        )
        temp, root, loop_dir = self.make_workspace(agent_command=command)
        self.addCleanup(temp.cleanup)

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "changed_file_limit_exceeded")

    def test_changed_file_limit_ignores_files_dirty_before_attempt(self):
        command = (
            f"{sys.executable} -c \"from pathlib import Path; Path('src').mkdir(); "
            "Path('src/new.txt').write_text('new')\""
        )
        temp, root, loop_dir = self.make_workspace(agent_command=command)
        self.addCleanup(temp.cleanup)
        for index in range(10):
            (root / f"preexisting{index}.txt").write_text("dirty", encoding="utf-8")

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "done")
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "verifiers_passed")

    def test_agent_command_allows_unrelated_braces(self):
        command = f"{sys.executable} -c \"print('{{not_a_loop_placeholder}}')\""
        temp, root, loop_dir = self.make_workspace(agent_command=command)
        self.addCleanup(temp.cleanup)

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "done")

    def test_forbidden_path_change_blocks_task(self):
        command = f"{sys.executable} -c \"from pathlib import Path; Path('.env').write_text('secret')\""
        temp, root, loop_dir = self.make_workspace(agent_command=command)
        self.addCleanup(temp.cleanup)

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "path_policy_violation")
        self.assertIn(".env", state["path_policy_violations"])

    def test_out_of_allowed_path_change_blocks_task(self):
        command = f"{sys.executable} -c \"from pathlib import Path; Path('outside.txt').write_text('x')\""
        temp, root, loop_dir = self.make_workspace(agent_command=command)
        self.addCleanup(temp.cleanup)

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "path_policy_violation")
        self.assertIn("outside.txt", state["path_policy_violations"])

    def test_exhausted_budget_blocks_before_agent_runs(self):
        command = f"{sys.executable} -c \"from pathlib import Path; Path('src').mkdir(); Path('src/agent-ran').write_text('x')\""
        temp, root, loop_dir = self.make_workspace(agent_command=command, max_attempts=1)
        self.addCleanup(temp.cleanup)
        (loop_dir / "state.json").write_text(
            json.dumps({"attempts": {"T001": 1}}), encoding="utf-8"
        )

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        self.assertFalse((root / "src" / "agent-ran").exists())
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "max_attempts_exhausted")

    def test_agent_failure_retries_without_marking_success(self):
        command = f"{sys.executable} -c \"import sys; print('agent failed'); sys.exit(9)\""
        temp, root, loop_dir = self.make_workspace(agent_command=command, max_attempts=2)
        self.addCleanup(temp.cleanup)

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "retry")
        self.assertIn("- [running] T001: Make tests pass", (loop_dir / "tasks.md").read_text())
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "agent_failed_retry_available")

    def test_agent_timeout_blocks_with_durable_state_and_log(self):
        command = f"{sys.executable} -c \"import time; time.sleep(2)\""
        temp, root, loop_dir = self.make_workspace(agent_command=command, max_attempts=1)
        self.addCleanup(temp.cleanup)
        config = (loop_dir / "config.yaml").read_text(encoding="utf-8")
        (loop_dir / "config.yaml").write_text(
            config.replace("max_runtime_seconds_per_task: 60", "max_runtime_seconds_per_task: 1"),
            encoding="utf-8",
        )

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "agent_timeout")
        agent_logs = list((loop_dir / "runs").glob("*/agent.log"))
        self.assertEqual(len(agent_logs), 1)
        self.assertIn("timed out", agent_logs[0].read_text(encoding="utf-8"))

    def test_timeout_kills_child_process_group(self):
        child_script = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', "
            "\"import time; from pathlib import Path; time.sleep(2); Path('src/late.txt').write_text('late')\"]); "
            "time.sleep(5)"
        )
        command = f"{sys.executable} -c {child_script!r}"
        temp, root, loop_dir = self.make_workspace(agent_command=command, max_attempts=1)
        self.addCleanup(temp.cleanup)
        (root / "src").mkdir()
        config = (loop_dir / "config.yaml").read_text(encoding="utf-8")
        (loop_dir / "config.yaml").write_text(
            config.replace("max_runtime_seconds_per_task: 60", "max_runtime_seconds_per_task: 1"),
            encoding="utf-8",
        )

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)
        import time
        time.sleep(3)

        self.assertEqual(result.status, "blocked")
        self.assertFalse((root / "src" / "late.txt").exists())

    def test_multiple_running_tasks_are_invalid_queue_state(self):
        temp, root, loop_dir = self.make_workspace(task_status="running")
        self.addCleanup(temp.cleanup)
        (loop_dir / "tasks.md").write_text(
            "- [running] T001: First task\n"
            "- [running] T002: Second task\n",
            encoding="utf-8",
        )

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "multiple_running_tasks")

    def test_review_on_success_marks_review(self):
        temp, root, loop_dir = self.make_workspace(review_on_success=True)
        self.addCleanup(temp.cleanup)

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "review")
        self.assertIn("- [review] T001: Make tests pass", (loop_dir / "tasks.md").read_text())

    def test_loop_control_file_changes_are_enforced(self):
        command = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "Path('loop/config.yaml').write_text('max_attempts_per_task: 99')\""
        )
        temp, root, loop_dir = self.make_workspace(
            agent_command=command,
            allowed_paths=["src", "tests"],
        )
        self.addCleanup(temp.cleanup)

        result = loop_runner.run_once(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "path_policy_violation")
        self.assertIn("loop/config.yaml", state["path_policy_violations"])

    def test_diff_log_excludes_generated_artifacts_for_configured_loop_dir(self):
        temp, root, loop_dir = self.make_workspace()
        self.addCleanup(temp.cleanup)
        custom_loop = root / "custom-loop"
        loop_dir.rename(custom_loop)
        subprocess.run("git init -q", cwd=root, shell=True, check=True, stdout=subprocess.PIPE)
        (custom_loop / "runs" / "old").mkdir(parents=True)
        (custom_loop / "review-runs" / "old").mkdir(parents=True)
        (custom_loop / "runs" / "old" / "tracked.txt").write_text("old", encoding="utf-8")
        (custom_loop / "review-runs" / "old" / "tracked.txt").write_text("old", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "real.txt").write_text("old", encoding="utf-8")
        subprocess.run("git add .", cwd=root, shell=True, check=True, stdout=subprocess.PIPE)
        subprocess.run(
            "git -c user.email=test@example.com -c user.name=Test commit -m initial",
            cwd=root,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
        )
        (custom_loop / "runs" / "old" / "tracked.txt").write_text("new", encoding="utf-8")
        (custom_loop / "review-runs" / "old" / "tracked.txt").write_text("new", encoding="utf-8")
        (root / "src" / "real.txt").write_text("new", encoding="utf-8")
        run_dir = custom_loop / "runs" / "current"
        run_dir.mkdir(parents=True)

        loop_runner.write_diff_log(root, custom_loop, run_dir)

        diff_log = (run_dir / "diff.log").read_text(encoding="utf-8")
        self.assertIn("src/real.txt", diff_log)
        self.assertNotIn("custom-loop/runs/old/tracked.txt", diff_log)
        self.assertNotIn("custom-loop/review-runs/old/tracked.txt", diff_log)

    def test_cli_dry_run_uses_default_loop_directory(self):
        temp, root, loop_dir = self.make_workspace()
        self.addCleanup(temp.cleanup)

        completed = subprocess.run(
            [sys.executable, str(Path(loop_runner.__file__)), "--dry-run"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("dry_run", completed.stdout)


if __name__ == "__main__":
    unittest.main()
