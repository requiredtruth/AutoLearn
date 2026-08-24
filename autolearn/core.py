"""Transactional experiment runner used by the AutoLearn CLI.

The engine intentionally does not generate edits. An agent, script, or person places
bounded hypotheses in the proposal inbox; this module supplies the durable state,
measurement, gates, audit trail, and byte-exact rollback layer.
"""

from __future__ import annotations

import csv
import difflib
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_REL = Path(".ai_programs/autolearn")
CONFIG_NAME = "autolearn.json"
RESULT_FIELDS = (
    "run_id", "candidate_id", "command", "exit_code", "before_metric",
    "after_metric", "tests", "duration_seconds", "kept", "notes",
)


class AutoLearnError(RuntimeError):
    """A safe, user-actionable AutoLearn failure."""


@dataclass(frozen=True)
class Entry:
    kind: str
    mode: int
    data: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.kind.encode() + b"\0" + self.data).hexdigest()


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def initialize(root: Path, goal: str) -> dict[str, Any]:
    """Create a conservative example configuration and state directory."""
    if not goal.strip():
        raise AutoLearnError("goal must not be empty")
    root.mkdir(parents=True, exist_ok=True)
    config = root / CONFIG_NAME
    if config.exists():
        raise AutoLearnError(f"refusing to overwrite {config}")
    _write_json(config, {
        "schema": 1,
        "goal": goal.strip(),
        "direction": "minimize",
        "metric_command": ["python3", "benchmark.py"],
        "metric_key": "score",
        "epsilon": 0.0,
        "target": None,
        "gates": [["python3", "-m", "unittest", "discover", "-s", "tests"]],
        "writable_paths": ["src/**"],
        "preserve_paths": [".env", "**/*.key", "data/**"],
        "timeout_seconds": 300,
        "max_snapshot_bytes": 268435456,
        "poll_seconds": 10,
    })
    state_dir = root / STATE_REL
    (state_dir / "proposals").mkdir(parents=True, exist_ok=True)
    _write_json(state_dir / "state.json", {"schema": 1, "goal": goal.strip(), "baseline": None, "best": None, "seen": []})
    (state_dir / "results.tsv").write_text("\t".join(RESULT_FIELDS) + "\n", encoding="utf-8")
    (state_dir / "current.md").write_text("# Current experiment\n\nNo experiment is active.\n", encoding="utf-8")
    (state_dir / "project_map.md").write_text("# Project map\n\nRun `python3 -m autolearn audit_only` to refresh.\n", encoding="utf-8")
    proposal = state_dir / "proposals/001-example.json"
    _write_json(proposal, {
        "id": "example-001",
        "hypothesis": "Describe one narrow, measurable change.",
        "expected_improvement": "Explain why the primary metric should improve.",
        "files": ["src/example.py"],
        "risk": "Describe the likely regression surface.",
        "apply_command": ["python3", "tools/apply_candidate.py"],
        "repair_command": None,
        "keep_condition": "primary metric improves and every gate passes",
        "revert_condition": "metric regresses, a gate fails, or scope is violated",
    })
    return {"initialized": str(root), "goal": goal.strip(), "example_proposal": str(proposal)}


class AutoLearn:
    """Run evidence-gated experiments in a local project."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.config_path = self.root / CONFIG_NAME
        if not self.config_path.is_file():
            raise AutoLearnError(f"missing {CONFIG_NAME}; run init first")
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self._validate_config()
        self.state_dir = self.root / STATE_REL
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "proposals").mkdir(exist_ok=True)
        self.state_path = self.state_dir / "state.json"
        if not self.state_path.exists():
            _write_json(self.state_path, {"schema": 1, "goal": self.config["goal"], "baseline": None, "best": None, "seen": []})
        self.results_path = self.state_dir / "results.tsv"
        if not self.results_path.exists():
            self.results_path.write_text("\t".join(RESULT_FIELDS) + "\n", encoding="utf-8")

    def _validate_config(self) -> None:
        c = self.config
        if c.get("schema") != 1 or not str(c.get("goal", "")).strip():
            raise AutoLearnError("config requires schema 1 and a non-empty goal")
        if c.get("direction") not in {"minimize", "maximize"}:
            raise AutoLearnError("direction must be minimize or maximize")
        self._command(c.get("metric_command"), "metric_command")
        for gate in c.get("gates", []):
            self._command(gate, "gate")
        if not c.get("writable_paths"):
            raise AutoLearnError("writable_paths must explicitly bound candidate edits")
        if float(c.get("epsilon", 0)) < 0:
            raise AutoLearnError("epsilon cannot be negative")
        if not 1 <= int(c.get("timeout_seconds", 300)) <= 86400:
            raise AutoLearnError("timeout_seconds must be between 1 and 86400")
        if int(c.get("max_snapshot_bytes", 268435456)) < 1:
            raise AutoLearnError("max_snapshot_bytes must be positive")

    @staticmethod
    def _command(value: Any, label: str) -> list[str]:
        if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
            raise AutoLearnError(f"{label} must be a non-empty JSON string array (no shell)")
        return value

    def _state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _run(self, command: list[str]) -> dict[str, Any]:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command, cwd=self.root, capture_output=True, text=True,
                timeout=int(self.config.get("timeout_seconds", 300)), check=False,
                env={**os.environ, "AUTOLEARN": "1"},
            )
            return {
                "command": command, "exit_code": proc.returncode,
                "stdout": proc.stdout[-1048576:], "stderr": proc.stderr[-1048576:],
                "duration": time.monotonic() - started, "timeout": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "command": command, "exit_code": 124,
                "stdout": (exc.stdout or "")[-1048576:] if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "")[-1048576:] if isinstance(exc.stderr, str) else "",
                "duration": time.monotonic() - started, "timeout": True,
            }

    def _measure(self) -> tuple[float, dict[str, Any]]:
        run = self._run(self._command(self.config["metric_command"], "metric_command"))
        if run["exit_code"] != 0:
            raise AutoLearnError(f"metric command failed ({run['exit_code']}): {run['stderr'].strip()}")
        lines = [line for line in run["stdout"].splitlines() if line.strip()]
        if not lines:
            raise AutoLearnError("metric command produced no JSON line")
        try:
            payload = json.loads(lines[-1])
            score = float(payload[self.config.get("metric_key", "score")])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise AutoLearnError("last metric output line must be JSON containing a finite numeric metric") from exc
        if score != score or score in (float("inf"), float("-inf")):
            raise AutoLearnError("metric must be finite")
        return score, {"payload": payload, "run": run}

    def _gates(self) -> tuple[bool, list[dict[str, Any]]]:
        results = [self._run(self._command(cmd, "gate")) for cmd in self.config.get("gates", [])]
        return all(item["exit_code"] == 0 for item in results), results

    def _excluded(self, relative: str) -> bool:
        return relative == ".git" or relative.startswith(".git/") or relative == str(STATE_REL) or relative.startswith(str(STATE_REL) + "/")

    def _snapshot(self) -> dict[str, Entry]:
        snapshot: dict[str, Entry] = {}
        total = 0
        for base, dirs, files in os.walk(self.root, followlinks=False):
            base_path = Path(base)
            dirs[:] = [d for d in dirs if not self._excluded((base_path / d).relative_to(self.root).as_posix())]
            for name in files:
                path = base_path / name
                relative = path.relative_to(self.root).as_posix()
                if self._excluded(relative):
                    continue
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    data, kind = os.readlink(path).encode("utf-8", "surrogateescape"), "symlink"
                elif stat.S_ISREG(info.st_mode):
                    data, kind = path.read_bytes(), "file"
                else:
                    raise AutoLearnError(f"unsupported project entry: {relative}")
                total += len(data)
                if total > int(self.config.get("max_snapshot_bytes", 268435456)):
                    raise AutoLearnError("snapshot exceeds max_snapshot_bytes; narrow the project or raise the explicit budget")
                snapshot[relative] = Entry(kind, stat.S_IMODE(info.st_mode), data)
        return snapshot

    def _changes(self, before: dict[str, Entry], after: dict[str, Entry]) -> list[str]:
        return sorted(path for path in set(before) | set(after) if path not in before or path not in after or before[path].digest != after[path].digest or before[path].mode != after[path].mode)

    @staticmethod
    def _matches(path: str, patterns: list[str]) -> bool:
        for pattern in patterns:
            clean = pattern.rstrip("/")
            if fnmatch.fnmatchcase(path, pattern) or path == clean or path.startswith(clean + "/"):
                return True
        return False

    def _restore(self, before: dict[str, Entry], after: dict[str, Entry]) -> None:
        for relative in sorted(set(after) - set(before), reverse=True):
            path = self.root / relative
            if path.is_symlink() or path.is_file():
                path.unlink()
        for relative, entry in before.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() or path.is_symlink():
                path.unlink()
            if entry.kind == "symlink":
                os.symlink(entry.data.decode("utf-8", "surrogateescape"), path)
            else:
                path.write_bytes(entry.data)
                path.chmod(entry.mode)
        for base, dirs, _ in os.walk(self.root, topdown=False):
            for name in dirs:
                path = Path(base) / name
                relative = path.relative_to(self.root).as_posix()
                if not self._excluded(relative):
                    try:
                        path.rmdir()
                    except OSError:
                        pass

    def _write_patch(self, run_id: str, before: dict[str, Entry], after: dict[str, Entry], changes: list[str]) -> Path:
        output: list[str] = []
        for relative in changes:
            old, new = before.get(relative), after.get(relative)
            if old and new and old.kind == new.kind == "file":
                try:
                    a = old.data.decode("utf-8").splitlines(keepends=True)
                    b = new.data.decode("utf-8").splitlines(keepends=True)
                except UnicodeDecodeError:
                    output.append(f"Binary change: {relative}\n")
                else:
                    output.extend(difflib.unified_diff(a, b, f"a/{relative}", f"b/{relative}"))
            else:
                output.append(f"{('Added' if old is None else 'Deleted' if new is None else 'Changed')}: {relative}\n")
        path = self.state_dir / "runs" / run_id / "patch.diff"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(output), encoding="utf-8")
        return path

    def _next_candidate(self) -> tuple[Path, dict[str, Any]] | None:
        seen = set(self._state().get("seen", []))
        for path in sorted((self.state_dir / "proposals").glob("*.json")):
            candidate = json.loads(path.read_text(encoding="utf-8"))
            candidate_id = str(candidate.get("id", "")).strip()
            if not candidate_id or candidate_id in seen:
                continue
            for key in ("hypothesis", "expected_improvement", "files", "risk", "apply_command", "keep_condition", "revert_condition"):
                if not candidate.get(key):
                    raise AutoLearnError(f"{path.name} is missing {key}")
            self._command(candidate["apply_command"], "apply_command")
            if candidate.get("repair_command") is not None:
                self._command(candidate["repair_command"], "repair_command")
            if not isinstance(candidate["files"], list) or not all(isinstance(x, str) for x in candidate["files"]):
                raise AutoLearnError(f"{path.name} files must be a string array")
            return path, candidate
        return None

    def _better(self, before: float, after: float) -> bool:
        epsilon = float(self.config.get("epsilon", 0))
        if self.config["direction"] == "minimize":
            return after < before - epsilon
        return after > before + epsilon

    def _target_met(self, score: float) -> bool:
        target = self.config.get("target")
        if target is None:
            return False
        return score <= float(target) if self.config["direction"] == "minimize" else score >= float(target)

    def _baseline(self) -> tuple[dict[str, Any], float]:
        state = self._state()
        if state.get("best") is not None:
            return state, float(state["best"])
        snapshot = self._snapshot()
        try:
            score, evidence = self._measure()
            gates_ok, gates = self._gates()
        except Exception:
            self._restore(snapshot, self._snapshot())
            raise
        after = self._snapshot()
        baseline_changes = self._changes(snapshot, after)
        if baseline_changes:
            self._restore(snapshot, after)
            raise AutoLearnError("baseline metric or gate command modified the project: " + ", ".join(baseline_changes))
        if not gates_ok:
            raise AutoLearnError("baseline gates fail; AutoLearn will not edit a broken baseline")
        state.update({"baseline": score, "best": score, "baseline_evidence": evidence["payload"], "baseline_gates": len(gates)})
        _write_json(self.state_path, state)
        return state, score

    def audit_only(self) -> dict[str, Any]:
        before = self._snapshot()
        state = self._state()
        candidate = self._next_candidate()
        lines = ["# Project map", "", f"Goal: {self.config['goal']}", f"Tracked entries: {len(before)}", f"Snapshot bytes: {sum(len(x.data) for x in before.values())}", f"Best metric: {state.get('best')}", f"Next candidate: {candidate[1]['id'] if candidate else 'none'}", "", "No files were edited by audit mode.", ""]
        (self.state_dir / "project_map.md").write_text("\n".join(lines), encoding="utf-8")
        return {"mode": "audit_only", "entries": len(before), "best": state.get("best"), "next_candidate": candidate[1]["id"] if candidate else None}

    def plan_only(self) -> dict[str, Any]:
        state, best = self._baseline()
        candidate = self._next_candidate()
        return {"mode": "plan_only", "goal": self.config["goal"], "best": best, "target_met": self._target_met(best), "candidate": candidate[1] if candidate else None, "seen": len(state.get("seen", []))}

    def do_it(self) -> dict[str, Any]:
        state, before_score = self._baseline()
        if self._target_met(before_score):
            return {"mode": "do_it", "status": "target_met", "best": before_score}
        pending = self._next_candidate()
        if pending is None:
            return {"mode": "do_it", "status": "idle", "best": before_score}
        _, candidate = pending
        run_id = f"{_utc()}-{candidate['id']}"
        run_dir = self.state_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "before.diff").write_text("", encoding="utf-8")
        (run_dir / "status_before.txt").write_text(json.dumps({"best": before_score, "candidate": candidate["id"]}, sort_keys=True) + "\n", encoding="utf-8")
        (self.state_dir / "current.md").write_text(
            f"# Current experiment\n\n- Run: `{run_id}`\n- Goal: {self.config['goal']}\n- Hypothesis: {candidate['hypothesis']}\n- Expected: {candidate['expected_improvement']}\n- Files: {', '.join(candidate['files'])}\n- Risk: {candidate['risk']}\n- Keep: {candidate['keep_condition']}\n- Revert: {candidate['revert_condition']}\n",
            encoding="utf-8",
        )
        snapshot = self._snapshot()
        started = time.monotonic()
        apply_result = self._run(candidate["apply_command"])
        after_apply = self._snapshot()
        changes = self._changes(snapshot, after_apply)
        self._write_patch(run_id, snapshot, after_apply, changes)
        unauthorized = [p for p in changes if not self._matches(p, self.config["writable_paths"]) or self._matches(p, self.config.get("preserve_paths", []))]
        after_score: float | None = None
        gates_ok = False
        notes = ""
        metric_exit = 0
        if apply_result["exit_code"] != 0:
            notes = "apply command failed"
            metric_exit = apply_result["exit_code"]
        elif unauthorized:
            notes = "scope violation: " + ", ".join(unauthorized)
            metric_exit = 3
        else:
            try:
                after_score, _ = self._measure()
                gates_ok, _ = self._gates()
                if not gates_ok:
                    notes = "one or more gates failed"
                    metric_exit = 4
            except AutoLearnError as exc:
                repair = candidate.get("repair_command")
                if repair:
                    repair_result = self._run(repair)
                    try:
                        repaired = self._snapshot()
                        repaired_changes = self._changes(snapshot, repaired)
                        repaired_bad = [p for p in repaired_changes if not self._matches(p, self.config["writable_paths"]) or self._matches(p, self.config.get("preserve_paths", []))]
                        if repair_result["exit_code"] or repaired_bad:
                            raise AutoLearnError("repair failed or violated scope")
                        after_score, _ = self._measure()
                        gates_ok, _ = self._gates()
                        after_apply, changes = repaired, repaired_changes
                        self._write_patch(run_id, snapshot, repaired, changes)
                        notes = "repaired once" if gates_ok else "repair completed but gates failed"
                    except AutoLearnError as repaired_exc:
                        notes = f"evaluation crashed; repair failed: {repaired_exc}"
                        metric_exit = 5
                else:
                    notes = f"evaluation crashed: {exc}"
                    metric_exit = 5
        final_snapshot = self._snapshot()
        final_changes = self._changes(snapshot, final_snapshot)
        final_unauthorized = [p for p in final_changes if not self._matches(p, self.config["writable_paths"]) or self._matches(p, self.config.get("preserve_paths", []))]
        if final_unauthorized:
            unauthorized = sorted(set(unauthorized) | set(final_unauthorized))
            notes = "scope violation: " + ", ".join(unauthorized)
            metric_exit = 3
            gates_ok = False
        after_apply, changes = final_snapshot, final_changes
        self._write_patch(run_id, snapshot, final_snapshot, final_changes)
        kept = bool(after_score is not None and gates_ok and (self._better(before_score, after_score) or self._target_met(after_score)))
        if not kept:
            self._restore(snapshot, self._snapshot())
            if not notes:
                notes = "primary metric did not improve beyond epsilon"
        else:
            state["best"] = after_score
        state.setdefault("seen", []).append(candidate["id"])
        state["last_run"] = run_id
        _write_json(self.state_path, state)
        row = {
            "run_id": run_id, "candidate_id": candidate["id"],
            "command": json.dumps(candidate["apply_command"], separators=(",", ":")),
            "exit_code": metric_exit, "before_metric": before_score,
            "after_metric": "" if after_score is None else after_score,
            "tests": "pass" if gates_ok else "fail", "duration_seconds": f"{time.monotonic() - started:.6f}",
            "kept": str(kept).lower(), "notes": notes,
        }
        with self.results_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, delimiter="\t", lineterminator="\n")
            writer.writerow(row)
        (self.state_dir / "current.md").write_text(f"# Current experiment\n\nLast run `{run_id}` was **{'kept' if kept else 'reverted'}**.\n", encoding="utf-8")
        return {"mode": "do_it", "status": "kept" if kept else "reverted", "run_id": run_id, "before": before_score, "after": after_score, "changes": changes, "notes": notes}

    def run_forever(self) -> dict[str, Any]:
        completed = 0
        try:
            while True:
                result = self.do_it()
                if result["status"] == "target_met":
                    return {"mode": "run_forever", "status": "target_met", "cycles": completed, "best": result["best"]}
                if result["status"] == "idle":
                    time.sleep(max(1, int(self.config.get("poll_seconds", 10))))
                    continue
                completed += 1
        except KeyboardInterrupt:
            return {"mode": "run_forever", "status": "stopped", "cycles": completed, "best": self._state().get("best")}

    def report(self) -> dict[str, Any]:
        state = self._state()
        rows: list[dict[str, str]] = []
        with self.results_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        return {
            "mode": "report", "goal": self.config["goal"], "baseline": state.get("baseline"),
            "best": state.get("best"), "experiments": len(rows),
            "kept": sum(row.get("kept") == "true" for row in rows),
            "reverted": sum(row.get("kept") == "false" for row in rows),
            "last_run": state.get("last_run"),
        }
