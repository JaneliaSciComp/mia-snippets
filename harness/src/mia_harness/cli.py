"""`harness` command line (SPEC §6.1)."""
import argparse
import json
import sys
from pathlib import Path

from .io import from_env
from .run import smoke


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smoke", help="open every array, check against the record, read one patch")
    s.add_argument("binding", type=Path)
    s.add_argument("--snippets", type=Path, required=True, help="snippet snapshot directory")
    s.add_argument("--record", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True, help="result JSON")
    s.add_argument("--work", type=Path, default=Path.cwd(), help="job workspace (default: current directory)")
    for name in ("crop", "verify", "preview"):
        sub.add_parser(name, help="not implemented yet")
    args, extra = p.parse_known_args(argv)
    if args.cmd != "smoke":
        print(f"harness {args.cmd}: not implemented", file=sys.stderr)
        return 2
    if extra:
        p.error(f"unrecognized arguments: {' '.join(extra)}")

    io = from_env(args.work)
    try:
        result = smoke(args.binding, args.snippets, args.record, io)
    finally:
        io.close()
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["ok"] else 1
