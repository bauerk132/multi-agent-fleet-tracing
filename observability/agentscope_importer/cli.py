"""CLI for the M5 AgentScope importer.

Usage (run from the repo root):
  python -m agentscope_importer import <file> [--data-dir DIR] [--producer NAME]
  python -m agentscope_importer verify [--data-dir DIR]
  python -m agentscope_importer receipt <import_id> [--data-dir DIR]
  python -m agentscope_importer purge [--data-dir DIR]
  python -m agentscope_importer stats [--data-dir DIR]

`verify` runs the full contract assertion against a fresh temporary
ledger and exits 0 only if every check passes. It never touches the
caller's default ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

# Allow `python -m agentscope_importer` from the observability directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentscope_importer import ledger as ledger_mod  # noqa: E402
from agentscope_importer.importer import (  # noqa: E402
    ImportRefused,
    ImportSettings,
    import_file,
)
from agentscope_importer.verify import run_verify  # noqa: E402


def _ledger_path(data_dir):
    if data_dir:
        return os.path.join(data_dir, "agentscope-ledger.sqlite3")
    return ledger_mod.default_ledger_path()


def _cmd_import(args):
    conn = ledger_mod.open_ledger(_ledger_path(args.data_dir))
    try:
        receipt = import_file(conn, args.file, args.producer)
    except ImportRefused as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["quality"] not in ("invalid", "unsupported") else 1


def _cmd_verify(args):
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = os.path.join(tmp, "verify-ledger.sqlite3")
        conn = ledger_mod.open_ledger(ledger_path)
        results, conn = run_verify(conn, ledger_path)
        conn.close()
    failed = [r for r in results if not r["passed"]]
    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        print("[%s] %s — %s" % (status, result["name"], result["detail"]))
    print("%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 0 if not failed else 1


def _cmd_receipt(args):
    conn = ledger_mod.open_ledger(_ledger_path(args.data_dir))
    receipt = ledger_mod.get_receipt(conn, args.import_id)
    if receipt is None:
        print("no receipt for import_id %s" % args.import_id, file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


def _cmd_purge(args):
    from agentscope_importer.importer import now_iso

    conn = ledger_mod.open_ledger(_ledger_path(args.data_dir))
    settings = ImportSettings()
    result = ledger_mod.purge(conn, settings.max_imports, settings.max_age_days, now_iso())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_stats(args):
    conn = ledger_mod.open_ledger(_ledger_path(args.data_dir))
    print(json.dumps(ledger_mod.counts(conn), indent=2, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentscope_importer")
    parser.add_argument("--data-dir", default=None, help="ledger directory (default: local state dir)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="import a file and print the receipt")
    p_import.add_argument("file")
    p_import.add_argument("--producer", default="fleet-tracing")
    p_import.set_defaults(func=_cmd_import)

    p_verify = sub.add_parser("verify", help="run the contract assertion (fresh temp ledger)")
    p_verify.set_defaults(func=_cmd_verify)

    p_receipt = sub.add_parser("receipt", help="print the receipt for an import id")
    p_receipt.add_argument("import_id")
    p_receipt.set_defaults(func=_cmd_receipt)

    p_purge = sub.add_parser("purge", help="enforce the retention policy now")
    p_purge.set_defaults(func=_cmd_purge)

    p_stats = sub.add_parser("stats", help="ledger counts")
    p_stats.set_defaults(func=_cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
