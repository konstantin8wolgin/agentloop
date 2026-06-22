# Project

## Goal

Build a new project through small, verified tasks.

The agreed product contract lives in `loop/goal.md`.
The inspectable source context lives in `loop/context_pack.md`.

## Tech Stack

Set this before starting the loop. Examples:

- Python CLI
- TypeScript web app
- Rust service
- Documentation site

## Hard Constraints

- Work only on the current task from `loop/tasks.md`.
- Prefer small changes that are easy to review.
- Do not start unrelated tasks.
- Do not edit secrets, credentials, or files outside the configured scope.
- Leave generated prompts, logs, and verifier output in `loop/runs/`.

## Definition Of Done

A task is done when:

- the requested behavior is implemented,
- all configured verifier commands pass,
- changed files stay within the configured budget,
- the task is moved to `review` or `done`.
