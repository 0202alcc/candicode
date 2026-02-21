# Native Opencode Integration (Option 3)

This document describes how to wire the pipeline as the default prompt path inside opencode core.

## Goal

Replace direct model calls in opencode prompt handling with:

`OpencodePipelinePlugin.handle_prompt(...)`

## Integration point

In opencode core, patch the component that currently handles:

1. user prompt ingestion
2. model invocation
3. output streaming/final message

## Required patch pattern

1. Initialize plugin at app boot:

```python
from integration.opencode_plugin import OpencodePipelinePlugin
from your_real_hosted_client import RealHostedClient

plugin = OpencodePipelinePlugin(
    repo_root=current_workspace_root,
    model_client=RealHostedClient(...)
)
```

2. Route prompt handling through plugin:

```python
result = plugin.handle_prompt(
    prompt=user_prompt,
    priority="interactive",
    dry_run=False,
)
```

3. Render pipeline result in UI:

- `result.status`
- `result.final_phase`
- `result.final_detail`
- `result.provenance_bundle_path`

4. (Optional) Add replay action:

```python
replay = plugin.replay(provenance_bundle_path)
```

## Contract stability

The backend must conform to frozen v1 schemas:

- `contracts/schemas/backend_request_v1.json`
- `contracts/schemas/backend_response_v1.json`
- `contracts/schemas/trace_event_v1.json`

Use `ValidatedBackendClientV1` to enforce this boundary.

## Validation commands

```bash
python3 scripts/validate_backend_contract_v1.py
python3 scripts/run_pipeline_scenarios.py
python3 -m unittest discover -s tests -p "test_*.py" -v
```
