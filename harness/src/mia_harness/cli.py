"""`harness` command line (SPEC §6.1)."""
import argparse
import json
import shutil
import sys
from pathlib import Path

from .api import CROP
from .fetch import fetch
from .io import from_env
from .plan import load_plan
from .preview import preview
from .provenance import VoxelAxis
from .run import crop, smoke
from .stage import stage
from .tensorize import tensorize
from .verify import verify


def _json_arg(text: str) -> dict:
    """A JSON object given inline or as a file path."""
    text = text.strip()
    return json.loads(text if text.startswith("{") else Path(text).read_text())


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
        s.add_argument("--size", type=int, default=CROP, help="crop edge in pixels; a smaller raw is cropped whole")
        if name == "smoke":
            s.add_argument("--out", type=Path, required=True, help="result JSON")
        else:
            s.add_argument("--n", type=int, required=True)
            s.add_argument("--seed", required=True)
            s.add_argument("--round", required=True, help="round_id, i<issue>-e<event id>")
            s.add_argument("--batch", type=int, required=True)
            s.add_argument("--out", type=Path, required=True, help="crop directory; report.json goes in it")
            s.add_argument("--existing", type=Path, help="published manifest.json")
    s = sub.add_parser("stage", help="write one sparse OME-Zarr 0.6 image per raw volume from a plan rule, labels "
                                     "alongside (staging spec §4.1)")
    s.add_argument("binding", type=Path)
    s.add_argument("--snippets", type=Path, required=True, help="snippet snapshot directory")
    s.add_argument("--record", type=Path, required=True)
    s.add_argument("--work", type=Path, default=Path.cwd(), help="job workspace (default: current directory)")
    s.add_argument("--plan", type=Path, required=True, help="plan-rule.json: the rule and the artifact id inputs")
    s.add_argument("--voxel-size", type=Path, required=True,
                   help='voxel_size.json: {"<axis>": {"nm", "source", "basis"?, "range"?}}; the file wins per axis')
    s.add_argument("--out", type=Path, required=True, help="directory for v<i>.zarr and stage.json")
    s.add_argument("--seed", type=int, default=0)
    s = sub.add_parser("fetch", help="every URL of a binding into the raw cache; fetch.json has per-file outcomes")
    s.add_argument("binding", type=Path)
    s.add_argument("--record", type=Path, required=True)
    s.add_argument("--work", type=Path, default=Path.cwd(), help="job workspace (default: current directory)")
    s.add_argument("--out", type=Path, required=True, help="result JSON")
    s = sub.add_parser("tensorize", help="one crop descriptor through a tensorizer: sample.npz + .json (buffers spec §4)")
    s.add_argument("pyramid", type=Path, help="a staged v<i>.zarr directory (raw/ and labels/ inside)")
    s.add_argument("--tensorizers", type=Path, required=True, help="tensorizer snapshot directory")
    s.add_argument("--name", required=True)
    s.add_argument("--descriptor", required=True, help="JSON text or a path to a JSON file")
    s.add_argument("--params", help="JSON text or a path to a JSON file (default: {})")
    s.add_argument("--out", type=Path, required=True, help="sample .npz; meta goes to the same name .json")
    s = sub.add_parser("verify", help="SPEC §6.4 checks; loads no snippet code")
    s.add_argument("dir", type=Path)
    s.add_argument("--binding", type=Path, required=True)
    s.add_argument("--existing", type=Path, help="published manifest.json")
    s.add_argument("--size", type=int, default=CROP, help="the crop edge `crop` was given")
    s.add_argument("--out", type=Path, required=True, help="result JSON")
    s = sub.add_parser("preview", help="label overlay PNG per crop (SPEC §6.7)")
    s.add_argument("dir", type=Path)
    s.add_argument("--out", type=Path, required=True, help="directory for <crop_id>/overlay.png")
    args = p.parse_args(argv)

    if args.cmd == "verify":
        result = verify(args.dir, args.binding, args.existing, size=args.size)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
        return 0 if result["ok_count"] == result["n"] and not result["errors"] else 1
    if args.cmd == "tensorize":
        descriptor, params = _json_arg(args.descriptor), _json_arg(args.params) if args.params else {}
        try:
            sample = tensorize(args.pyramid, descriptor, args.tensorizers, args.name, params)
        except ValueError as e:
            print(f"tensorize failed: {e}", file=sys.stderr)
            return 1
        import numpy as np
        np.savez(args.out, **{k: v for k, v in sample.items() if k != "meta"})
        args.out.with_suffix(".json").write_text(json.dumps(sample.get("meta", {}), indent=2, default=str) + "\n")
        return 0
    if args.cmd == "preview":
        errors = preview(args.dir, args.out)
        print(*errors, sep="\n", file=sys.stderr)
        return 1 if errors else 0
    io = from_env(args.work)
    try:
        if args.cmd == "stage":
            voxel = {axis: VoxelAxis(**value) for axis, value in json.loads(args.voxel_size.read_text()).items()}
            return 0 if stage(args.binding, args.snippets, args.record, io, plan=load_plan(args.plan), voxel=voxel,
                              out=args.out, seed=args.seed)["ok"] else 1
        if args.cmd == "fetch":
            result: dict = {"ok": False, "partial": False, "bytes_fetched": io.bytes_fetched, "files": [], "errors": []}
            try:
                result = fetch(args.binding, args.record, io)
            except BaseException as e:      # the report lands even when something escapes `fetch`
                result["errors"].append(f"{type(e).__name__}: {e}")
                raise
            finally:
                args.out.write_text(json.dumps(result, indent=2) + "\n")
            return 0 if result["ok"] and not result["partial"] else 2 if result["partial"] else 1
        if args.cmd == "crop":
            return 0 if crop(args.binding, args.snippets, args.record, io, n=args.n, seed=args.seed,
                             round_id=args.round, batch=args.batch, out=args.out, existing=args.existing,
                             size=args.size)["made"] else 1
        result = smoke(args.binding, args.snippets, args.record, io, size=args.size)
    finally:
        io.close()
        shutil.rmtree(io.staging_dir, ignore_errors=True)   # §6.2: staging goes when the command exits
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["ok"] else 1
