#!/usr/bin/env python3
"""Publish multi-agent-fleet-tracing to GitHub.

Usage (from the repo root, after `gh auth login`):
    python3 publish.py [--repo NAME] [--visibility public|private] [--message MSG]

What it does:
  1. Checks git and gh exist and that gh is logged in.
  2. Inits git (main branch) if needed, ensures .gitignore covers .venv/ and caches.
  3. Refuses to stage anything that looks like a secret (.env, *token*, *secret*, *.pem).
  4. Commits all changes (skips the commit when there is nothing new).
  5. Creates the GitHub repo with `gh` if it does not exist yet, then pushes.

Nothing here stores credentials. Auth is whatever `gh` already has.
"""

import argparse
import subprocess
import sys
from pathlib import Path

SECRET_MARKERS = (".env", "token", "secret", ".pem", ".key", "credentials")


def run(cmd, check=True):
    print("+", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.stdout:
        print(p.stdout, end="")
    if p.stderr:
        print(p.stderr, end="", file=sys.stderr)
    if check and p.returncode != 0:
        sys.exit(f"FAILED ({p.returncode}): {' '.join(cmd)}")
    return p


def main():
    ap = argparse.ArgumentParser(description="Publish this project to GitHub.")
    ap.add_argument("--repo", default="multi-agent-fleet-tracing",
                    help="GitHub repo name (default: multi-agent-fleet-tracing)")
    ap.add_argument("--visibility", choices=["public", "private"], default="private",
                    help="Repo visibility (default: private)")
    ap.add_argument("--message", default="Milestone 1: local receiver + coordinator + 12-span fixture",
                    help="Commit message")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    if not (root / "observability").is_dir():
        sys.exit("Run this from the repo root (observability/ not found).")

    for tool in ("git", "gh"):
        p = subprocess.run(["which", tool], capture_output=True)
        if p.returncode != 0:
            sys.exit(f"{tool} is not installed.")

    auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if auth.returncode != 0:
        sys.exit("gh is not logged in. Run `gh auth login` first, then re-run this script.")

    if not (root / ".git").exists():
        run(["git", "init", "-b", "main"], check=True)
        run(["git", "config", "user.name", "Kyle Bauer"])
        run(["git", "config", "user.email", "bauerk132@gmail.com"])

    gitignore = root / ".gitignore"
    wanted = {".venv/", "__pycache__/", "*.pyc", ".DS_Store", "observability/out/"}
    existing = set(gitignore.read_text().splitlines()) if gitignore.exists() else set()
    if not wanted <= existing:
        gitignore.write_text("\n".join(sorted(existing | wanted)) + "\n")
        print("updated .gitignore")

    # Secret scan on what would be staged.
    staged = run(["git", "add", "--dry-run", "-A"], check=True)
    for line in staged.stdout.splitlines():
        low = line.lower()
        if any(m in low for m in SECRET_MARKERS):
            sys.exit(f"Refusing to stage a possible secret: {line.strip()}")

    run(["git", "add", "-A"])
    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    if status.stdout.strip():
        run(["git", "commit", "-m", args.message])
    else:
        print("Nothing new to commit.")

    view = subprocess.run(["gh", "repo", "view", args.repo], capture_output=True)
    if view.returncode != 0:
        run(["gh", "repo", "create", args.repo, f"--{args.visibility}",
             "--source", ".", "--remote", "origin"])
    else:
        print(f"Repo {args.repo} already exists on GitHub; pushing to it.")
        remote = subprocess.run(["git", "remote"], capture_output=True, text=True)
        if "origin" not in remote.stdout.split():
            url = subprocess.run(["gh", "repo", "view", args.repo, "--json", "url",
                                  "--jq", ".url"], capture_output=True, text=True)
            if url.returncode != 0 or not url.stdout.strip():
                sys.exit("Could not read the repo URL from gh. Add the remote by hand:\n"
                         "  git remote add origin https://github.com/<owner>/" + args.repo + ".git")
            run(["git", "remote", "add", "origin", url.stdout.strip()])

    # Push (set upstream on first push).
    push = subprocess.run(["git", "push", "-u", "origin", "main"],
                          capture_output=True, text=True)
    print(push.stdout, end="")
    print(push.stderr, end="", file=sys.stderr)
    if push.returncode != 0:
        sys.exit("Push failed. Check the remote URL with `git remote -v` — it must be\n"
                 "  https://github.com/<your-username>/" + args.repo + ".git")
    url = subprocess.run(["gh", "repo", "view", args.repo, "--json", "url",
                          "--jq", ".url"], capture_output=True, text=True)
    print(f"\nDone — {url.stdout.strip() or '(repo: ' + args.repo + ')'}")


if __name__ == "__main__":
    main()
