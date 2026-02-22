from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class AuditEvent:
    timestamp: float
    event_type: str
    task_id: str
    payload: Dict
    prev_hash: str
    record_hash: str


class AuditLogger:
    """
    Append-only event log with a hash chain for tamper evidence.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash = self._load_last_hash()

    def emit(self, event_type: str, task_id: str, payload: Dict) -> AuditEvent:
        timestamp = time.time()
        prev_hash = self._last_hash
        record_hash = self._hash_event(
            timestamp=timestamp,
            event_type=event_type,
            task_id=task_id,
            payload=payload,
            prev_hash=prev_hash,
        )
        event = AuditEvent(
            timestamp=timestamp,
            event_type=event_type,
            task_id=task_id,
            payload=payload,
            prev_hash=prev_hash,
            record_hash=record_hash,
        )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event), sort_keys=True) + "\n")
        self._last_hash = record_hash
        return event

    def verify_chain(self) -> bool:
        if not self.path.exists():
            return True
        prev_hash = ""
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("prev_hash", "") != prev_hash:
                return False
            expected_hash = self._hash_event(
                timestamp=rec["timestamp"],
                event_type=rec["event_type"],
                task_id=rec["task_id"],
                payload=rec["payload"],
                prev_hash=rec["prev_hash"],
            )
            if rec.get("record_hash") != expected_hash:
                return False
            prev_hash = rec["record_hash"]
        return True

    def read_events(self) -> List[Dict]:
        if not self.path.exists():
            return []
        events: List[Dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def _load_last_hash(self) -> str:
        if not self.path.exists():
            return ""
        lines = [line for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return ""
        last = json.loads(lines[-1])
        return str(last.get("record_hash", ""))

    @staticmethod
    def _hash_event(
        timestamp: float,
        event_type: str,
        task_id: str,
        payload: Dict,
        prev_hash: str,
    ) -> str:
        canonical = json.dumps(
            {
                "timestamp": timestamp,
                "event_type": event_type,
                "task_id": task_id,
                "payload": payload,
                "prev_hash": prev_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProvenanceBundler:
    """
    Writes per-task provenance bundles with content digest.
    """

    def __init__(self, bundle_dir: str | Path) -> None:
        self.bundle_dir = Path(bundle_dir)
        self.bundle_dir.mkdir(parents=True, exist_ok=True)

    def write_bundle(
        self,
        task_id: str,
        task_payload: Dict,
        outcome: str,
        metadata: Optional[Dict] = None,
    ) -> Path:
        material = {
            "task_id": task_id,
            "outcome": outcome,
            "task": task_payload,
            "metadata": metadata or {},
        }
        canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        bundle = {
            "task_id": task_id,
            "outcome": outcome,
            "digest": digest,
            "material": material,
        }
        path = self.bundle_dir / f"{task_id}.json"
        path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
        return path
