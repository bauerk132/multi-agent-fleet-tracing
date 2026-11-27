"""Allow `python -m claude_code_plugin ...` (usage in cli.py)."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
