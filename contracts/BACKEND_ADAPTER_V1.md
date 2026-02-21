# Backend Adapter v1 (Frozen)

This repository freezes the backend integration contract at `v1` so the orchestrator can swap hosted/local model providers without changing supervisor logic.

## Contract files

- `contracts/schemas/backend_request_v1.json`
- `contracts/schemas/backend_response_v1.json`
- `contracts/schemas/trace_event_v1.json`

## Compatibility rules

1. `version` MUST be `v1` in both request and response payloads.
2. `request_id` MUST round-trip from request to response.
3. `trace_events` MUST use the `trace_event_v1` shape and enum values.
4. Orchestrator integrations MUST validate request and response payloads before use.
5. Additive changes require a new version (for example, `v2`) if they break existing validations.

## Stable adapter API

Use `agents/backend_adapter_v1.py`:

- `build_agent_request_v1(...)`
- `ValidatedBackendClientV1.chat(...)`
- `BackendAdapterV1` protocol
