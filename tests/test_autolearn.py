from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autolearn.core import AutoLearn, initialize


class AutoLearnTests(unittest.TestCase):
    def project(self, score: int = 10) -> Path:
        root = Path(tempfile.mkdtemp())
        initialize(root, "lower the measured score")
        (root / "src").mkdir()
        (root / "src/value.txt").write_text(str(score), encoding="utf-8")
        (root / "benchmark.py").write_text(
            "import json\nfrom pathlib import Path\nprint(json.dumps({'score': int(Path('src/value.txt').read_text())}))\n",
            encoding="utf-8",
        )
        (root / "gate.py").write_text(
            "from pathlib import Path\nraise SystemExit(0 if int(Path('src/value.txt').read_text()) >= 0 else 1)\n",
            encoding="utf-8",
        )
        config = json.loads((root / "autolearn.json").read_text())
        config.update({
            "metric_command": ["python3", "benchmark.py"],
            "gates": [["python3", "gate.py"]],
            "writable_paths": ["src/**"],
            "preserve_paths": ["protected.txt"],
            "timeout_seconds": 5,
        })
        (root / "autolearn.json").write_text(json.dumps(config), encoding="utf-8")
        (root / ".ai_programs/autolearn/proposals/001-example.json").unlink()
        return root

    def proposal(self, root: Path, candidate_id: str, script: str, files: list[str] | None = None) -> None:
        tool = root / f"apply_{candidate_id}.py"
        tool.write_text(script, encoding="utf-8")
        data = {
            "id": candidate_id, "hypothesis": "a smaller value scores better",
            "expected_improvement": "the score decreases", "files": files or ["src/value.txt"],
            "risk": "the gate could regress", "apply_command": ["python3", tool.name],
            "repair_command": None, "keep_condition": "score improves and gate passes",
            "revert_condition": "score or gate regresses",
        }
        (root / ".ai_programs/autolearn/proposals" / f"{candidate_id}.json").write_text(json.dumps(data), encoding="utf-8")

    def test_init_refuses_overwrite(self):
        root = Path(tempfile.mkdtemp())
        initialize(root, "goal")
        with self.assertRaises(Exception):
            initialize(root, "other")

    def test_plan_establishes_baseline_without_applying(self):
        root = self.project()
        self.proposal(root, "better", "from pathlib import Path\nPath('src/value.txt').write_text('5')\n")
        result = AutoLearn(root).plan_only()
        self.assertEqual(result["best"], 10)
        self.assertEqual((root / "src/value.txt").read_text(), "10")

    def test_improvement_is_kept_and_logged(self):
        root = self.project()
        self.proposal(root, "better", "from pathlib import Path\nPath('src/value.txt').write_text('5')\n")
        result = AutoLearn(root).do_it()
        self.assertEqual(result["status"], "kept")
        self.assertEqual((root / "src/value.txt").read_text(), "5")
        self.assertIn("better", (root / ".ai_programs/autolearn/results.tsv").read_text())

    def test_regression_is_byte_exactly_reverted(self):
        root = self.project()
        path = root / "src/value.txt"
        path.chmod(0o640)
        self.proposal(root, "worse", "from pathlib import Path\np=Path('src/value.txt'); p.write_text('20'); p.chmod(0o600)\n")
        result = AutoLearn(root).do_it()
        self.assertEqual(result["status"], "reverted")
        self.assertEqual(path.read_bytes(), b"10")
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_gate_failure_reverts_even_with_better_metric(self):
        root = self.project()
        self.proposal(root, "invalid", "from pathlib import Path\nPath('src/value.txt').write_text('-1')\n")
        result = AutoLearn(root).do_it()
        self.assertEqual(result["status"], "reverted")
        self.assertEqual((root / "src/value.txt").read_text(), "10")

    def test_unauthorized_change_is_reverted(self):
        root = self.project()
        (root / "protected.txt").write_text("safe", encoding="utf-8")
        self.proposal(root, "escape", "from pathlib import Path\nPath('protected.txt').write_text('changed')\n", ["protected.txt"])
        result = AutoLearn(root).do_it()
        self.assertEqual(result["status"], "reverted")
        self.assertIn("scope violation", result["notes"])
        self.assertEqual((root / "protected.txt").read_text(), "safe")

    def test_audit_does_not_run_candidate(self):
        root = self.project()
        self.proposal(root, "better", "from pathlib import Path\nPath('src/value.txt').write_text('5')\n")
        result = AutoLearn(root).audit_only()
        self.assertEqual(result["next_candidate"], "better")
        self.assertEqual((root / "src/value.txt").read_text(), "10")

    def test_gate_side_effect_outside_scope_is_reverted(self):
        root = self.project()
        protected = root / "protected.txt"
        protected.write_text("safe", encoding="utf-8")
        (root / "gate.py").write_text(
            "from pathlib import Path\n"
            "score = int(Path('src/value.txt').read_text())\n"
            "if score < 10: Path('protected.txt').write_text('gate side effect')\n",
            encoding="utf-8",
        )
        self.proposal(root, "better", "from pathlib import Path\nPath('src/value.txt').write_text('5')\n")
        result = AutoLearn(root).do_it()
        self.assertEqual(result["status"], "reverted")
        self.assertIn("scope violation", result["notes"])
        self.assertEqual(protected.read_text(), "safe")
        self.assertEqual((root / "src/value.txt").read_text(), "10")


if __name__ == "__main__":
    unittest.main()
