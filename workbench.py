#!/usr/bin/env python3
"""CLI and supervisor for the local Loop Workbench."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import shlex
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import loop_runner
import review_loop


@dataclasses.dataclass
class SupervisorEvent:
    cycle: int
    worker_status: str
    review_status: str | None
    stop_reason: str | None = None


def create_goal_contract(
    *,
    root: Path,
    product: str,
    user: str,
    mvp: str,
    success_criteria: list[str],
    acceptance_checks: list[str],
    out_of_scope: list[str],
    allowed_paths: list[str],
    forbidden_paths: list[str],
    stop_rules: list[str],
) -> dict[str, Any]:
    if not acceptance_checks:
        raise ValueError("goal contract requires at least one acceptance check")
    if not success_criteria:
        raise ValueError("goal contract requires at least one success criterion")

    loop_dir = root / "loop"
    loop_dir.mkdir(parents=True, exist_ok=True)
    contract = {
        "product": product,
        "user": user,
        "mvp": mvp,
        "success_criteria": success_criteria,
        "acceptance_checks": acceptance_checks,
        "out_of_scope": out_of_scope,
        "allowed_paths": allowed_paths,
        "forbidden_paths": forbidden_paths,
        "stop_rules": stop_rules,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    (loop_dir / "goal.json").write_text(
        json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (loop_dir / "goal.md").write_text(render_goal_contract(contract), encoding="utf-8")
    return contract


def render_goal_contract(contract: dict[str, Any]) -> str:
    sections = [
        "# Goal Contract",
        "",
        f"## Product\n{contract['product']}",
        "",
        f"## User\n{contract['user']}",
        "",
        f"## MVP\n{contract['mvp']}",
        "",
        "## Success Criteria",
        _render_list(contract["success_criteria"]),
        "",
        "## Acceptance Checks",
        _render_list(contract["acceptance_checks"]),
        "",
        "## Out Of Scope",
        _render_list(contract["out_of_scope"]),
        "",
        "## Allowed Paths",
        _render_list(contract["allowed_paths"]),
        "",
        "## Forbidden Paths",
        _render_list(contract["forbidden_paths"]),
        "",
        "## Stop Rules",
        _render_list(contract["stop_rules"]),
        "",
    ]
    return "\n".join(sections)


def _render_list(items: list[str]) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- {item}" for item in items)


def create_context_pack(
    *,
    root: Path,
    local_paths: list[Path],
    urls: list[str],
    url_fetcher: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    loop_dir = root / "loop"
    context_dir = loop_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    fetcher = url_fetcher or fetch_url_text
    sources: list[dict[str, str]] = []
    pack_parts = ["# Context Pack", ""]

    for path in local_paths:
        source = Path(path)
        text = source.read_text(encoding="utf-8", errors="replace")
        target_name = f"local-{source.stem}.md"
        target = context_dir / target_name
        target.write_text(text, encoding="utf-8")
        sources.append({"type": "local", "source": str(source), "path": str(target.relative_to(root))})
        pack_parts.extend([f"## Local: {source}", "", text.strip(), ""])

    for index, url in enumerate(urls, start=1):
        text = fetcher(url)
        target = context_dir / f"url-{index:03d}.md"
        target.write_text(f"# URL Source\n\n{url}\n\n{text}", encoding="utf-8")
        sources.append({"type": "url", "source": url, "path": str(target.relative_to(root))})
        pack_parts.extend([f"## URL: {url}", "", text.strip(), ""])

    (loop_dir / "context_pack.md").write_text("\n".join(pack_parts).rstrip() + "\n", encoding="utf-8")
    manifest = {"sources": sources, "created_at": dt.datetime.now(dt.UTC).isoformat()}
    (loop_dir / "context_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def fetch_url_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def plan_tasks(root: Path, task_titles: list[str]) -> list[dict[str, str]]:
    loop_dir = root / "loop"
    goal_path = loop_dir / "goal.md"
    context_path = loop_dir / "context_pack.md"
    if not goal_path.exists():
        raise ValueError("loop/goal.md is required before planning tasks")
    if not context_path.exists():
        raise ValueError("loop/context_pack.md is required before planning tasks")
    if not task_titles:
        raise ValueError("at least one task title is required")

    tasks = []
    lines = []
    for index, title in enumerate(task_titles, start=1):
        task_id = f"T{index:03d}"
        tasks.append({"id": task_id, "title": title})
        lines.extend(
            [
                f"- [todo] {task_id}: {title}",
                "  Goal: loop/goal.md",
                "  Context: loop/context_pack.md",
                "  Verifier: run configured verifier commands",
                "",
            ]
        )
    (loop_dir / "tasks.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return tasks


def plan_tasks_with_command(root: Path, planner_command: str, timeout_seconds: int) -> list[dict[str, str]]:
    loop_dir = root / "loop"
    prompt = build_planner_prompt(root)
    rendered_command = planner_command.replace("{root}", shlex.quote(str(root)))
    result = loop_runner.run_shell_command(
        rendered_command,
        cwd=root,
        timeout=timeout_seconds,
        stdin_text=prompt,
    )
    if result.timed_out or result.returncode != 0:
        raise RuntimeError(f"planner command failed: {result.stderr or result.stdout}")
    titles = [
        line.strip().lstrip("-").strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    if not titles:
        raise RuntimeError("planner command produced no tasks")
    (loop_dir / "planner.log").write_text(
        loop_runner._format_command_result(result), encoding="utf-8"
    )
    return plan_tasks(root, titles)


def build_planner_prompt(root: Path) -> str:
    loop_dir = root / "loop"
    goal = (loop_dir / "goal.md").read_text(encoding="utf-8")
    context = (loop_dir / "context_pack.md").read_text(encoding="utf-8")
    return "\n".join(
        [
            "# Planner Task",
            "",
            "Decompose the goal into small implementation task titles.",
            "Return one task title per line. Do not include commentary.",
            "",
            "## Goal",
            goal,
            "",
            "## Context",
            context,
        ]
    )


def run_supervisor(
    *,
    root: Path,
    max_cycles: int,
    max_hours: float,
    max_consecutive_failures: int,
    sleep_seconds: float,
) -> dict[str, Any]:
    root = root.resolve()
    loop_dir = root / "loop"
    state_path = loop_dir / "supervisor_state.json"
    started_at = time.time()
    deadline = started_at + (max_hours * 3600)
    state = _load_supervisor_state(state_path)
    if not state:
        state = {
            "started_at": dt.datetime.now(dt.UTC).isoformat(),
            "max_cycles": max_cycles,
            "max_hours": max_hours,
            "events": [],
            "consecutive_failures": 0,
        }
    state["max_cycles"] = max_cycles
    state["max_hours"] = max_hours

    start_cycle = len(state.get("events", [])) + 1
    for cycle in range(start_cycle, max_cycles + 1):
        if time.time() >= deadline:
            return _finish_supervisor(root, state_path, state, "blocked", "max_wall_clock_time")

        worker = loop_runner.run_once(root=root, loop_dir=loop_dir)
        review_status = None
        stop_reason = None

        if worker.status in {"done", "review"}:
            review = review_loop.run_review_loop(root=root, loop_dir=loop_dir)
            review_status = review.status
            if review.status == "approved":
                state["consecutive_failures"] = 0
            else:
                state["consecutive_failures"] += 1
                stop_reason = "review_failed"
        elif worker.status in {"retry"}:
            state["consecutive_failures"] += 1
            stop_reason = "worker_retry"
        elif worker.status == "idle":
            return _finish_supervisor(root, state_path, state, "complete", "no_runnable_tasks")
        else:
            state["consecutive_failures"] += 1
            stop_reason = worker.message

        event = dataclasses.asdict(
            SupervisorEvent(
                cycle=cycle,
                worker_status=worker.status,
                review_status=review_status,
                stop_reason=stop_reason,
            )
        )
        state["events"].append(event)
        _save_supervisor_state(state_path, state)

        if state["consecutive_failures"] >= max_consecutive_failures:
            return _finish_supervisor(root, state_path, state, "blocked", "max_consecutive_failures")

        if _all_tasks_finished(loop_dir / "tasks.md"):
            return _finish_supervisor(root, state_path, state, "complete", "all_tasks_finished")

        if sleep_seconds:
            time.sleep(sleep_seconds)

    return _finish_supervisor(root, state_path, state, "blocked", "max_cycles_exhausted")


def _all_tasks_finished(tasks_path: Path) -> bool:
    _, tasks = loop_runner.parse_tasks(tasks_path)
    return bool(tasks) and all(task.status in {"done", "review"} for task in tasks)


def _finish_supervisor(
    root: Path,
    state_path: Path,
    state: dict[str, Any],
    status: str,
    stop_reason: str,
) -> dict[str, Any]:
    state["status"] = status
    state["stop_reason"] = stop_reason
    state["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
    _save_supervisor_state(state_path, state)
    _write_final_report(root, state)
    return {"status": status, "stop_reason": stop_reason, "cycles": len(state["events"])}


def _save_supervisor_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_supervisor_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def _write_final_report(root: Path, state: dict[str, Any]) -> None:
    loop_dir = root / "loop"
    _, tasks = loop_runner.parse_tasks(loop_dir / "tasks.md")
    completed = [task for task in tasks if task.status in {"done", "review"}]
    blocked = [task for task in tasks if task.status == "blocked"]
    lines = [
        "# Final Report",
        "",
        f"Status: {state.get('status')}",
        f"Stop reason: {state.get('stop_reason')}",
        "",
        "## Completed Tasks",
        _render_list([f"{task.id}: {task.title}" for task in completed]),
        "",
        "## Blocked Tasks",
        _render_list([f"{task.id}: {task.title}" for task in blocked]),
        "",
        "## Cycles",
        _render_list(
            [
                f"{event['cycle']}: worker={event['worker_status']} review={event.get('review_status')}"
                for event in state.get("events", [])
            ]
        ),
        "",
        "## Next Human Decisions",
        "- Review blocked tasks or raise budgets if progress is acceptable.",
    ]
    (loop_dir / "final_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Loop Workbench CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_goal = subparsers.add_parser("init-goal", help="Create loop/goal.md from a JSON contract")
    init_goal.add_argument("--input", required=True, help="Path to goal JSON")
    init_goal.add_argument("--root", default=".", help="Workspace root")

    context = subparsers.add_parser("ingest-context", help="Create loop/context_pack.md")
    context.add_argument("--root", default=".", help="Workspace root")
    context.add_argument("--file", action="append", default=[], help="Local file to ingest")
    context.add_argument("--url", action="append", default=[], help="URL to fetch and ingest")

    plan = subparsers.add_parser("plan-tasks", help="Create loop/tasks.md from task titles")
    plan.add_argument("--root", default=".", help="Workspace root")
    plan.add_argument("--planner-command", help="Command that outputs one task title per line")
    plan.add_argument("--timeout-seconds", type=int, default=900)
    plan.add_argument("titles", nargs="*")

    supervise = subparsers.add_parser("supervise", help="Run bounded supervisor cycles")
    supervise.add_argument("--root", default=".", help="Workspace root")
    supervise.add_argument("--max-hours", type=float, default=24)
    supervise.add_argument("--max-cycles", type=int, default=100)
    supervise.add_argument("--max-consecutive-failures", type=int, default=3)
    supervise.add_argument("--sleep-seconds", type=float, default=30)

    args = parser.parse_args(argv)
    root = Path(getattr(args, "root", ".")).resolve()

    if args.command == "init-goal":
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        create_goal_contract(root=root, **payload)
        print(json.dumps({"status": "created", "path": str(root / "loop" / "goal.md")}, indent=2))
        return 0
    if args.command == "ingest-context":
        result = create_context_pack(
            root=root,
            local_paths=[Path(item) for item in args.file],
            urls=args.url,
        )
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "plan-tasks":
        if args.planner_command:
            result = plan_tasks_with_command(
                root=root,
                planner_command=args.planner_command,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            result = plan_tasks(root=root, task_titles=args.titles)
        print(json.dumps({"tasks": result}, indent=2))
        return 0
    if args.command == "supervise":
        result = run_supervisor(
            root=root,
            max_cycles=args.max_cycles,
            max_hours=args.max_hours,
            max_consecutive_failures=args.max_consecutive_failures,
            sleep_seconds=args.sleep_seconds,
        )
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "complete" else 1
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
