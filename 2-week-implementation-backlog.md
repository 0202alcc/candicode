# 2-Week Implementation Backlog (Laptop Opencode v1)

## Week 1

1. `P0` Create `pipeline_config` + schema validation.
- Deliverable: typed config file + validator CLI.
- Dependency: none.
- Done when: invalid config fails with actionable errors.

2. `P0` Build orchestrator skeleton.
- Deliverable: supervisor loop with task IDs, queue class, state load/save.
- Dependency: `#1`.
- Done when: one prompt can run through stubbed phases end-to-end.

3. `P0` Implement policy gate engine (fail-closed).
- Deliverable: requirements/CI/security/QA/perf/review/merge/rollout gates.
- Dependency: `#1`, `#2`.
- Done when: gate outcomes are deterministic and block appropriately.

4. `P0` Add Versioning Agent and git branch policy.
- Deliverable: auto branch `codex/<task-id>-<slug>`, block direct `main` edits.
- Dependency: `#2`, `#3`.
- Done when: every task creates/uses isolated branch.

5. `P0` Tool runner wrapper with permission boundaries.
- Deliverable: controlled interfaces for search/tests/lint/git/scanners.
- Dependency: `#2`.
- Done when: all tool calls flow through wrappers (no direct shell escapes).

6. `P1` Agent handoff contracts (JSON schemas).
- Deliverable: per-agent input/output schema + runtime validation.
- Dependency: `#2`.
- Done when: invalid handoff is rejected and logged.

7. `P1` Basic agent modules on one hosted model.
- Deliverable: triage/requirements/planner/coder/test/reviewer/docs.
- Dependency: `#2`, `#6`.
- Done when: simple bugfix path completes with generated trace.

## Week 2

8. `P0` Deterministic verification pipeline.
- Deliverable: standard CI command set + flaky-test governance metadata.
- Dependency: `#3`, `#5`, `#7`.
- Done when: same input gives stable gate outcomes.

9. `P0` Human checkpoint UX.
- Deliverable: structured approvals for requirements, waivers, merge, rollout.
- Dependency: `#3`, `#4`, `#7`.
- Done when: merge blocked until explicit structured approval.

10. `P1` Observability + immutable audit trail.
- Deliverable: structured event log, artifact hashes, provenance bundle.
- Dependency: `#3`, `#4`, `#7`.
- Done when: every task has reconstructible timeline.

11. `P1` Resilience controls.
- Deliverable: retry/fallback/degrade/safe-mode + task budgets.
- Dependency: `#2`, `#3`, `#7`.
- Done when: model/tool failure triggers controlled fallback path.

12. `P1` Dry-run and replay modes.
- Deliverable: no-write mode + replay from recorded artifacts.
- Dependency: `#10`.
- Done when: task can be replayed without live side effects.

13. `P0` Validation scenario suite.
- Deliverable: scripted scenarios for bugfix, refactor, migration, security block, rollback.
- Dependency: `#4`, `#8`, `#9`, `#11`.
- Done when: expected gate/approval behavior passes in all scenarios.

14. `P0` Freeze v1 interfaces for future PC LLM swap.
- Deliverable: stable backend adapter contract and trace schema.
- Dependency: `#7`, `#10`, `#11`, `#13`.
- Done when: swapping model backend requires no orchestrator logic changes.

## Critical Path

1. `#1 -> #2 -> #3 -> #4 -> #8 -> #9 -> #13 -> #14`

## Suggested Initial Issue List (copy-ready)

1. `feat(config): add pipeline config + schema validator`
2. `feat(orchestrator): implement supervisor task loop and state packets`
3. `feat(policy): implement fail-closed gate engine`
4. `feat(versioning): enforce per-task branch workflow`
5. `feat(tools): wrap tool execution with policy guardrails`
6. `feat(contracts): enforce JSON schema handoffs between agents`
7. `feat(agents): add v1 triage/plan/code/test/review/docs agents`
8. `feat(ci): deterministic verification + flaky test policy`
9. `feat(ux): structured approval checkpoints`
10. `feat(audit): provenance bundle + immutable event trail`
11. `feat(resilience): fallback chain + safe mode + budgets`
12. `feat(debug): dry-run and replay execution modes`
13. `test(e2e): add pipeline scenario suite`
14. `chore(api): freeze v1 backend adapter contract`
