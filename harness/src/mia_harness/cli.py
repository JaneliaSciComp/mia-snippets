"""`harness` command line (SPEC §6.1)."""
import argparse
import json
import sys
from pathlib import Path

from .io import from_env
from .preview import preview
from .run import crop, smoke
from .verify import verify


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    jobs = {"smoke": "open every array, check against the record, read one patch",
            "crop": "sample and write crops (SPEC §6.2, §6.3)"}
    for name, help in jobs.items():
        s = sub.add_parser(name, help=help)
        s.add_argument("binding", type=Path)
        s.add_argument("--snippets", type=Path, required=True, help="snippet snapshot directory")
        s.add_argument("--record", type=Path, required=True)
        s.add_argument("--work", type=Path, default=Path.cwd(), help="job workspace (default: current directory)")
        if name == "smoke":
            s.add_argument("--out", type=Path, required=True, help="result JSON")
        else:
            s.add_argument("--n", type=int, required=True)
            s.add_argument("--seed", required=True)
            s.add_argument("--round", required=True, help="round_id, i<issue>-e<event id>")
            s.add_argument("--batch", type=int, required=True)
            s.add_argument("--out", type=Path, required=True, help="crop directory; report.json goes in it")
            s.add_argument("--existing", type=Path, help="published manifest.json")
    s = sub.add_parser("verify", help="SPEC §6.4 checks; loads no snippet code")
    s.add_argument("dir", type=Path)
    s.add_argument("--binding", type=Path, required=True)
    s.add_argument("--existing", type=Path, help="published manifest.json")
    s.add_argument("--out", type=Path, required=True, help="result JSON")
    s = sub.add_parser("preview", help="label overlay PNG per crop (SPEC §6.7)")
    s.add_argument("dir", type=Path)
    s.add_argument("--out", type=Path, required=True, help="directory for <crop_id>/overlay.png")
    args = p.parse_args(argv)

    if args.cmd == "verify":
        result = verify(args.dir, args.binding, args.existing)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
        return 0 if result["ok_count"] == result["n"] and not result["errors"] else 1
    if args.cmd == "preview":
        errors = preview(args.dir, args.out)
        print(*errors, sep="\n", file=sys.stderr)
        return 1 if errors else 0
    io = from_env(args.work)
    try:
        if args.cmd == "crop":
            return 0 if crop(args.binding, args.snippets, args.record, io, n=args.n, seed=args.seed,
                             round_id=args.round, batch=args.batch, out=args.out, existing=args.existing)["made"] else 1
        result = smoke(args.binding, args.snippets, args.record, io)
    finally:
        io.close()
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["ok"] else 1
