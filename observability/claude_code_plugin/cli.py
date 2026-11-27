"""Command-line entry point for the Claude Code adapter (M7-B step 3, S9, D11).

Usage (run from the observability/ directory):
  python -m claude_code_plugin convert <in.jsonl> --out <out.json>

convert reads a receiver JSONL file (one OTLP/JSON resourceSpans object per
line), maps it with mapping.convert_lines, and writes the canonical flat span
array (mapping.serialize). That array is a "spans" source the AgentScope
bridge accepts.

Refusals write no output file and leave any existing output file unchanged:

  input_too_large    the input is more than adapter.MAX_TOTAL_BYTES bytes (exit 2)
  input_unreadable   the input cannot be opened or read (exit 2)
  output_exists      the --out path already exists; checked before the input is
                     read (exit 2)
  output_too_large   the serialized output is more than MAX_OUTPUT_BYTES (exit 2)
  output_unwritable  the temp file cannot be written, or cannot be moved onto
                     the output path (exit 2)
  no_spans           the conversion kept zero spans (exit 1)

Every result goes to stdout as one JSON object with sorted keys. A success
prints the merged counters (all ints) plus spans_written and exits 0. A refusal
prints {"error": "<code>"}. Nothing printed ever contains the input path, the
output path, the temp directory, or any input content. Tracebacks are not
printed for these cases. argparse usage errors go to stderr as argparse always
prints them.

The write is atomic: a temp file in the output directory is written, flushed
and fsynced, then os.replace'd onto the output path. The temp file is removed
on any failure. The existence check repeats immediately before the replace.
os.replace overwrites silently on Windows, so that check narrows the race
window without fully closing it.

Stdlib only. This module imports nothing from agentscope_importer,
copilot_plugin or hermes_plugin.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

# Allow `python -m claude_code_plugin` from the observability directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_code_plugin import adapter as adapter_module  # noqa: E402
from claude_code_plugin import mapping  # noqa: E402

# Output cap: 16 MiB, copied by value. Read as a module global at call time so
# tests can patch it.
MAX_OUTPUT_BYTES = 16 * 1024 * 1024

EXIT_OK = 0
EXIT_NO_SPANS = 1
EXIT_REFUSED = 2


class _Refusal(Exception):
    """A refusal with a fixed reason code and an exit status."""

    def __init__(self, code, exit_code=EXIT_REFUSED):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code


def _read_input(in_path, limit):
    # Read at most limit + 1 bytes: one extra byte is enough to detect overflow.
    try:
        with open(in_path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        raise _Refusal("input_unreadable")
    if len(data) > limit:
        raise _Refusal("input_too_large")
    return data


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _write_atomic(out_path, raw):
    out_dir = os.path.dirname(os.path.abspath(out_path))
    try:
        fd, temp = tempfile.mkstemp(dir=out_dir, prefix=".cc-convert-", suffix=".tmp")
    except OSError:
        raise _Refusal("output_unwritable")
    moved = False
    try:
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise _Refusal("output_unwritable")
        if os.path.lexists(out_path):
            raise _Refusal("output_exists")
        try:
            os.replace(temp, out_path)
        except OSError:
            raise _Refusal("output_unwritable")
        moved = True
    finally:
        if not moved:
            _remove_quietly(temp)


def _convert(in_path, out_path):
    if os.path.lexists(out_path):
        raise _Refusal("output_exists")
    data = _read_input(in_path, adapter_module.MAX_TOTAL_BYTES)
    spans, counters = mapping.convert_lines(data.splitlines())
    if not spans:
        raise _Refusal("no_spans", EXIT_NO_SPANS)
    raw = mapping.serialize(spans)
    if len(raw) > MAX_OUTPUT_BYTES:
        raise _Refusal("output_too_large")
    _write_atomic(out_path, raw)
    result = dict(counters)
    result["spans_written"] = len(spans)
    print(json.dumps(result, sort_keys=True))
    return EXIT_OK


def main(argv=None):
    """Run the CLI and return the exit code (0 ok, 1 no spans, 2 refused)."""
    parser = argparse.ArgumentParser(prog="claude_code_plugin")
    sub = parser.add_subparsers(dest="command", required=True)
    p_convert = sub.add_parser(
        "convert", help="convert a receiver JSONL file to a bridge-ready span array",
    )
    p_convert.add_argument("input")
    p_convert.add_argument("--out", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED
    try:
        return _convert(args.input, args.out)
    except _Refusal as refusal:
        print(json.dumps({"error": refusal.code}, sort_keys=True))
        return refusal.exit_code
