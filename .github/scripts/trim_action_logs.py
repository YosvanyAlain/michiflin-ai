"""Delete only old GitHub Actions log files; never delete runs or artifacts.

This operates on the current code repository using its short-lived Actions
GITHUB_TOKEN. It keeps logs less than 30 days old and at least the latest ten
completed runs per workflow. It does not inspect or print log contents.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

RETENTION_DAYS = 30
KEEP_PER_WORKFLOW = 10
MAX_DELETE_PER_RUN = 100


def cleanup(token: str, repo: str, *, dry_run: bool = False) -> tuple[int, int]:
    if not token or repo.count("/") != 1:
        raise ValueError("missing Actions token or repository")
    base = f"https://api.github.com/repos/{repo}/actions/runs"
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    runs: list[dict] = []
    # GitHub currently limits filtered run listings to 1,000 entries. Never
    # delete based on a partial listing: automatic provider retention remains.
    for page in range(1, 12):
        response = requests.get(base, headers=headers, params={"status": "completed", "per_page": 100, "page": page}, timeout=30)
        response.raise_for_status()
        batch = response.json().get("workflow_runs")
        if not isinstance(batch, list):
            raise ValueError("invalid workflow listing")
        runs.extend(x for x in batch if isinstance(x, dict))
        if len(runs) >= 1000 or response.json().get("total_count", 0) > 1000:
            print("[maintenance] too many runs for a complete listing; no logs deleted")
            return 0, 0
        if len(batch) < 100:
            break
    else:
        print("[maintenance] too many runs for a complete listing; no logs deleted")
        return 0, 0
    runs.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    seen: defaultdict[int, int] = defaultdict(int)
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    eligible: list[int] = []
    for item in runs:
        workflow, run_id = item.get("workflow_id"), item.get("id")
        if not isinstance(workflow, int) or not isinstance(run_id, int) or item.get("status") != "completed":
            continue
        seen[workflow] += 1
        try:
            created = datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        if seen[workflow] > KEEP_PER_WORKFLOW and created < cutoff:
            eligible.append(run_id)
    deleted = 0
    for run_id in eligible[:MAX_DELETE_PER_RUN]:
        if dry_run:
            continue
        response = requests.delete(f"{base}/{run_id}/logs", headers=headers, timeout=30)
        if response.status_code == 204:
            deleted += 1
        elif response.status_code != 404:
            response.raise_for_status()
    print(f"[maintenance] {len(eligible)} old log sets eligible; {deleted} removed. Runs and artifacts kept.")
    return len(eligible), deleted


def main() -> int:
    try:
        cleanup(os.getenv("GH_TOKEN", ""), os.getenv("GITHUB_REPOSITORY", ""), dry_run="--dry-run" in sys.argv[1:])
    except (requests.RequestException, ValueError) as exc:
        print(f"[maintenance] deferred ({exc.__class__.__name__}); provider retention still applies", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
