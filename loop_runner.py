#!/usr/bin/env python3
"""Local queue worker for loop-engineered project building."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import signal
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


TASK_RE = re.compile(r"^- \[(?P<status>[a-z]+)\] (?P<id>[A-Za-z0-9_-]+): (?P<title>.+)$")


@dataclasses.dataclass
class Task:
    id: str
    title: str
    status: str
    line_index: int
    body: str


@dataclasses.dataclass
class CommandResult:
    name: str
    command: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclasses.dataclass
class RunResult:
    status: str
    task: Task | None
    run_dir: Path | None
    message: str


class QueueStateError(ValueError):
    pass


def load_simple_yaml(path: Path) -> dict[str, Any]:
    """Parse the small YAML subset used by loop/config.yaml."""
    lines = path.read_text(encoding="utf-8").splitlines()
    config: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        raw = lines[index]
        if not raw.strip() or raw.lstrip().startswith("#"):
            index += 1
            continue
        if raw.startswith(" "):
            raise ValueError(f"Unexpected indentation in {path}: {raw}")
        key, value = _split_key_value(raw)
        if value:
            config[key] = _parse_scalar(value)
            index += 1
            continue

        block: list[Any] = []
        index += 1
        while index < len(lines) and lines[index].startswith("  "):
            item_line = lines[index].strip()
            if not item_line or item_line.startswith("#"):
                index += 1
                continue
            if item_line.startswith("- "):
                first = item_line[2:]
                if ":" in first:
                    item_key, item_value = _split_key_value(first)
                    item: dict[str, Any] = {item_key: _parse_scalar(item_value)}
                    index += 1
                    while index < len(lines) and lines[index].startswith("    "):
                        child_key, child_value = _split_key_value(lines[index].strip())
                        item[child_key] = _parse_scalar(child_value)
                        index += 1
                    block.append(item)
                else:
                    block.append(_parse_scalar(first))
                    index += 1
            else:
                raise ValueError(f"Unsupported YAML line in {path}: {lines[index]}")
        config[key] = block
    return config


def _split_key_value(line: str) -> tuple[str, str]:
    if ":" not in line:
        raise ValueError(f"Expected key/value line: {line}")
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _parse_scalar(value: str) -> Any:
    if value == "":
        return ""
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1].replace('\\"', '"').replace("\\'", "'")
    return value


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_tasks(path: Path) -> tuple[list[str], list[Task]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    tasks: list[Task] = []
    for index, line in enumerate(lines):
        match = TASK_RE.match(line)
        if not match:
            continue
        body_lines: list[str] = []
        cursor = index + 1
        while cursor < len(lines) and not TASK_RE.match(lines[cursor]):
            body_lines.append(lines[cursor])
            cursor += 1
        tasks.append(
            Task(
                id=match.group("id"),
                title=match.group("title"),
                status=match.group("status"),
                line_index=index,
                body="\n".join(body_lines).strip(),
            )
        )
    return lines, tasks


def write_task_status(path: Path, task: Task, new_status: str) -> None:
    lines, _ = parse_tasks(path)
    lines[task.line_index] = f"- [{new_status}] {task.id}: {task.title}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def select_task(tasks_path: Path, promote_todo: bool = True) -> Task | None:
    _, tasks = parse_tasks(tasks_path)
    running_tasks = [task for task in tasks if task.status == "running"]
    if len(running_tasks) > 1:
        ids = ", ".join(task.id for task in running_tasks)
        raise QueueStateError(f"Multiple running tasks: {ids}")
    for task in tasks:
        if task.status == "running":
            return task
    for task in tasks:
        if task.status == "todo":
            if promote_todo:
                write_task_status(tasks_path, task, "running")
                return dataclasses.replace(task, status="running")
            return task
    return None


def make_run_dir(loop_dir: Path) -> Path:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = loop_dir / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def build_prompt(
    *,
    project_text: str,
    task: Task,
    config: dict[str, Any],
    state: dict[str, Any],
    remaining_attempts: int,
) -> str:
    return "\n".join(
        [
            "# Loop Worker Task",
            "",
            "You are the implementation agent inside a bounded local loop.",
            "Work only on the current task. Do not start unrelated tasks.",
            "",
            "## Project Context",
            project_text.strip(),
            "",
            "## Current Task",
            f"{task.id}: {task.title}",
            task.body or "(no extra task notes)",
            "",
            "## Constraints",
            f"Allowed paths: {', '.join(config.get('allowed_paths', [])) or '(not set)'}",
            f"Forbidden paths: {', '.join(config.get('forbidden_paths', [])) or '(not set)'}",
            f"Maximum changed files: {config.get('max_changed_files')}",
            f"Remaining attempts: {remaining_attempts}",
            "",
            "## Previous Verifier Output",
            state.get("last_verifier_output") or "(none)",
            "",
            "## Required Behavior",
            "- Make the smallest useful change for the current task.",
            "- Leave verification to the loop runner.",
            "- Stop and explain if the task is unsafe or underspecified.",
        ]
    )


def run_shell_command(command: str, cwd: Path, timeout: int, stdin_text: str | None = None) -> CommandResult:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=stdin_text, timeout=timeout)
        return CommandResult(
            name="command",
            command=command,
            returncode=process.returncode if process.returncode is not None else 0,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        message = f"Command timed out after {timeout} seconds."
        return CommandResult(
            name="command",
            command=command,
            returncode=124,
            stdout=stdout or "",
            stderr=(stderr + "\n" + message).strip() + "\n",
            timed_out=True,
        )


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_agent(config: dict[str, Any], root: Path, prompt: str, run_dir: Path, timeout: int) -> CommandResult:
    command = str(config.get("agent_command", "")).strip()
    if not command:
        return CommandResult("agent", "", 0, "No agent_command configured.\n", "")

    prompt_file = run_dir / "prompt.md"
    rendered = command.replace("{prompt_file}", shlex.quote(str(prompt_file))).replace(
        "{root}", shlex.quote(str(root))
    )
    result = run_shell_command(rendered, root, timeout, stdin_text=prompt)
    result.name = "agent"
    return result


def run_verifiers(config: dict[str, Any], root: Path, run_dir: Path, timeout: int) -> list[CommandResult]:
    results: list[CommandResult] = []
    for item in config.get("verifier_commands", []):
        name = str(item.get("name", "verifier"))
        command = str(item["command"])
        result = run_shell_command(command, root, timeout)
        result.name = name
        (run_dir / f"verifier-{name}.log").write_text(
            _format_command_result(result), encoding="utf-8"
        )
        results.append(result)
    return results


def _format_command_result(result: CommandResult) -> str:
    return (
        f"$ {result.command}\n"
        f"exit: {result.returncode}\n\n"
        f"stdout:\n{result.stdout}\n\n"
        f"stderr:\n{result.stderr}\n"
    )


def changed_files(root: Path, loop_dir: Path) -> list[str]:
    git_dir = root / ".git"
    if git_dir.exists():
        completed = subprocess.run(
            "git status --porcelain",
            cwd=root,
            text=True,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        files: list[str] = []
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            path = line[3:] if len(line) > 3 else line
            if path.startswith(str(loop_dir.relative_to(root)) + "/"):
                continue
            files.append(path)
        return files

    files = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_relative_to(loop_dir):
            files.append(str(path.relative_to(root)))
    return files


def file_snapshot(root: Path, loop_dir: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_generated_loop_artifact(path, loop_dir):
            continue
        if ".git" in path.relative_to(root).parts:
            continue
        relative = str(path.relative_to(root))
        snapshot[relative] = _file_hash(path)
    return snapshot


def _is_generated_loop_artifact(path: Path, loop_dir: Path) -> bool:
    try:
        relative = path.relative_to(loop_dir)
    except ValueError:
        return False
    return relative.parts[:1] in {("runs",), ("review-runs",)}


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def paths_changed_between(before: dict[str, str], after: dict[str, str]) -> list[str]:
    paths = set(before) | set(after)
    return sorted(path for path in paths if before.get(path) != after.get(path))


def enforce_changed_file_limit(
    config: dict[str, Any],
    attempt_changed_files: list[str],
) -> list[str]:
    limit = int(config.get("max_changed_files", 0) or 0)
    if limit <= 0:
        return []
    return attempt_changed_files if len(attempt_changed_files) > limit else []


def path_policy_violations(config: dict[str, Any], changed_paths: list[str]) -> list[str]:
    allowed = [path.strip("/") for path in config.get("allowed_paths", []) if path]
    forbidden = [path.strip("/") for path in config.get("forbidden_paths", []) if path]
    violations: list[str] = []
    for path in changed_paths:
        normalized = path.strip("/")
        if any(_path_matches(normalized, rule) for rule in forbidden):
            violations.append(path)
            continue
        if allowed and not any(_path_matches(normalized, rule) for rule in allowed):
            violations.append(path)
    return violations


def _path_matches(path: str, rule: str) -> bool:
    if rule in {"", "."}:
        return True
    return path == rule or path.startswith(rule + "/")


def write_diff_log(root: Path, loop_dir: Path, run_dir: Path) -> None:
    if not (root / ".git").exists():
        return
    loop_relative = str(loop_dir.relative_to(root))
    exclusions = [
        f":(exclude){loop_relative}/runs",
        f":(exclude){loop_relative}/review-runs",
    ]
    pathspec = " ".join(shlex.quote(item) for item in [".", *exclusions])
    command = f"git diff --stat -- {pathspec} && git diff -- {pathspec}"
    completed = subprocess.run(
        command,
        cwd=root,
        text=True,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    (run_dir / "diff.log").write_text(
        f"$ {command}\n"
        f"exit: {completed.returncode}\n\n"
        f"stdout:\n{completed.stdout}\n\n"
        f"stderr:\n{completed.stderr}\n",
        encoding="utf-8",
    )


def run_once(root: Path, loop_dir: Path, dry_run: bool = False) -> RunResult:
    root = root.resolve()
    loop_dir = loop_dir.resolve()
    config = load_simple_yaml(loop_dir / "config.yaml")
    state_path = loop_dir / "state.json"
    tasks_path = loop_dir / "tasks.md"
    state = load_state(state_path)
    state.setdefault("attempts", {})

    try:
        task = select_task(tasks_path, promote_todo=not dry_run)
    except QueueStateError as error:
        state["stop_reason"] = "multiple_running_tasks"
        state["error"] = str(error)
        save_state(state_path, state)
        return RunResult("blocked", None, None, str(error))
    if task is None:
        state["stop_reason"] = "no_runnable_tasks"
        save_state(state_path, state)
        return RunResult("idle", None, None, "No todo or running task found.")

    attempts = int(state["attempts"].get(task.id, 0))
    max_attempts = int(config.get("max_attempts_per_task", 1))
    if not dry_run and attempts >= max_attempts:
        write_task_status(tasks_path, task, "blocked")
        state["current_task_id"] = task.id
        state["stop_reason"] = "max_attempts_exhausted"
        save_state(state_path, state)
        return RunResult("blocked", task, None, "Attempt budget is already exhausted.")

    remaining_attempts = max(max_attempts - attempts, 0)
    run_dir = make_run_dir(loop_dir)
    project_text = (loop_dir / "project.md").read_text(encoding="utf-8")
    prompt = build_prompt(
        project_text=project_text,
        task=task,
        config=config,
        state=state,
        remaining_attempts=remaining_attempts,
    )
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    state["current_task_id"] = task.id
    state["current_run_dir"] = str(run_dir.relative_to(root))

    if dry_run:
        state["stop_reason"] = "dry_run"
        save_state(state_path, state)
        return RunResult("dry_run", task, run_dir, "Prompt written; agent not run.")

    timeout = int(config.get("max_runtime_seconds_per_task", 300))
    before_snapshot = file_snapshot(root, loop_dir)
    agent_result = run_agent(config, root, prompt, run_dir, timeout)
    (run_dir / "agent.log").write_text(_format_command_result(agent_result), encoding="utf-8")
    write_diff_log(root, loop_dir, run_dir)
    state["attempts"][task.id] = attempts + 1

    after_agent_snapshot = file_snapshot(root, loop_dir)
    attempt_changed = paths_changed_between(before_snapshot, after_agent_snapshot)

    path_violations = path_policy_violations(config, attempt_changed)
    if path_violations:
        state["stop_reason"] = "path_policy_violation"
        state["path_policy_violations"] = path_violations
        write_task_status(tasks_path, task, "blocked")
        save_state(state_path, state)
        return RunResult("blocked", task, run_dir, "Path policy violation.")

    changed_over_limit = enforce_changed_file_limit(config, attempt_changed)
    if changed_over_limit:
        state["stop_reason"] = "changed_file_limit_exceeded"
        state["changed_files"] = changed_over_limit
        write_task_status(tasks_path, task, "blocked")
        save_state(state_path, state)
        return RunResult("blocked", task, run_dir, "Changed file limit exceeded.")

    if agent_result.timed_out:
        write_task_status(tasks_path, task, "blocked")
        state["stop_reason"] = "agent_timeout"
        state["last_agent_output"] = _format_command_result(agent_result)
        save_state(state_path, state)
        return RunResult("blocked", task, run_dir, "Agent command timed out.")

    if agent_result.returncode != 0:
        state["last_agent_output"] = _format_command_result(agent_result)
        if state["attempts"][task.id] >= max_attempts:
            write_task_status(tasks_path, task, "blocked")
            state["stop_reason"] = "agent_failed_max_attempts_exhausted"
            save_state(state_path, state)
            return RunResult("blocked", task, run_dir, "Agent failed and attempt budget is exhausted.")
        state["stop_reason"] = "agent_failed_retry_available"
        save_state(state_path, state)
        return RunResult("retry", task, run_dir, "Agent failed; retry available.")

    verifier_results = run_verifiers(config, root, run_dir, timeout)
    failed = [result for result in verifier_results if result.returncode != 0]
    state["last_verifier_output"] = "\n\n".join(_format_command_result(result) for result in verifier_results)

    timed_out_verifiers = [result for result in verifier_results if result.timed_out]
    if timed_out_verifiers and state["attempts"][task.id] >= max_attempts:
        write_task_status(tasks_path, task, "blocked")
        state["stop_reason"] = "verifier_timeout"
        save_state(state_path, state)
        return RunResult("blocked", task, run_dir, "Verifier timed out and attempt budget is exhausted.")

    if not failed:
        final_status = "review" if config.get("review_on_success", True) else "done"
        write_task_status(tasks_path, task, final_status)
        state["stop_reason"] = "verifiers_passed"
        state["current_task_id"] = None
        save_state(state_path, state)
        return RunResult(final_status, task, run_dir, "All verifiers passed.")

    if state["attempts"][task.id] >= max_attempts:
        write_task_status(tasks_path, task, "blocked")
        state["stop_reason"] = "max_attempts_exhausted"
        save_state(state_path, state)
        return RunResult("blocked", task, run_dir, "Verifier failed and attempt budget is exhausted.")

    state["stop_reason"] = "verifier_failed_retry_available"
    save_state(state_path, state)
    return RunResult("retry", task, run_dir, "Verifier failed; retry available.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one local loop-engineering queue iteration.")
    parser.add_argument("--loop-dir", default="loop", help="Path to the loop state directory.")
    parser.add_argument("--dry-run", action="store_true", help="Write the prompt without running the agent.")
    args = parser.parse_args(argv)

    root = Path.cwd()
    loop_dir = (root / args.loop_dir).resolve()
    result = run_once(root=root, loop_dir=loop_dir, dry_run=args.dry_run)
    print(
        json.dumps(
            {
                "status": result.status,
                "task": result.task.id if result.task else None,
                "run_dir": str(result.run_dir) if result.run_dir else None,
                "message": result.message,
            },
            indent=2,
        )
    )
    return 0 if result.status in {"dry_run", "done", "review", "retry", "idle"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
