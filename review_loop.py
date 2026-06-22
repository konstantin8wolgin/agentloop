#!/usr/bin/env python3
"""Independent reviewer/fixer loop for improving the workflow itself."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import shlex
from pathlib import Path
from typing import Any

from loop_runner import (
    CommandResult,
    enforce_changed_file_limit,
    file_snapshot,
    load_simple_yaml,
    path_policy_violations,
    paths_changed_between,
    run_shell_command,
)


@dataclasses.dataclass
class ReviewLoopResult:
    status: str
    iterations: int
    run_dir: Path
    message: str


def run_review_loop(root: Path, loop_dir: Path) -> ReviewLoopResult:
    root = root.resolve()
    loop_dir = loop_dir.resolve()
    config = load_simple_yaml(loop_dir / "review_config.yaml")
    state_path = loop_dir / "review_state.json"
    state: dict[str, Any] = {"iterations": []}

    min_iterations = int(config.get("min_iterations", 5))
    max_iterations = int(config.get("max_iterations", 10))
    if min_iterations < 1:
        raise ValueError("min_iterations must be at least 1")
    if max_iterations < min_iterations:
        raise ValueError("max_iterations must be greater than or equal to min_iterations")

    approval_marker = str(config.get("approval_marker", "NO_BLOCKING_FINDINGS"))
    run_dir = _make_review_run_dir(loop_dir)
    previous_review = ""
    previous_verifier_output = ""

    for iteration in range(1, max_iterations + 1):
        iteration_dir = run_dir / f"iteration-{iteration:02d}"
        iteration_dir.mkdir(parents=True, exist_ok=False)

        reviewer_prompt = build_reviewer_prompt(
            config=config,
            iteration=iteration,
            min_iterations=min_iterations,
            max_iterations=max_iterations,
            approval_marker=approval_marker,
            previous_review=previous_review,
            previous_verifier_output=previous_verifier_output,
        )
        (iteration_dir / "reviewer-prompt.md").write_text(reviewer_prompt, encoding="utf-8")
        before_reviewer_snapshot = file_snapshot(root, loop_dir)
        review_result = run_configured_command(
            config,
            key="reviewer_command",
            root=root,
            iteration_dir=iteration_dir,
            iteration=iteration,
            stdin_text=reviewer_prompt,
        )
        review_text = review_result.stdout + review_result.stderr
        (iteration_dir / "review.md").write_text(review_text, encoding="utf-8")
        (iteration_dir / "reviewer.log").write_text(format_command_result(review_result), encoding="utf-8")
        after_reviewer_snapshot = file_snapshot(root, loop_dir)
        reviewer_changed = paths_changed_between(before_reviewer_snapshot, after_reviewer_snapshot)

        state["iterations"].append({"iteration": iteration, "review_returncode": review_result.returncode})
        save_review_state(state_path, state)

        if review_result.timed_out or review_result.returncode != 0:
            state["stop_reason"] = "reviewer_failed"
            save_review_state(state_path, state)
            return ReviewLoopResult("blocked", iteration, run_dir, "Reviewer command failed.")

        if reviewer_changed:
            state["stop_reason"] = "reviewer_changed_files"
            state["reviewer_changed_files"] = reviewer_changed
            save_review_state(state_path, state)
            return ReviewLoopResult("blocked", iteration, run_dir, "Reviewer command changed files.")

        if review_is_approved(review_text, approval_marker):
            if iteration >= min_iterations:
                verifier_results = run_verifiers(config, root, iteration_dir)
                previous_verifier_output = "\n\n".join(
                    format_command_result(result) for result in verifier_results
                )
                state["iterations"][-1]["verifier_returncodes"] = [
                    result.returncode for result in verifier_results
                ]
                if any(result.returncode != 0 for result in verifier_results):
                    state["stop_reason"] = "verifier_failed"
                    save_review_state(state_path, state)
                    return ReviewLoopResult("blocked", iteration, run_dir, "Verifier failed before approval.")
                state["stop_reason"] = "approved"
                state["last_verifier_output"] = previous_verifier_output
                save_review_state(state_path, state)
                return ReviewLoopResult("approved", iteration, run_dir, "Reviewer approved after minimum iterations.")
            previous_review = review_text
            continue

        fixer_prompt = build_fixer_prompt(
            config=config,
            iteration=iteration,
            review_text=review_text,
            previous_verifier_output=previous_verifier_output,
        )
        (iteration_dir / "fixer-prompt.md").write_text(fixer_prompt, encoding="utf-8")
        before_fixer_snapshot = file_snapshot(root, loop_dir)
        fixer_result = run_configured_command(
            config,
            key="fixer_command",
            root=root,
            iteration_dir=iteration_dir,
            iteration=iteration,
            stdin_text=fixer_prompt,
        )
        (iteration_dir / "fixer.log").write_text(format_command_result(fixer_result), encoding="utf-8")
        if fixer_result.timed_out or fixer_result.returncode != 0:
            state["stop_reason"] = "fixer_failed"
            save_review_state(state_path, state)
            return ReviewLoopResult("blocked", iteration, run_dir, "Fixer command failed.")

        after_fixer_snapshot = file_snapshot(root, loop_dir)
        fixer_changed = paths_changed_between(before_fixer_snapshot, after_fixer_snapshot)
        path_violations = path_policy_violations(config, fixer_changed)
        if path_violations:
            state["stop_reason"] = "path_policy_violation"
            state["path_policy_violations"] = path_violations
            save_review_state(state_path, state)
            return ReviewLoopResult("blocked", iteration, run_dir, "Fixer changed files outside policy.")

        changed_over_limit = enforce_changed_file_limit(config, fixer_changed)
        if changed_over_limit:
            state["stop_reason"] = "changed_file_limit_exceeded"
            state["changed_files"] = changed_over_limit
            save_review_state(state_path, state)
            return ReviewLoopResult("blocked", iteration, run_dir, "Fixer changed too many files.")

        verifier_results = run_verifiers(config, root, iteration_dir)
        previous_verifier_output = "\n\n".join(format_command_result(result) for result in verifier_results)
        previous_review = review_text
        state["iterations"][-1]["verifier_returncodes"] = [
            result.returncode for result in verifier_results
        ]
        save_review_state(state_path, state)
        if any(result.returncode != 0 for result in verifier_results):
            state["stop_reason"] = "verifier_failed"
            save_review_state(state_path, state)
            return ReviewLoopResult("blocked", iteration, run_dir, "Verifier failed after fixer run.")

    state["stop_reason"] = "max_iterations_without_approval"
    save_review_state(state_path, state)
    return ReviewLoopResult("blocked", max_iterations, run_dir, "Review loop reached max iterations without approval.")


def _make_review_run_dir(loop_dir: Path) -> Path:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = loop_dir / "review-runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def build_reviewer_prompt(
    *,
    config: dict[str, Any],
    iteration: int,
    min_iterations: int,
    max_iterations: int,
    approval_marker: str,
    previous_review: str,
    previous_verifier_output: str,
) -> str:
    return "\n".join(
        [
            "# Independent Review",
            "",
            f"Iteration: {iteration}/{max_iterations}",
            f"Minimum independent review iterations before final approval: {min_iterations}",
            "",
            "Review the workflow as a code reviewer. Do not edit files.",
            "Find correctness, safety, test, and workflow issues.",
            "Return findings first, ordered by severity, with file/line references.",
            f"If there are no blocking findings, include this exact marker: {approval_marker}",
            "",
            "## Review Scope",
            str(config.get("review_scope", "loop_runner.py, review_loop.py, tests, README.md, loop/*.yaml, loop/*.md")),
            "",
            "## Previous Review",
            previous_review or "(none)",
            "",
            "## Previous Verifier Output",
            previous_verifier_output or "(none)",
        ]
    )


def review_is_approved(review_text: str, approval_marker: str) -> bool:
    return any(line.strip() == approval_marker for line in review_text.splitlines())


def build_fixer_prompt(
    *,
    config: dict[str, Any],
    iteration: int,
    review_text: str,
    previous_verifier_output: str,
) -> str:
    return "\n".join(
        [
            "# Fixer Task",
            "",
            f"Iteration: {iteration}",
            "Fix only the blocking findings from the independent reviewer.",
            "Do not broaden scope. Do not remove safety checks.",
            "After editing, leave verification to this loop.",
            "",
            "## Fix Scope",
            str(config.get("fix_scope", "local workflow files and tests")),
            "",
            "## Reviewer Findings",
            review_text.strip(),
            "",
            "## Previous Verifier Output",
            previous_verifier_output or "(none)",
        ]
    )


def run_configured_command(
    config: dict[str, Any],
    *,
    key: str,
    root: Path,
    iteration_dir: Path,
    iteration: int,
    stdin_text: str,
) -> CommandResult:
    command = str(config[key])
    rendered = (
        command.replace("{root}", shlex.quote(str(root)))
        .replace("{iteration_dir}", shlex.quote(str(iteration_dir)))
        .replace("{iteration}", str(iteration))
    )
    return run_shell_command(
        rendered,
        cwd=root,
        timeout=int(config.get("timeout_seconds", 900)),
        stdin_text=stdin_text,
    )


def run_verifiers(config: dict[str, Any], root: Path, iteration_dir: Path) -> list[CommandResult]:
    results: list[CommandResult] = []
    for item in config.get("verifier_commands", []):
        name = str(item.get("name", "verifier"))
        command = str(item["command"])
        result = run_shell_command(command, root, int(config.get("timeout_seconds", 900)))
        result.name = name
        (iteration_dir / f"verifier-{name}.log").write_text(
            format_command_result(result), encoding="utf-8"
        )
        results.append(result)
    return results


def format_command_result(result: CommandResult) -> str:
    timeout = " timed_out" if result.timed_out else ""
    return (
        f"$ {result.command}\n"
        f"exit: {result.returncode}{timeout}\n\n"
        f"stdout:\n{result.stdout}\n\n"
        f"stderr:\n{result.stderr}\n"
    )


def save_review_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an independent reviewer/fixer improvement loop.")
    parser.add_argument("--loop-dir", default="loop", help="Path to the loop state directory.")
    args = parser.parse_args(argv)

    root = Path.cwd()
    result = run_review_loop(root=root, loop_dir=root / args.loop_dir)
    print(
        json.dumps(
            {
                "status": result.status,
                "iterations": result.iterations,
                "run_dir": str(result.run_dir),
                "message": result.message,
            },
            indent=2,
        )
    )
    return 0 if result.status == "approved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
