"""Verify a mapped fleet document against the REAL AgentScope importer.

Builds one synthetic Copilot turn with the fleet mapper, then runs the
resulting canonical 0.1 document through AgentScope's actual
import_bytes (via make_importer) and asserts acceptance + idempotency.

Needs:
  AGENTSCOPE_SRC  an AgentScope checkout (backend/src/agentscope/...)
  pydantic        in the running interpreter

Run with the dedicated venv (the fleet test venv has no pydantic):
  AGENTSCOPE_SRC=~/workspace/agentscope-src \
    ~/workspace/agentscope-venv/bin/python \
    observability/examples/verify_agentscope_import.py

Prints PASS, SKIP (with reason), or FAIL. Exit 0 on PASS or SKIP.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentscope_importer.mapper import map_spans  # noqa: E402
from agentscope_importer.verify import real_import_check, synthetic_turn  # noqa: E402


def main():
    agentscope_src = os.environ.get("AGENTSCOPE_SRC", "")
    mapped = map_spans(synthetic_turn("example-trace-0001"), "copilot")
    document = mapped["documents"][0][1]
    status, detail = real_import_check(document, agentscope_src)
    print("%s: %s" % (status.upper(), detail))
    return 0 if status in ("pass", "skip") else 1


if __name__ == "__main__":
    raise SystemExit(main())
