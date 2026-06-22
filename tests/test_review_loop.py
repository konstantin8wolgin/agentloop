import json
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import review_loop


class ReviewLoopTests(unittest.TestCase):
    def make_workspace(
        self,
        reviewer_command,
        fixer_command=None,
        min_iterations=1,
        max_iterations=3,
        verifier_command=None,
        allowed_paths=None,
    ):
        temp = TemporaryDirectory()
        root = Path(temp.name)
        loop_dir = root / "loop"
        loop_dir.mkdir()
        (root / "target.txt").write_text("initial", encoding="utf-8")
        configured_fixer = fixer_command or f"{sys.executable} -c \"print('fixer noop')\""
        verifier = verifier_command or f"{sys.executable} -c \"print('ok')\""
        configured_allowed = allowed_paths or ["target.txt"]
        allowed_yaml = "\n".join(f"  - {path}" for path in configured_allowed)
        config_text = (
            f"min_iterations: {min_iterations}\n"
            f"max_iterations: {max_iterations}\n"
            'approval_marker: "NO_BLOCKING_FINDINGS"\n'
            f'reviewer_command: "{reviewer_command}"\n'
            f'fixer_command: "{configured_fixer}"\n'
            "max_changed_files: 4\n"
            "allowed_paths:\n"
            f"{allowed_yaml}\n"
            "forbidden_paths:\n"
            "  - .env\n"
            "verifier_commands:\n"
            "  - name: tests\n"
            f'    command: "{verifier}"\n'
        )
        (loop_dir / "review_config.yaml").write_text(
            config_text,
            encoding="utf-8",
        )
        return temp, root, loop_dir

    def test_reviewer_approval_stops_after_minimum_iterations(self):
        reviewer = f"{sys.executable} -c \"print('NO_BLOCKING_FINDINGS')\""
        temp, root, loop_dir = self.make_workspace(reviewer, min_iterations=2, max_iterations=5)
        self.addCleanup(temp.cleanup)

        result = review_loop.run_review_loop(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "approved")
        self.assertEqual(result.iterations, 2)
        self.assertEqual(len(list((loop_dir / "review-runs").glob("*/iteration-*"))), 2)

    def test_reviewer_approval_requires_passing_verifier(self):
        reviewer = f"{sys.executable} -c \"print('NO_BLOCKING_FINDINGS')\""
        verifier = f"{sys.executable} -c \"import sys; print('broken'); sys.exit(5)\""
        temp, root, loop_dir = self.make_workspace(
            reviewer,
            min_iterations=2,
            max_iterations=3,
            verifier_command=verifier,
        )
        self.addCleanup(temp.cleanup)

        result = review_loop.run_review_loop(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.iterations, 2)
        state = json.loads((loop_dir / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "verifier_failed")

    def test_blocking_review_that_mentions_approval_marker_does_not_approve(self):
        reviewer = (
            f"{sys.executable} -c \"print('BLOCKING: do not print NO_BLOCKING_FINDINGS yet')\""
        )
        temp, root, loop_dir = self.make_workspace(reviewer, min_iterations=1, max_iterations=1)
        self.addCleanup(temp.cleanup)

        result = review_loop.run_review_loop(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        state = json.loads((loop_dir / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "max_iterations_without_approval")

    def test_fixer_runs_when_review_has_findings(self):
        reviewer = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "print('NO_BLOCKING_FINDINGS' if Path('target.txt').read_text() == 'fixed' else 'BLOCKING: fix target')\""
        )
        fixer = f"{sys.executable} -c \"from pathlib import Path; Path('target.txt').write_text('fixed')\""
        temp, root, loop_dir = self.make_workspace(reviewer, fixer, min_iterations=1, max_iterations=3)
        self.addCleanup(temp.cleanup)

        result = review_loop.run_review_loop(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "approved")
        self.assertEqual(result.iterations, 2)
        self.assertEqual((root / "target.txt").read_text(encoding="utf-8"), "fixed")

    def test_loop_blocks_after_max_iterations_without_approval(self):
        reviewer = f"{sys.executable} -c \"print('BLOCKING: still bad')\""
        temp, root, loop_dir = self.make_workspace(reviewer, min_iterations=1, max_iterations=2)
        self.addCleanup(temp.cleanup)

        result = review_loop.run_review_loop(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.iterations, 2)
        state = json.loads((loop_dir / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "max_iterations_without_approval")

    def test_approval_is_blocked_when_latest_verifier_failed(self):
        reviewer = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "print('NO_BLOCKING_FINDINGS' if Path('target.txt').read_text() == 'fixed' else 'BLOCKING: fix target')\""
        )
        fixer = f"{sys.executable} -c \"from pathlib import Path; Path('target.txt').write_text('fixed')\""
        verifier = f"{sys.executable} -c \"import sys; print('tests failed'); sys.exit(3)\""
        temp, root, loop_dir = self.make_workspace(
            reviewer,
            fixer,
            min_iterations=1,
            max_iterations=3,
            verifier_command=verifier,
        )
        self.addCleanup(temp.cleanup)

        result = review_loop.run_review_loop(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        state = json.loads((loop_dir / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "verifier_failed")

    def test_fixer_out_of_scope_change_blocks_review_loop(self):
        reviewer = f"{sys.executable} -c \"print('BLOCKING: create unsafe file')\""
        fixer = f"{sys.executable} -c \"from pathlib import Path; Path('.env').write_text('secret')\""
        temp, root, loop_dir = self.make_workspace(reviewer, fixer, min_iterations=1, max_iterations=2)
        self.addCleanup(temp.cleanup)

        result = review_loop.run_review_loop(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        state = json.loads((loop_dir / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "path_policy_violation")

    def test_fixer_changed_file_limit_blocks_review_loop(self):
        reviewer = f"{sys.executable} -c \"print('BLOCKING: too many files')\""
        fixer = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "[Path('target' + str(i) + '.txt').write_text(str(i)) for i in range(5)]\""
        )
        temp, root, loop_dir = self.make_workspace(
            reviewer,
            fixer,
            min_iterations=1,
            max_iterations=2,
            allowed_paths=["."],
        )
        self.addCleanup(temp.cleanup)

        result = review_loop.run_review_loop(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        state = json.loads((loop_dir / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "changed_file_limit_exceeded")

    def test_reviewer_approval_with_mutation_is_blocked(self):
        reviewer = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "Path('target.txt').write_text('reviewer edit'); print('NO_BLOCKING_FINDINGS')\""
        )
        temp, root, loop_dir = self.make_workspace(reviewer, min_iterations=1, max_iterations=2)
        self.addCleanup(temp.cleanup)

        result = review_loop.run_review_loop(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        state = json.loads((loop_dir / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "reviewer_changed_files")

    def test_reviewer_blocking_finding_with_mutation_is_blocked_before_fixer(self):
        reviewer = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "Path('target.txt').write_text('reviewer edit'); print('BLOCKING: real issue')\""
        )
        fixer = f"{sys.executable} -c \"from pathlib import Path; Path('target.txt').write_text('fixer edit')\""
        temp, root, loop_dir = self.make_workspace(reviewer, fixer, min_iterations=1, max_iterations=2)
        self.addCleanup(temp.cleanup)

        result = review_loop.run_review_loop(root=root, loop_dir=loop_dir)

        self.assertEqual(result.status, "blocked")
        self.assertEqual((root / "target.txt").read_text(encoding="utf-8"), "reviewer edit")
        state = json.loads((loop_dir / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stop_reason"], "reviewer_changed_files")


if __name__ == "__main__":
    unittest.main()
