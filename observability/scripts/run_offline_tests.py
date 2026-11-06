#!/usr/bin/env python3
"""Self-contained offline test runner (Fix Round 1, M1-P1-01).

Runs the Milestone 1 + Milestone 2 unittest suites with the standard
library only — no pytest, no network, no installed packages beyond the
pinned OpenTelemetry SDK already in the repo venv.

Usage (from the repo root or anywhere):
    python scripts/run_offline_tests.py [-v]

Exit code 0 = all tests passed; 1 = failures/errors; 2 = runner error.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

OBSERVABILITY_DIR = Path(__file__).resolve().parent.parent


def _python():
    venv_python = OBSERVABILITY_DIR.parent / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


def main():
    parser = argparse.ArgumentParser(description="Run the offline test suites")
    parser.add_argument("-v", "--verbose", action="count", default=1)
    args = parser.parse_args()

    verbosity = "-" + "v" * min(args.verbose, 2)
    cmd = [
        _python(),
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-t",
        ".",
        verbosity,
    ]
    print(f"running: {' '.join(cmd)} (cwd={OBSERVABILITY_DIR})", flush=True)
    try:
        proc = subprocess.run(cmd, cwd=str(OBSERVABILITY_DIR))
    except OSError as exc:
        print(f"runner error: {exc}", file=sys.stderr)
        return 2
    if proc.returncode == 0:
        print("offline tests: PASS", flush=True)
    else:
        print(f"offline tests: FAIL (exit {proc.returncode})", flush=True)
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
