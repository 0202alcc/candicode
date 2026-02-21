import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.audit import AuditLogger, ProvenanceBundler


class AuditLoggerTests(unittest.TestCase):
    def test_hash_chain_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "audit.jsonl"
            logger = AuditLogger(log_path)
            logger.emit("task_created", "t1", {"x": 1})
            logger.emit("phase_result", "t1", {"phase": "triage", "status": "success"})
            self.assertTrue(logger.verify_chain())

    def test_hash_chain_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "audit.jsonl"
            logger = AuditLogger(log_path)
            logger.emit("task_created", "t1", {"x": 1})
            lines = log_path.read_text(encoding="utf-8").splitlines()
            rec = json.loads(lines[0])
            rec["payload"]["x"] = 2
            log_path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            self.assertFalse(logger.verify_chain())


class ProvenanceBundlerTests(unittest.TestCase):
    def test_bundle_write_includes_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundler = ProvenanceBundler(Path(tmp) / "bundles")
            path = bundler.write_bundle("task1", {"status": "done"}, "completed")
            self.assertTrue(path.exists())
            content = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("task1", content["task_id"])
            self.assertEqual("completed", content["outcome"])
            self.assertTrue(content["digest"])


if __name__ == "__main__":
    unittest.main()
