from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_demo_acceptance_cli_reports_retrieval_sql_and_policy(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    script = Path("scripts/mdl_knowledge_eval.py")

    completed = subprocess.run(
        [sys.executable, str(script), "--demo", "--output", str(output)],
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": ""},
    )

    report = json.loads(completed.stdout)
    saved = json.loads(output.read_text(encoding="utf-8"))

    assert report == saved
    assert report["passed"] is True
    assert report["summary"]["retrieval"] == {"passed": 8, "total": 8}
    assert report["summary"]["sql_results"] == {"passed": 3, "total": 3}
    assert report["summary"]["policy_checks"] == {"passed": 2, "total": 2}
    assert all(
        item["grounded_answer_contains_evidence"] for item in report["sql_results"]
    )
