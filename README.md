# Local Loop Engineering Workflow

This repository contains a small, inspectable queue worker for learning loop
engineering with coding agents.

The runner owns the outer loop:

```text
read task -> generate prompt -> run Codex -> run verifiers -> retry or stop
```

The agent only works on the current task. It does not choose arbitrary new
work; the queue and verifier feedback decide what happens next.

## Files

- `loop_runner.py`: local Python queue worker.
- `loop/project.md`: project goal, constraints, and definition of done.
- `loop/tasks.md`: markdown queue using `todo`, `running`, `blocked`,
  `review`, and `done`.
- `loop/config.yaml`: budgets, Codex command, verifier commands, and safety
  paths.
- `loop/state.json`: persisted loop state.
- `loop/runs/`: generated prompts, agent logs, verifier logs, and diffs.
- `review_loop.py`: independent reviewer/fixer loop for improving the workflow.
- `loop/review_config.yaml`: reviewer/fixer commands, iteration limits, and
  approval marker.
- `workbench.py`: goal contract, context ingestion, task planning, and bounded
  supervisor CLI.
- `loop/goal.md`: PRD-style agreement before autonomous execution.
- `loop/context_pack.md`: inspectable context assembled from files and URLs.

## First Run

Inspect the generated prompt without running Codex:

```bash
python3 loop_runner.py --dry-run
```

Run one loop attempt:

```bash
python3 loop_runner.py
```

The runner intentionally performs one bounded attempt per invocation. Run it
again to retry a failed task while budget remains.

## Goal-To-Run Pipeline

Create a goal contract from JSON:

```bash
python3 workbench.py init-goal --input goal-input.json
```

Ingest local files and URLs into an inspectable context pack:

```bash
python3 workbench.py ingest-context --file notes.md --url https://example.com/spec
```

Create a task queue from agreed task titles:

```bash
python3 workbench.py plan-tasks "Create contract CLI" "Add supervisor report"
```

Or let a planner command decompose the goal/context into one task title per
line:

```bash
python3 workbench.py plan-tasks --planner-command "codex exec --cd {root} --sandbox read-only --ask-for-approval never -"
```

Run bounded autonomous cycles:

```bash
python3 workbench.py supervise --max-hours 24 --max-cycles 100 --max-consecutive-failures 3
```

The supervisor repeatedly runs one worker attempt, verifier checks, and the
reviewer/fixer loop. It writes `loop/supervisor_state.json` and
`loop/final_report.md`.

## Reviewer/Fixer Loop

Use this when you want a separate reviewer agent and fixer agent to iterate on
the workflow itself:

```bash
python3 review_loop.py
```

The default loop runs at least 5 independent review iterations and at most 10.
Each iteration writes prompts, reviews, fixer logs, and verifier logs under
`loop/review-runs/`.

The roles are deliberately separated:

- reviewer: read-only Codex run that reports blocking findings
- fixer: workspace-write Codex run that fixes only the current reviewer findings
- verifier: deterministic local commands from `loop/review_config.yaml`

The loop stops only when the reviewer includes the configured approval marker
after the minimum iteration count, or when the maximum iteration count is
reached without approval.

## Task Format

Use one task per queue item:

```markdown
- [todo] T001: Add a health check endpoint
  Scope: only touch src and tests
  Verifier: test command must pass
```

The runner picks an existing `running` task first. If none exists, it promotes
the first `todo` task to `running`.

## Safety Model

Keep the loop boring:

- one task at a time
- deterministic verifier commands
- small retry budget
- changed-file limit
- persistent logs for every attempt
- human review for successful tasks by default
- separate reviewer/fixer roles for improvement loops
