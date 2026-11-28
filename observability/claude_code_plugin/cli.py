"""Command-line entry point for the Claude Code adapter (M7-B step 3, S9, D11).

Usage (run from the observability/ directory):
  python -m claude_code_plugin convert <in.jsonl> --out <out.json>
  python -m claude_code_plugin env --endpoint http://127.0.0.1:4318/v1/traces

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
prints them. The env command only prints a validated PowerShell block; it does
not change the current environment, write files, or make network requests.

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
import re
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit

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

ENV_WARNING_NAMES = (
    "OTEL_LOG_USER_PROMPTS",
    "OTEL_LOG_TOOL_DETAILS",
    "OTEL_LOG_TOOL_CONTENT",
    "OTEL_LOG_ASSISTANT_RESPONSES",
    "OTEL_LOG_RAW_API_BODIES",
    "OTEL_EXPORTER_OTLP_HEADERS",
)


def _fixed_env_error(parser):
    parser.exit(EXIT_REFUSED, "error: invalid env arguments\n")


class _Subparser(argparse.ArgumentParser):
    fixed_errors = False

    def error(self, message):
        if self.fixed_errors:
            _fixed_env_error(self)
        super().error(message)


class _MainParser(_Subparser):
    def parse_args(self, args=None, namespace=None):
        values = list(sys.argv[1:] if args is None else args)
        self.env_mode = "env" in values
        return super().parse_args(values, namespace)

    def error(self, message):
        if getattr(self, "env_mode", False):
            _fixed_env_error(self)
        super().error(message)


def _validated_env_endpoint(value):
    if not isinstance(value, str) or any(
        char.isspace() or unicodedata.category(char) == "Cc" for char in value
    ):
        raise ValueError("invalid endpoint")
    if "?" in value or "#" in value or "\\" in value:
        raise ValueError("invalid endpoint")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ValueError("invalid endpoint") from None
    if parts.scheme.lower() != "http" or parts.path != "/v1/traces":
        raise ValueError("invalid endpoint")
    if (
        not parts.netloc
        or "@" in parts.netloc
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("invalid endpoint")

    hostname = parts.hostname
    if hostname not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("invalid endpoint")
    if hostname == "::1":
        match = re.fullmatch(r"\[::1\]:([0-9]+)", parts.netloc, flags=re.IGNORECASE)
    else:
        match = re.fullmatch(
            r"(127\.0\.0\.1|localhost):([0-9]+)",
            parts.netloc,
            flags=re.IGNORECASE,
        )
    if match is None:
        raise ValueError("invalid endpoint")
    port_text = match.group(1 if hostname == "::1" else 2)
    significant = port_text.lstrip("0") or "0"
    if len(significant) > 5 or significant == "0":
        raise ValueError("invalid endpoint")
    port = int(significant)
    if port > 65535:
        raise ValueError("invalid endpoint")
    display_host = f"[{hostname}]" if hostname == "::1" else hostname
    return f"http://{display_host}:{port}/v1/traces"


def _print_env_block(endpoint):
    lines = (
        "$env:CLAUDE_CODE_ENABLE_TELEMETRY = '1'",
        "$env:CLAUDE_CODE_ENHANCED_TELEMETRY_BETA = '1'",
        "$env:OTEL_TRACES_EXPORTER = 'otlp'",
        "$env:OTEL_METRICS_EXPORTER = 'none'",
        "$env:OTEL_LOGS_EXPORTER = 'none'",
        "$env:OTEL_EXPORTER_OTLP_TRACES_PROTOCOL = 'http/json'",
        f"$env:OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = '{endpoint}'",
    )
    print("\n".join(lines))
    for name in ENV_WARNING_NAMES:
        if name in os.environ:
            print(f"warning: {name} is set; review telemetry settings", file=sys.stderr)


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
    parser = _MainParser(prog="claude_code_plugin")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Subparser)
    p_convert = sub.add_parser(
        "convert", help="convert a receiver JSONL file to a bridge-ready span array",
    )
    p_convert.add_argument("input")
    p_convert.add_argument("--out", required=True)
    p_env = sub.add_parser(
        "env", help="print a traces-only telemetry environment block"
    )
    p_env.add_argument("--endpoint", required=True)
    p_env.fixed_errors = True
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED
    if args.command == "env":
        try:
            endpoint = _validated_env_endpoint(args.endpoint)
        except ValueError:
            print("error: invalid env arguments", file=sys.stderr)
            return EXIT_REFUSED
        _print_env_block(endpoint)
        return EXIT_OK
    try:
        return _convert(args.input, args.out)
    except _Refusal as refusal:
        print(json.dumps({"error": refusal.code}, sort_keys=True))
        return refusal.exit_code
