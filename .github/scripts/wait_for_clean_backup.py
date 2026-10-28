"""Pause a one-time owner-authorized purge until backup refs are cleaned.

The agent operating the deployment can replace the private backup's old refs
without racing a running bot. No state contents or credentials are printed.
This script never modifies a repository. On timeout it leaves the verified
sanitized backup and all old refs in place and allows the service to start.
"""
from __future__ import annotations

import os
import sys
import time

import requests


def main() -> int:
    repo = os.environ.get("STATE_BACKUP_REPO", "").strip().strip("/")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if repo.count("/") != 1 or not token:
        print("Backup history cleanup could not be monitored; old refs were not declared removed.")
        return 0
    base = "https://api.github.com/repos/" + repo
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}

    def get(path: str) -> dict | list:
        response = requests.get(base + path, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()

    try:
        head = get("/git/ref/heads/main")["object"]["sha"]
        expected_tree = get("/git/commits/" + head)["tree"]["sha"]
    except (requests.RequestException, KeyError, TypeError, ValueError):
        print("Backup history cleanup could not be monitored; old refs were not declared removed.")
        return 0
    print("Verified sanitized backup ready; waiting for serialized backup-ref cleanup.", flush=True)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        try:
            current = get("/git/ref/heads/main")["object"]["sha"]
            commit = get("/git/commits/" + current)
            tags = get("/tags?per_page=1")
            branches = get("/branches?per_page=100")
            repo_info = get("")
            if (not commit.get("parents") and commit.get("tree", {}).get("sha") == expected_tree
                    and not tags and len(branches) == 1 and branches[0].get("name") == "main"
                    and repo_info.get("default_branch") == "main"):
                print("Verified sanitized backup is now the root of main; no backup tags remain.", flush=True)
                return 0
        except (requests.RequestException, KeyError, TypeError, ValueError):
            pass
        time.sleep(5)
    print("Backup cleanup was not confirmed before timeout; old refs may remain. Starting the bot with the verified sanitized backup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
