from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

from agents.backend_adapter_v1 import (
    HostedModelClientAdapterV1,
    ValidatedBackendClientV1,
)
from agents.basic_agents import BasicAgentSuite
from agents.model_client import HostedModelClient
from contracts.registry import ContractRegistry
from orchestrator.tool_runner import ToolRequest, ToolResult, ToolRunner
from orchestrator.verification import FlakyPolicy, VerificationRunner
from orchestrator.versioning import VersioningAgent
from orchestrator.supervisor import PhaseResult, StateStore, Supervisor, Task, TaskQueue


@dataclass
class PluginResult:
    task_id: str
    status: str
    final_phase: str
    final_detail: str
    provenance_bundle_path: Optional[str]
    phase_history: list[dict]
    phase_agent_map: Dict[str, str]


class OpencodePipelinePlugin:
    """
    Native integration wrapper expected to be called by opencode prompt handlers.
    """

    def __init__(
        self,
        repo_root: str | Path,
        model_client: HostedModelClient,
        workspace_root: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.workspace_root = Path(workspace_root) if workspace_root else self.repo_root
        self.supervisor = self._build_supervisor(model_client=model_client)

    def handle_prompt(
        self, prompt: str, priority: str = "interactive", dry_run: bool = False
    ) -> PluginResult:
        task = self.supervisor.run_prompt(prompt=prompt, priority=priority, dry_run=dry_run)
        final = task.phase_history[-1]
        return PluginResult(
            task_id=task.task_id,
            status=task.status,
            final_phase=final.phase,
            final_detail=final.detail,
            provenance_bundle_path=task.provenance_bundle_path,
            phase_history=[
                {
                    "phase": item.phase,
                    "status": item.status,
                    "detail": item.detail,
                    "timestamp": item.timestamp,
                }
                for item in task.phase_history
            ],
            phase_agent_map=_phase_agent_map(),
        )

    def replay(self, provenance_bundle_path: str | Path) -> PluginResult:
        task = self.supervisor.replay_from_bundle(provenance_bundle_path)
        final = task.phase_history[-1] if task.phase_history else None
        return PluginResult(
            task_id=task.task_id,
            status=task.status,
            final_phase=final.phase if final else "none",
            final_detail=final.detail if final else "replayed without phases",
            provenance_bundle_path=task.provenance_bundle_path,
            phase_history=[
                {
                    "phase": item.phase,
                    "status": item.status,
                    "detail": item.detail,
                    "timestamp": item.timestamp,
                }
                for item in task.phase_history
            ],
            phase_agent_map=_phase_agent_map(),
        )

    def _build_supervisor(self, model_client: HostedModelClient) -> Supervisor:
        schemas = self.repo_root / "contracts" / "schemas"
        validated = ValidatedBackendClientV1.from_schema_paths(
            adapter=HostedModelClientAdapterV1(structured_client=model_client),
            request_schema_path=schemas / "backend_request_v1.json",
            response_schema_path=schemas / "backend_response_v1.json",
        )
        phase_handlers = BasicAgentSuite.from_validated_backend_v1(
            validated_client=validated,
            default_model="hosted-model-v1",
            strict_code_file_edits=True,
        ).build_phase_handlers()
        phase_handlers["intent"] = self._wrap_intent_handler(phase_handlers["intent"])

        versioning_agent = VersioningAgent(repo_path=self.workspace_root) if _is_git_repo(self.workspace_root) else None
        tool_runner = ToolRunner(
            handlers={
                "repo.search": self._handle_repo_search,
                "repo.write": self._handle_repo_write,
            },
            safe_tools={
                "repo.search",
                "repo.read",
                "repo.write",
                "test.run",
                "lint.run",
            },
        )
        verification_runner = VerificationRunner(
            checks=["pipeline.default"],
            check_executor=self._check_executor,
            flaky_policy=FlakyPolicy(
                max_retries=0,
                quarantine_after_failures=2,
                require_owner=False,
                require_expiry=False,
            ),
        )

        return Supervisor(
            queue=TaskQueue(),
            state_store=StateStore(self.workspace_root / ".opencode-pipeline" / "state.json"),
            contract_registry=ContractRegistry.from_directory(schemas),
            phase_handlers=phase_handlers,
            versioning_agent=versioning_agent,
            tool_runner=tool_runner,
            verification_runner=verification_runner,
        )

    def _wrap_intent_handler(
        self,
        base_handler: Callable[[Task], PhaseResult],
    ) -> Callable[[Task], PhaseResult]:
        def _handler(task: Task) -> PhaseResult:
            original_prompt = task.prompt
            task.prompt = self._intent_prompt_with_repo_context(original_prompt)
            try:
                result = base_handler(task)
            finally:
                task.prompt = original_prompt
            if result.status != "success":
                return result
            payload = task.agent_outputs.get("intent")
            if not isinstance(payload, dict):
                return PhaseResult(
                    phase="intent",
                    status="failed",
                    detail="intent_agent returned invalid payload",
                    timestamp=result.timestamp,
                )

            rewritten = payload.get("rewritten_prompt")
            rewritten_prompt = rewritten.strip() if isinstance(rewritten, str) else task.prompt
            raw_targets = payload.get("target_files")
            targets = [t.strip() for t in raw_targets if isinstance(t, str) and t.strip()] if isinstance(raw_targets, list) else []
            if not targets:
                targets = self._extract_filename_tokens(rewritten_prompt or task.prompt)

            resolved: list[str] = []
            ambiguous: list[str] = []
            missing: list[str] = []
            for target in targets:
                resolution = self._resolve_target_path(target)
                if resolution["status"] == "ok":
                    resolved.append(resolution["path"])  # type: ignore[index]
                elif resolution["status"] == "ambiguous":
                    candidates = ", ".join(resolution["candidates"][:5])  # type: ignore[index]
                    ambiguous.append(f"{target} -> {candidates}")
                else:
                    missing.append(target)

            if ambiguous:
                return PhaseResult(
                    phase="intent",
                    status="failed",
                    detail=(
                        "intent target file is ambiguous; specify exact path: "
                        + "; ".join(ambiguous)
                    ),
                    timestamp=result.timestamp,
                )

            if missing and resolved:
                return PhaseResult(
                    phase="intent",
                    status="failed",
                    detail=(
                        "intent target file(s) not found: "
                        + ", ".join(missing)
                        + ". Provide exact existing path(s)."
                    ),
                    timestamp=result.timestamp,
                )

            proposed_new = payload.get("proposed_new_files")
            new_files = (
                [p.strip() for p in proposed_new if isinstance(p, str) and p.strip()]
                if isinstance(proposed_new, list)
                else []
            )
            if missing and not resolved:
                inferred = self._infer_relevant_files(task.prompt, rewritten_prompt)
                if inferred:
                    payload["target_files"] = inferred
                    payload["rewritten_prompt"] = (
                        f"{rewritten_prompt}\n\n"
                        f"Use exact target files: {', '.join(inferred)}. "
                        "Return full file_edits for each touched target file."
                    )
                    payload["success_criteria"] = (
                        payload["success_criteria"] if isinstance(payload.get("success_criteria"), list) else []
                    )
                    payload["proposed_new_files"] = new_files
                    task.agent_outputs["intent"] = payload
                    return PhaseResult(
                        phase="intent",
                        status="success",
                        detail=f"intent_agent inferred target files from repo: {', '.join(inferred)}",
                        timestamp=result.timestamp,
                    )
                if new_files:
                    return PhaseResult(
                        phase="intent",
                        status="failed",
                        detail=(
                            "intent proposes creating new files: "
                            + ", ".join(new_files)
                            + ". Confirm by re-prompting with explicit paths and 'allow new files'."
                        ),
                        timestamp=result.timestamp,
                    )
                return PhaseResult(
                    phase="intent",
                    status="failed",
                    detail=(
                        "intent target file(s) not found: "
                        + ", ".join(missing)
                        + ". No clear matches were found in the repo. "
                        "Provide exact path(s) or confirm new file creation."
                    ),
                    timestamp=result.timestamp,
                )

            if resolved:
                payload["target_files"] = sorted(set(resolved))
                payload["rewritten_prompt"] = rewritten_prompt
            else:
                payload["target_files"] = []
                payload["rewritten_prompt"] = rewritten_prompt
            payload["success_criteria"] = (
                payload["success_criteria"] if isinstance(payload.get("success_criteria"), list) else []
            )
            payload["proposed_new_files"] = new_files
            task.agent_outputs["intent"] = payload
            return PhaseResult(
                phase="intent",
                status="success",
                detail=(
                    "intent_agent produced payload"
                    if not resolved
                    else f"intent_agent resolved target files: {', '.join(payload['target_files'])}"
                ),
                timestamp=result.timestamp,
            )

        return _handler

    def _intent_prompt_with_repo_context(self, user_prompt: str) -> str:
        candidates = self._intent_repo_candidates(limit=200)
        listing = "\n".join(f"- {path}" for path in candidates) if candidates else "- (no candidate files found)"
        return (
            f"{user_prompt}\n\n"
            "Intent-phase instructions:\n"
            "- First inspect the repository candidate file list below.\n"
            "- Choose target_files from existing paths when the request implies editing current files.\n"
            "- Only set proposed_new_files when new files/folders are truly required.\n"
            "- Do not invent generic filenames if suitable existing files are present.\n\n"
            "Repository candidate files:\n"
            f"{listing}\n"
        )

    def _handle_repo_search(self, request: ToolRequest) -> ToolResult:
        query = request.params.get("query", "").strip()
        if not query:
            return ToolResult(ok=False, detail="repo.search requires non-empty query")
        cmd = ["rg", "--line-number", "--hidden", "--glob", "!.git", query, str(self.workspace_root)]
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if proc.returncode not in (0, 1):
            return ToolResult(ok=False, detail=f"repo.search failed: {proc.stderr.strip()}")
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        preview = "\n".join(lines[:20]) if lines else "no matches"
        return ToolResult(ok=True, detail=f"repo.search completed ({len(lines)} matches)", output=preview)

    def _handle_repo_write(self, request: ToolRequest) -> ToolResult:
        rel_path = request.params.get("path", "").strip()
        content = request.params.get("content", "")
        if not rel_path:
            return ToolResult(ok=False, detail="repo.write requires path")
        if not isinstance(content, str):
            return ToolResult(ok=False, detail="repo.write requires string content")
        target = (self.workspace_root / rel_path).resolve()
        workspace = self.workspace_root.resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            return ToolResult(ok=False, detail=f"repo.write path escapes workspace: {rel_path}")
        allow_new = os.getenv("OPENCODE_PIPELINE_ALLOW_NEW_FILES", "").strip().lower() in {"1", "true", "yes"}
        if not target.exists() and not allow_new:
            remapped = self._resolve_existing_path_from_basename(rel_path)
            if remapped is not None:
                target = remapped
            else:
                basename = Path(rel_path).name
                candidates = self._find_existing_paths_by_basename(basename)
                if len(candidates) > 1:
                    sample = ", ".join(candidates[:5])
                    return ToolResult(
                        ok=False,
                        detail=(
                            f"repo.write ambiguous path '{rel_path}'. "
                            f"Found multiple existing '{basename}' files: {sample}"
                        ),
                    )
            if not target.exists():
                return ToolResult(
                    ok=False,
                    detail=(
                        f"repo.write blocked for new file '{rel_path}'. "
                        "Set OPENCODE_PIPELINE_ALLOW_NEW_FILES=1 to allow creating new files."
                    ),
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if str(target.relative_to(workspace)) != rel_path:
            return ToolResult(
                ok=True,
                detail=f"wrote {target} (resolved from '{rel_path}')",
            )
        return ToolResult(ok=True, detail=f"wrote {target}")

    def _resolve_existing_path_from_basename(self, rel_path: str) -> Optional[Path]:
        basename = Path(rel_path).name
        if not basename:
            return None
        candidates = self._find_existing_paths_by_basename(basename)
        if len(candidates) != 1:
            return None
        return (self.workspace_root / candidates[0]).resolve()

    def _resolve_target_path(self, path_hint: str) -> Dict:
        direct = (self.workspace_root / path_hint).resolve()
        workspace = self.workspace_root.resolve()
        try:
            direct.relative_to(workspace)
        except ValueError:
            return {"status": "missing"}
        if direct.is_file():
            return {"status": "ok", "path": str(direct.relative_to(workspace))}
        basename = Path(path_hint).name
        candidates = self._find_existing_paths_by_basename(basename)
        if len(candidates) == 1:
            return {"status": "ok", "path": candidates[0]}
        if len(candidates) > 1:
            return {"status": "ambiguous", "candidates": candidates}
        return {"status": "missing"}

    def _find_existing_paths_by_basename(self, basename: str) -> list[str]:
        if not basename:
            return []
        out: list[str] = []
        for match in self.workspace_root.rglob(basename):
            if not match.is_file():
                continue
            if ".git" in match.parts:
                continue
            out.append(str(match.relative_to(self.workspace_root)))
        out.sort()
        return out

    @staticmethod
    def _extract_filename_tokens(text: str) -> list[str]:
        if not text:
            return []
        matches = re.findall(r"\b[\w./-]+\.[a-zA-Z0-9]+\b", text)
        out: list[str] = []
        for item in matches:
            clean = item.strip().strip(".,:;!?()[]{}\"'")
            if clean and clean not in out:
                out.append(clean)
        return out

    def _infer_relevant_files(self, user_prompt: str, rewritten_prompt: str) -> list[str]:
        keywords = self._keywords(user_prompt + " " + rewritten_prompt)
        candidates: list[tuple[int, str]] = []
        preferred_ext = {".html", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
        for path in self.workspace_root.rglob("*"):
            if not path.is_file():
                continue
            if ".git" in path.parts:
                continue
            if path.suffix.lower() not in preferred_ext:
                continue
            rel = str(path.relative_to(self.workspace_root))
            lower = rel.lower()
            score = 0
            if any(part in lower for part in ("web", "client", "frontend", "ui", "canvas")):
                score += 3
            score += sum(1 for kw in keywords if kw in lower)
            if path.name.lower() in {"index.html", "main.css", "app.tsx", "app.jsx", "app.js"}:
                score += 1
            if score > 0:
                candidates.append((score, rel))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        top = [rel for _, rel in candidates[:3]]
        return top

    def _intent_repo_candidates(self, limit: int = 200) -> list[str]:
        preferred_ext = {".html", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".py"}
        candidates: list[str] = []
        for path in self.workspace_root.rglob("*"):
            if not path.is_file():
                continue
            if ".git" in path.parts:
                continue
            if path.suffix.lower() not in preferred_ext:
                continue
            rel = str(path.relative_to(self.workspace_root))
            candidates.append(rel)
            if len(candidates) >= limit:
                break
        candidates.sort()
        return candidates

    @staticmethod
    def _keywords(text: str) -> list[str]:
        tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
        stop = {
            "the",
            "and",
            "with",
            "that",
            "just",
            "want",
            "make",
            "into",
            "from",
            "for",
            "this",
            "web",
            "interface",
        }
        out: list[str] = []
        for token in tokens:
            if token in stop:
                continue
            if token not in out:
                out.append(token)
        return out

    def _check_executor(self, check_name: str) -> bool:
        command = _check_command(check_name)
        if not command:
            return True
        proc = subprocess.run(
            command,
            cwd=self.workspace_root,
            text=True,
            capture_output=True,
            check=False,
            shell=True,
        )
        return proc.returncode == 0


def _phase_agent_map() -> Dict[str, str]:
    mapping = dict(BasicAgentSuite.AGENT_PHASES)
    mapping.update(
        {
            "versioning": "versioning_agent",
            "execute": "tool_runner(repo.write/repo.search)",
            "verify": "verification_runner",
            "human_checkpoints": "approval_manager",
            "gate_pre_merge": "policy_engine",
            "finalize": "supervisor",
        }
    )
    return mapping


def _is_git_repo(path: Path) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _check_command(check_name: str) -> Optional[str]:
    key = check_name.strip().upper().replace("-", "_")
    env_key = f"OPENCODE_PIPELINE_CHECK_{key}"
    if env_key in os.environ:
        return os.environ[env_key].strip() or None
    defaults = {
        "unit": None,
        "lint": None,
    }
    return defaults.get(check_name)
