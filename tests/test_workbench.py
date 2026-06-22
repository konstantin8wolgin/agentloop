import json
import sys
import textwrap
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import workbench


class WorkbenchTests(unittest.TestCase):
    def make_root(self):
        temp = TemporaryDirectory()
        root = Path(temp.name)
        (root / "loop").mkdir()
        return temp, root

    def test_goal_contract_requires_acceptance_checks_and_writes_prd(self):
        temp, root = self.make_root()
        self.addCleanup(temp.cleanup)

        contract = workbench.create_goal_contract(
            root=root,
            product="Loop Workbench",
            user="A developer supervising coding agents",
            mvp="Goal-to-run pipeline",
            success_criteria=["Create goal contract", "Run bounded supervisor"],
            acceptance_checks=["python3 -m unittest discover -s tests"],
            out_of_scope=["Web UI"],
            allowed_paths=["src", "tests", "loop"],
            forbidden_paths=[".env", "secrets"],
            stop_rules=["Stop after repeated verifier failure"],
        )

        self.assertEqual(contract["product"], "Loop Workbench")
        goal_path = root / "loop" / "goal.md"
        self.assertTrue(goal_path.exists())
        goal_text = goal_path.read_text(encoding="utf-8")
        self.assertIn("# Goal Contract", goal_text)
        self.assertIn("Loop Workbench", goal_text)
        self.assertIn("python3 -m unittest discover -s tests", goal_text)
        state = json.loads((root / "loop" / "goal.json").read_text(encoding="utf-8"))
        self.assertEqual(state["forbidden_paths"], [".env", "secrets"])

    def test_goal_contract_rejects_missing_acceptance_checks(self):
        temp, root = self.make_root()
        self.addCleanup(temp.cleanup)

        with self.assertRaisesRegex(ValueError, "acceptance"):
            workbench.create_goal_contract(
                root=root,
                product="Loop Workbench",
                user="A developer",
                mvp="Goal-to-run pipeline",
                success_criteria=["Useful"],
                acceptance_checks=[],
                out_of_scope=[],
                allowed_paths=["src"],
                forbidden_paths=[".env"],
                stop_rules=["Stop on risk"],
            )

    def test_context_pack_ingests_local_files_and_urls_as_inspectable_files(self):
        temp, root = self.make_root()
        self.addCleanup(temp.cleanup)
        notes = root / "notes.md"
        notes.write_text("# Notes\n\nImportant local context.", encoding="utf-8")
        url_text = root / "url-content.txt"
        url_text.write_text("Fetched URL content", encoding="utf-8")

        pack = workbench.create_context_pack(
            root=root,
            local_paths=[notes],
            urls=["https://example.com/spec"],
            url_fetcher=lambda url: url_text.read_text(encoding="utf-8"),
        )

        self.assertEqual(len(pack["sources"]), 2)
        context_dir = root / "loop" / "context"
        self.assertTrue((context_dir / "local-notes.md").exists())
        self.assertTrue((context_dir / "url-001.md").exists())
        pack_text = (root / "loop" / "context_pack.md").read_text(encoding="utf-8")
        self.assertIn("Important local context", pack_text)
        self.assertIn("https://example.com/spec", pack_text)

    def test_planner_creates_tasks_from_goal_and_context(self):
        temp, root = self.make_root()
        self.addCleanup(temp.cleanup)
        (root / "loop" / "goal.md").write_text(
            "# Goal Contract\n\n## MVP\nGoal-to-run pipeline\n", encoding="utf-8"
        )
        (root / "loop" / "context_pack.md").write_text(
            "# Context Pack\n\nRelevant constraints.\n", encoding="utf-8"
        )

        tasks = workbench.plan_tasks(
            root=root,
            task_titles=["Create contract CLI", "Add supervisor report"],
        )

        self.assertEqual([task["id"] for task in tasks], ["T001", "T002"])
        tasks_text = (root / "loop" / "tasks.md").read_text(encoding="utf-8")
        self.assertIn("- [todo] T001: Create contract CLI", tasks_text)
        self.assertIn("Context: loop/context_pack.md", tasks_text)

    def test_planner_command_decomposes_goal_into_tasks(self):
        temp, root = self.make_root()
        self.addCleanup(temp.cleanup)
        (root / "loop" / "goal.md").write_text("# Goal Contract\n", encoding="utf-8")
        (root / "loop" / "context_pack.md").write_text("# Context Pack\n", encoding="utf-8")
        command = f"{sys.executable} -c \"print('Create contract CLI\\nAdd supervisor report')\""

        tasks = workbench.plan_tasks_with_command(root=root, planner_command=command, timeout_seconds=30)

        self.assertEqual([task["title"] for task in tasks], ["Create contract CLI", "Add supervisor report"])
        self.assertIn("T002", (root / "loop" / "tasks.md").read_text(encoding="utf-8"))

    def test_planner_command_supports_root_placeholder(self):
        temp, root = self.make_root()
        self.addCleanup(temp.cleanup)
        (root / "loop" / "goal.md").write_text("# Goal Contract\n", encoding="utf-8")
        (root / "loop" / "context_pack.md").write_text("# Context Pack\n", encoding="utf-8")
        command = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "print('Root ok' if Path('{root}').exists() else 'Root missing')\""
        )

        tasks = workbench.plan_tasks_with_command(root=root, planner_command=command, timeout_seconds=30)

        self.assertEqual(tasks[0]["title"], "Root ok")

    def test_cli_init_goal_creates_contract_from_json_file(self):
        temp, root = self.make_root()
        self.addCleanup(temp.cleanup)
        payload = root / "goal-input.json"
        payload.write_text(
            json.dumps(
                {
                    "product": "Loop Workbench",
                    "user": "Developer",
                    "mvp": "Goal-to-run pipeline",
                    "success_criteria": ["Contract exists"],
                    "acceptance_checks": ["python3 -m unittest discover -s tests"],
                    "out_of_scope": ["Web UI"],
                    "allowed_paths": ["src", "tests", "loop"],
                    "forbidden_paths": [".env"],
                    "stop_rules": ["Stop on verifier failure"],
                }
            ),
            encoding="utf-8",
        )

        with redirect_stdout(StringIO()):
            exit_code = workbench.main(["init-goal", "--input", str(payload), "--root", str(root)])

        self.assertEqual(exit_code, 0)
        self.assertTrue((root / "loop" / "goal.md").exists())

    def test_cli_plan_tasks_can_use_planner_command_without_titles(self):
        temp, root = self.make_root()
        self.addCleanup(temp.cleanup)
        (root / "loop" / "goal.md").write_text("# Goal Contract\n", encoding="utf-8")
        (root / "loop" / "context_pack.md").write_text("# Context Pack\n", encoding="utf-8")
        command = f"{sys.executable} -c \"print('First task\\nSecond task')\""

        with redirect_stdout(StringIO()):
            exit_code = workbench.main(
                [
                    "plan-tasks",
                    "--root",
                    str(root),
                    "--planner-command",
                    command,
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("Second task", (root / "loop" / "tasks.md").read_text(encoding="utf-8"))


class SupervisorTests(unittest.TestCase):
    def make_loop_workspace(self, worker_command=None, reviewer_command=None, verifier_command=None):
        temp = TemporaryDirectory()
        root = Path(temp.name)
        loop_dir = root / "loop"
        loop_dir.mkdir()
        (loop_dir / "project.md").write_text("# Project\n", encoding="utf-8")
        (loop_dir / "goal.md").write_text("# Goal Contract\n", encoding="utf-8")
        (loop_dir / "context_pack.md").write_text("# Context Pack\n", encoding="utf-8")
        (loop_dir / "tasks.md").write_text("- [todo] T001: Do one thing\n", encoding="utf-8")
        agent = worker_command or f"{sys.executable} -c \"from pathlib import Path; Path('src').mkdir(); Path('src/done.txt').write_text('done')\""
        verifier = verifier_command or f"{sys.executable} -c \"print('ok')\""
        (loop_dir / "config.yaml").write_text(
            textwrap.dedent(
                f"""\
                max_attempts_per_task: 2
                max_runtime_seconds_per_task: 60
                max_changed_files: 5
                review_on_success: false
                agent_command: "{agent}"
                verifier_commands:
                  - name: tests
                    command: "{verifier}"
                allowed_paths:
                  - src
                  - loop
                forbidden_paths:
                  - .env
                """
            ),
            encoding="utf-8",
        )
        reviewer = reviewer_command or f"{sys.executable} -c \"print('NO_BLOCKING_FINDINGS')\""
        (loop_dir / "review_config.yaml").write_text(
            textwrap.dedent(
                f"""\
                min_iterations: 1
                max_iterations: 2
                timeout_seconds: 60
                approval_marker: "NO_BLOCKING_FINDINGS"
                reviewer_command: "{reviewer}"
                fixer_command: "{sys.executable} -c \\"print('fixer noop')\\""
                max_changed_files: 5
                allowed_paths:
                  - src
                  - loop
                forbidden_paths:
                  - .env
                verifier_commands:
                  - name: tests
                    command: "{verifier}"
                """
            ),
            encoding="utf-8",
        )
        (loop_dir / "state.json").write_text("{}", encoding="utf-8")
        return temp, root

    def test_supervisor_runs_worker_review_cycle_and_writes_final_report(self):
        temp, root = self.make_loop_workspace()
        self.addCleanup(temp.cleanup)

        result = workbench.run_supervisor(
            root=root,
            max_cycles=2,
            max_hours=24,
            max_consecutive_failures=1,
            sleep_seconds=0,
        )

        self.assertEqual(result["status"], "complete")
        self.assertTrue((root / "loop" / "supervisor_state.json").exists())
        report = (root / "loop" / "final_report.md").read_text(encoding="utf-8")
        self.assertIn("Completed Tasks", report)
        self.assertIn("T001", report)

    def test_supervisor_resumes_existing_state(self):
        temp, root = self.make_loop_workspace()
        self.addCleanup(temp.cleanup)
        state_path = root / "loop" / "supervisor_state.json"
        state_path.write_text(
            json.dumps(
                {
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "max_cycles": 5,
                    "max_hours": 24,
                    "events": [{"cycle": 1, "worker_status": "retry", "review_status": None}],
                    "consecutive_failures": 0,
                }
            ),
            encoding="utf-8",
        )

        result = workbench.run_supervisor(
            root=root,
            max_cycles=3,
            max_hours=24,
            max_consecutive_failures=1,
            sleep_seconds=0,
        )

        self.assertEqual(result["status"], "complete")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(state["events"]), 2)

    def test_supervisor_stops_on_repeated_failures(self):
        verifier = f"{sys.executable} -c \"import sys; print('bad'); sys.exit(2)\""
        temp, root = self.make_loop_workspace(verifier_command=verifier)
        self.addCleanup(temp.cleanup)

        result = workbench.run_supervisor(
            root=root,
            max_cycles=5,
            max_hours=24,
            max_consecutive_failures=1,
            sleep_seconds=0,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stop_reason"], "max_consecutive_failures")


if __name__ == "__main__":
    unittest.main()
