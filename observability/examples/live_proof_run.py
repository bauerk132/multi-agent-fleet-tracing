"""Generate the M8 node-graph proof data: a live-mode Copilot document run
through the real pinned AgentScope pipeline in-process.

Output: JSON with the run, spans, findings and import report, for the
node-graph dashboard artifact.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentscope_importer import bridge  # noqa: E402
from agentscope_importer.mapper import canonical_bytes  # noqa: E402
from tests.test_agentscope_bridge import pinned_agentscope_projections  # noqa: E402
from tests.test_agentscope_bridge_live import copilot_spans, live_canonical_doc  # noqa: E402


def main():
    raw = canonical_bytes(live_canonical_doc())
    projections = pinned_agentscope_projections(raw)
    ids = bridge.predict_target_ids(raw)
    data = {
        "source": "live-mode adapter + pinned AgentScope pipeline (in-process)",
        "honest_label": "Observer-generated proof run, not a live Copilot trace.",
        "import_id": projections["report"]["import_id"],
        "quality": projections["report"]["quality"],
        "run": projections["run"],
        "run_summary": projections["run_summary"],
        "spans": projections["spans"],
        "findings": projections["findings"],
        "predicted_ids": ids,
    }
    out = Path(__file__).resolve().parent / "live_proof_run.json"
    out.write_text(json.dumps(data, indent=2, sort_keys=True))
    print(f"wrote {out}")
    print(f"run_id={projections['run']['id']} quality={projections['report']['quality']} spans={len(projections['spans'])} findings={len(projections['findings'])}")


if __name__ == "__main__":
    main()
