# CandiCode Project Rules

These project rules define how CandiCode agents collaborate inside this repository.

## Workflow Contract

- Treat the pipeline as canonical: `intent -> triage -> requirements -> plan -> code -> test -> review -> docs -> execute -> verify -> gate_pre_merge -> finalize`.
- Keep `intent_agent` as the default human-facing entrypoint.
- Use primary agents only for deliberate user mode switches.
- Use subagents for behind-the-scenes phase execution and orchestration.

## Quality Bar

- Prefer incremental changes with deterministic behavior.
- Run relevant validation before finalize:
  - unit tests for changed behavior
  - lint/format checks when available
  - schema/contract validation for pipeline payload changes
- Do not merge if required gate checks fail.

## Safety and Git

- Never perform destructive git/file operations unless explicitly requested.
- Never commit directly to protected branches.
- Keep branch naming aligned with `codex/*` conventions.

## Agent Delegation Rules

- `intent_agent` may delegate to: `triage_agent`, `requirements_agent`, `planner_agent`.
- `planner_agent` may delegate to: `triage_agent`, `requirements_agent`.
- `coder_agent` may delegate to: `test_agent`, `reviewer_agent`, `docs_agent`.
- `reviewer_agent` may delegate to: `coder_agent`, `test_agent`.
- `supervisor_agent` may delegate to all pipeline subagents.
- All other subagents should avoid delegation unless explicitly required by supervisor policy.

## Model Routing Policy

- `intent_agent`: primary `openai/gpt-5`, fallback `openai/gpt-5-mini`.
- `planner_agent`: primary `openai/gpt-5`, fallback `openai/gpt-5-mini`.
- `coder_agent`: primary `anthropic/claude-sonnet-4.5`, fallback `openai/gpt-5-mini`.
- `reviewer_agent`: primary `openai/gpt-5`, fallback `anthropic/claude-sonnet-4.5`.
- `triage_agent`: primary `openai/gpt-5-mini`, fallback `anthropic/claude-haiku-4.5`.
- `requirements_agent`: primary `openai/gpt-5-mini`, fallback `anthropic/claude-haiku-4.5`.
- `test_agent`: primary `anthropic/claude-sonnet-4.5`, fallback `openai/gpt-5-mini`.
- `docs_agent`: primary `openai/gpt-5-mini`, fallback `anthropic/claude-haiku-4.5`.
- orchestration agents (`versioning/execute/verify/human_checkpoints/gate_pre_merge/finalize/supervisor`): primary `openai/gpt-5-mini`, fallback `anthropic/claude-haiku-4.5`.

## LSP Policy

- LSP is required for `coder_agent` and `reviewer_agent`.
- LSP is optional for `test_agent`.
- LSP is unnecessary for intent/triage/requirements/docs/orchestration agents.

## Output Discipline

- Keep user-facing status updates concise and concrete.
- Include file paths and changed artifacts when summarizing work.
- Call out assumptions and unresolved risks explicitly.
