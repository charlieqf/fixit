from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def run_cycle(repo_path: Path) -> None:
    os.environ["LOCAL_REPO_PATH"] = str(repo_path)
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from app.main import app  # imported after env override

    client = TestClient(app)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKEPNG").decode()
    description_html = (
        "<p>Checkout issue after voucher apply.</p>"
        f"<p><img src=\"data:image/png;base64,{png}\" alt=\"checkout\"></p>"
    )

    create_resp = client.post(
        "/api/issues",
        data={
            "reported_by": "QA Demo",
            "title": "Mock cycle: checkout disabled",
            "issue_type": "bug",
            "description_html": description_html,
            "problem_summary": "Voucher apply leaves checkout button disabled.",
            "repro_steps": "Open cart\nApply voucher\nClick checkout",
            "expected_result": "Checkout button enabled.",
            "actual_result": "Checkout button disabled.",
            "acceptance_criteria": "Button enables\nE2E checkout passes",
            "environment": "staging chrome",
            "impact": "blocks guest checkout",
            "status": "ready_for_fix",
            "notes": "Mock submit step",
        },
        files=[("files", ("console.log", b"mock log", "text/plain"))],
    )
    create_resp.raise_for_status()
    payload = create_resp.json()
    issue_id = payload["issue_id"]

    for status in ("in_progress", "fixed_pending_verify", "verified_closed"):
        update_resp = client.post(
            f"/api/issues/{issue_id}/updates",
            data={
                "updated_by": "QA Demo",
                "status": status,
                "note": f"mock update -> {status}",
            },
        )
        update_resp.raise_for_status()

    issue_dir = repo_path / "quick-deal" / "Issues" / issue_id
    print(f"Issue ID: {issue_id}")
    print(f"Issue folder: {issue_dir}")
    print(f"issue_main.md: {(issue_dir / 'issue_main.md').exists()}")
    print(f"issue_main.json: {(issue_dir / 'issue_main.json').exists()}")
    print(f"ai_brief.md: {(issue_dir / 'ai_brief.md').exists()}")
    print(f"update yaml count: {len(list((issue_dir / 'updates').glob('*.yaml')))}")
    print(f"update json count: {len(list((issue_dir / 'updates').glob('*.json')))}")
    print(f"update md count: {len(list((issue_dir / 'updates').glob('*.md')))}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mock submit/edit/solve cycle.")
    parser.add_argument(
        "--repo-path",
        default="./tmp/mock_cycle_repo",
        help="Path for the local git-backed issue repo clone",
    )
    args = parser.parse_args()
    run_cycle(Path(args.repo_path).resolve())


if __name__ == "__main__":
    main()
