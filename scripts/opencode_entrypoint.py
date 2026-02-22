#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.model_client import StaticHostedModelClient
from agents.provider_client import OpenAICompatibleHostedModelClient
from integration.opencode_plugin import OpencodePipelinePlugin


def _static_responses() -> dict:
    return {
        "intent_agent": {
            "rewritten_prompt": "Translate request into concrete file-targeted implementation instructions.",
            "target_files": [],
            "success_criteria": ["Return file_edits with exact paths and full content."],
            "proposed_new_files": [],
        },
        "triage_agent": {
            "task_type": "bug",
            "risk_level": "low",
            "scope_size": "small",
            "intensity": "normal",
        },
        "requirements_agent": {
            "acceptance_criteria": ["ac1"],
            "non_goals": [],
            "open_questions": [],
        },
        "planner_agent": {
            "steps": ["step1"],
            "test_plan": ["unit"],
            "risk_notes": [],
        },
        "coder_agent": {
            "changes": ["chg"],
            "files_touched": [],
            "file_edits": [],
        },
        "test_agent": {"tests_added": ["tests/test_file.py"], "checks_to_run": ["unit"]},
        "reviewer_agent": {
            "findings": [],
            "risk_summary": "low risk",
            "required_fixes": [],
        },
        "docs_agent": {
            "changelog": "updated",
            "runbook_delta": "none",
            "migration_notes": "none",
        },
    }


def _resolve_client_mode(cli_value: str, provider_id: str | None = None, model_id: str | None = None) -> str:
    if cli_value != "auto":
        return cli_value
    provider = (provider_id or "").strip()
    model = (model_id or "").strip()
    if provider and model:
        if _is_local_provider(provider):
            return "provider"
        if _has_provider_api_key(provider):
            return "provider"
        return "static"
    has_provider_env = bool(
        os.getenv("OPENCODE_LLM_API_KEY", "").strip()
        and os.getenv("OPENCODE_LLM_MODEL", "").strip()
    )
    return "provider" if has_provider_env else "static"


def _provider_env_prefix(provider_id: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", provider_id.strip()).upper()
    return f"OPENCODE_PROVIDER_{normalized}_"


def _has_provider_api_key(provider_id: str) -> bool:
    prefix = _provider_env_prefix(provider_id)
    return bool(
        os.getenv(f"{prefix}API_KEY", "").strip()
        or os.getenv("OPENCODE_LLM_API_KEY", "").strip()
    )


def _is_local_provider(provider_id: str | None) -> bool:
    if not provider_id:
        return False
    configured = os.getenv("OPENCODE_LOCAL_PROVIDER_IDS", "localpc,local,ollama,llama_cpp")
    ids = {item.strip().lower() for item in configured.split(",") if item.strip()}
    return provider_id.strip().lower() in ids


def _resolve_provider_model(args: argparse.Namespace) -> str:
    model = (args.model_id or os.getenv("OPENCODE_LLM_MODEL", "")).strip()
    if not model:
        raise ValueError(
            "Missing model id. Set --model-id or OPENCODE_LLM_MODEL."
        )
    return model


def _resolve_provider_settings(args: argparse.Namespace) -> dict:
    provider_id = (args.provider_id or os.getenv("OPENCODE_LLM_PROVIDER", "")).strip() or None
    prefix = _provider_env_prefix(provider_id) if provider_id else ""
    model = _resolve_provider_model(args)

    def get(suffix: str, fallback_env: str, default: str = "") -> str:
        if prefix:
            value = os.getenv(f"{prefix}{suffix}", "").strip()
            if value:
                return value
        value = os.getenv(fallback_env, "").strip()
        if value:
            return value
        return default

    local_provider = _is_local_provider(provider_id)
    base_url_default = "http://127.0.0.1:11434/v1" if local_provider else "https://api.openai.com/v1"
    base_url = get("BASE_URL", "OPENCODE_LLM_BASE_URL", base_url_default)
    api_key = get("API_KEY", "OPENCODE_LLM_API_KEY", "")
    timeout_raw = get("TIMEOUT_SECONDS", "OPENCODE_LLM_TIMEOUT_SECONDS", "60")
    timeout_seconds = int(timeout_raw)
    headers_json = get("HEADERS_JSON", "OPENCODE_LLM_HEADERS_JSON", "")
    headers = None
    if headers_json:
        try:
            parsed_headers = json.loads(headers_json)
            if isinstance(parsed_headers, dict):
                headers = {str(k): str(v) for k, v in parsed_headers.items()}
        except Exception:
            headers = None
    require_api_key = not local_provider
    if require_api_key and not api_key:
        target = provider_id or "provider"
        raise ValueError(
            f"Missing API key for {target}. Set {prefix + 'API_KEY' if prefix else 'OPENCODE_LLM_API_KEY'}."
        )

    return {
        "provider_id": provider_id,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "timeout_seconds": timeout_seconds,
        "headers": headers,
    }


def _provider_error_text(detail: str) -> bool:
    text = detail.lower()
    markers = [
        "provider connection error",
        "provider http error",
        "certificate verify failed",
        "ssl:",
        "urlopen error",
        "connection refused",
        "timed out",
        "unauthorized",
        "invalid api key",
        "model not found",
    ]
    return any(marker in text for marker in markers)


def _fetch_accessible_provider_models(settings: dict) -> tuple[set[str], str | None]:
    url = f"{settings['base_url'].rstrip('/')}/models"
    headers = {"Content-Type": "application/json"}
    api_key = settings.get("api_key", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    extra_headers = settings.get("headers")
    if isinstance(extra_headers, dict):
        headers.update({str(k): str(v) for k, v in extra_headers.items()})
    req = urllib.request.Request(url=url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(
            req,
            timeout=settings.get("timeout_seconds", 60),
            context=_build_ssl_context(),
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return set(), f"model discovery HTTP error {exc.code}: {body}"
    except urllib.error.URLError as exc:
        return set(), f"model discovery connection error: {exc}"
    except Exception as exc:
        return set(), f"model discovery failed: {exc}"

    models: set[str] = set()
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    models.add(model_id.strip())
    return models, None


def _configured_fallback_models() -> list[str]:
    raw = (
        os.getenv("OPENCODE_PIPELINE_FALLBACK_MODELS", "").strip()
        or os.getenv("OPENCODE_LLM_FALLBACK_MODELS", "").strip()
    )
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _model_candidates(primary: str, accessible: set[str]) -> list[str]:
    candidates: list[str] = []
    for model in [primary, *_configured_fallback_models()]:
        if not model:
            continue
        if accessible and model not in accessible:
            continue
        if model not in candidates:
            candidates.append(model)
    # Extend with any other accessible models so runtime can continue after transient/provider-specific failures.
    for model in sorted(accessible):
        if model not in candidates:
            candidates.append(model)
    if not candidates and accessible:
        # Fallback to any known-accessible models if explicit candidates were filtered out.
        candidates.extend(sorted(accessible))
    return candidates


def _emit_progress(event: dict) -> None:
    print(f"::pipeline-progress::{json.dumps(event, sort_keys=True)}", file=sys.stderr, flush=True)


def _build_ssl_context() -> ssl.SSLContext:
    cafile = _resolve_ca_bundle()
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def _resolve_ca_bundle() -> str | None:
    for env_key in ("OPENCODE_LLM_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        value = os.getenv(env_key, "").strip()
        if value and os.path.isfile(value):
            return value
    try:
        import certifi  # type: ignore

        certifi_path = certifi.where()
        if certifi_path and os.path.isfile(certifi_path):
            return certifi_path
    except Exception:
        pass
    return None


def _provider_retry_attempts() -> int:
    raw = os.getenv("OPENCODE_PIPELINE_PROVIDER_RETRY_ATTEMPTS", "2").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 2
    return max(1, min(value, 5))


def main() -> int:
    parser = argparse.ArgumentParser(description="Native opencode pipeline entrypoint.")
    parser.add_argument("prompt", help="Prompt to process through pipeline.")
    parser.add_argument("--repo-root", default=str(ROOT), help="Repo root containing pipeline.")
    parser.add_argument(
        "--workspace-root",
        default=os.getcwd(),
        help="Workspace root to modify/test (defaults to current working directory).",
    )
    parser.add_argument("--priority", default="interactive", help="Task priority.")
    parser.add_argument("--dry-run", action="store_true", help="Run pipeline in dry-run mode.")
    parser.add_argument("--provider-id", default=None, help="Selected provider id (from /model).")
    parser.add_argument("--model-id", default=None, help="Selected model id (from /model).")
    parser.add_argument(
        "--model-client",
        choices=("auto", "provider", "static"),
        default="auto",
        help="Model client backend. auto chooses provider when required env vars are set.",
    )
    args = parser.parse_args()

    try:
        mode = _resolve_client_mode(args.model_client, provider_id=args.provider_id, model_id=args.model_id)
        fallback_note = None

        if mode == "provider":
            try:
                settings = _resolve_provider_settings(args)
            except ValueError as exc:
                if args.model_client != "auto":
                    raise
                _emit_progress(
                    {
                        "phase": "model_switch",
                        "status": "running",
                        "detail": f"provider unavailable; falling back to static ({exc})",
                        "agent": "opencode_entrypoint",
                        "timestamp": time.time(),
                    }
                )
                model_client = StaticHostedModelClient(responses=_static_responses())
                plugin = OpencodePipelinePlugin(
                    repo_root=args.repo_root,
                    model_client=model_client,
                    workspace_root=args.workspace_root,
                )
                result = plugin.handle_prompt(
                    prompt=args.prompt,
                    priority=args.priority,
                    dry_run=args.dry_run,
                    progress_callback=_emit_progress,
                )
                result.final_detail = f"{result.final_detail}\nprovider fallback: {exc}"
                print(json.dumps(result.__dict__, indent=2, sort_keys=True))
                return 0

            accessible_models, discovery_error = _fetch_accessible_provider_models(settings)
            if discovery_error:
                fallback_note = discovery_error
            candidates = _model_candidates(settings["model"], accessible_models)
            if not candidates:
                if args.model_client == "auto":
                    _emit_progress(
                        {
                            "phase": "model_switch",
                            "status": "running",
                            "detail": "no accessible provider fallback models; using static fallback",
                            "agent": "opencode_entrypoint",
                            "timestamp": time.time(),
                        }
                    )
                    model_client = StaticHostedModelClient(responses=_static_responses())
                    plugin = OpencodePipelinePlugin(
                        repo_root=args.repo_root,
                        model_client=model_client,
                        workspace_root=args.workspace_root,
                    )
                    result = plugin.handle_prompt(
                        prompt=args.prompt,
                        priority=args.priority,
                        dry_run=args.dry_run,
                        progress_callback=_emit_progress,
                    )
                    if fallback_note:
                        result.final_detail = f"{result.final_detail}\n{fallback_note}"
                    print(json.dumps(result.__dict__, indent=2, sort_keys=True))
                    return 0
                raise ValueError("No accessible provider models available")

            last_failure = None
            for index, model_name in enumerate(candidates):
                _emit_progress(
                    {
                        "phase": "model_switch",
                        "status": "running",
                        "detail": f"using provider model {model_name}",
                        "agent": "opencode_entrypoint",
                        "timestamp": time.time(),
                    }
                )
                model_client = OpenAICompatibleHostedModelClient(
                    api_key=settings["api_key"],
                    model=model_name,
                    base_url=settings["base_url"],
                    timeout_seconds=settings["timeout_seconds"],
                    headers=settings.get("headers"),
                )
                plugin = OpencodePipelinePlugin(
                    repo_root=args.repo_root,
                    model_client=model_client,
                    workspace_root=args.workspace_root,
                )
                attempts = _provider_retry_attempts()
                result = None
                for attempt in range(1, attempts + 1):
                    if attempt > 1:
                        _emit_progress(
                            {
                                "phase": "model_switch",
                                "status": "running",
                                "detail": f"retrying provider model {model_name} (attempt {attempt}/{attempts})",
                                "agent": "opencode_entrypoint",
                                "timestamp": time.time(),
                            }
                        )
                    result = plugin.handle_prompt(
                        prompt=args.prompt,
                        priority=args.priority,
                        dry_run=args.dry_run,
                        progress_callback=_emit_progress,
                    )
                    if not (result.status == "blocked" and _provider_error_text(result.final_detail)):
                        break
                assert result is not None
                if result.status == "blocked" and _provider_error_text(result.final_detail):
                    last_failure = result.final_detail
                    if index < len(candidates) - 1:
                        _emit_progress(
                            {
                                "phase": "model_switch",
                                "status": "running",
                                "detail": f"provider failure; retrying with next accessible model (previous: {model_name})",
                                "agent": "opencode_entrypoint",
                                "timestamp": time.time(),
                            }
                        )
                        continue
                    break

                if fallback_note:
                    result.final_detail = f"{result.final_detail}\n{fallback_note}"
                print(json.dumps(result.__dict__, indent=2, sort_keys=True))
                return 0

            if args.model_client == "auto":
                _emit_progress(
                    {
                        "phase": "model_switch",
                        "status": "running",
                        "detail": "all accessible provider models failed; switching to static fallback",
                        "agent": "opencode_entrypoint",
                        "timestamp": time.time(),
                    }
                )
                model_client = StaticHostedModelClient(responses=_static_responses())
                plugin = OpencodePipelinePlugin(
                    repo_root=args.repo_root,
                    model_client=model_client,
                    workspace_root=args.workspace_root,
                )
                result = plugin.handle_prompt(
                    prompt=args.prompt,
                    priority=args.priority,
                    dry_run=args.dry_run,
                    progress_callback=_emit_progress,
                )
                details = []
                if fallback_note:
                    details.append(fallback_note)
                if last_failure:
                    details.append(f"provider failure: {last_failure}")
                if details:
                    result.final_detail = f"{result.final_detail}\n" + "\n".join(details)
                print(json.dumps(result.__dict__, indent=2, sort_keys=True))
                return 0

            raise ValueError(last_failure or "Provider request failed")

        model_client = StaticHostedModelClient(responses=_static_responses())
        plugin = OpencodePipelinePlugin(
            repo_root=args.repo_root,
            model_client=model_client,
            workspace_root=args.workspace_root,
        )
        result = plugin.handle_prompt(
            prompt=args.prompt,
            priority=args.priority,
            dry_run=args.dry_run,
            progress_callback=_emit_progress,
        )
        print(json.dumps(result.__dict__, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        payload = {
            "status": "failed",
            "final_phase": "entrypoint",
            "final_detail": str(exc),
            "provenance_bundle_path": None,
            "phase_history": [
                {
                    "phase": "entrypoint",
                    "status": "failed",
                    "detail": str(exc),
                }
            ],
            "phase_agent_map": {"entrypoint": "opencode_entrypoint"},
            "task_id": "entrypoint-error",
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
