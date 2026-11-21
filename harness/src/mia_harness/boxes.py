"""A Neuroglancer precomputed annotation layer of a volume's planned crops: one axis-aligned box per sample per
level, so a viewer shows where a sparse pyramid's chunks come from and why the rest of it is blank.

Written beside the volume as `v<i>-crops/` (`info`, one spatial cell holding every box, `by_id/<id>`), in the
volume's OME coordinate space in nm. Neuroglancer puts the centre of voxel i at i·v, so raw pixels [s, s+P) are the
box [(s−½)v, (s+P−½)v], and a plane is one voxel thick in z. Properties: `level` (the pyramid factor), `split` and
a `color` by level. Format: https://github.com/google/neuroglancer/blob/master/src/datasource/precomputed/annotations.md
"""
import json
import random
import struct
from pathlib import Path

SPLITS = ("train", "val", "test")
COLORS = {1: (255, 64, 64), 2: (255, 200, 0), 4: (64, 220, 64), 8: (64, 160, 255)}
GREY = (180, 180, 180)


def write_crop_boxes(out: Path, samples: dict[str, list[dict]], voxel_size_nm: dict[str, float]) -> int:
    """Write the layer at `out` from the stage plan's `samples` per split; returns the number of boxes."""
    drawn = [(split, sample) for split in SPLITS for sample in samples.get(split, [])]
    axes = "zyx" if any("z" in sample["plane"] for _, sample in drawn) else "yx"
    boxes = []   # (id, point_a, point_b, level, split)
    for split, sample in drawn:
        patch = sample["patch"]
        for factor in sample["levels"]:
            start = dict(zip("yx", sample["starts"][str(factor)]))
            lo = {"z": sample["plane"].get("z", 0) - 0.5} | {a: start[a] * factor - 0.5 for a in "yx"}
            hi = {"z": lo["z"] + 1} | {a: (start[a] + patch) * factor - 0.5 for a in "yx"}
            boxes.append((len(boxes) + 1, [lo[a] * voxel_size_nm[a] for a in axes],
                          [hi[a] * voxel_size_nm[a] for a in axes], factor, SPLITS.index(split)))
    if not boxes:
        return 0
    levels = sorted({factor for *_, factor, _ in boxes})
    lower = [min(a[i] for _, a, *_ in boxes) for i in range(len(axes))]
    upper = [max(b[i] for _, _, b, *_ in boxes) for i in range(len(axes))]

    def encode(box: tuple) -> bytes:
        _, a, b, factor, split = box
        body = struct.pack(f"<{2 * len(axes)}f", *a, *b) + struct.pack("<5B", factor, split, *COLORS.get(factor, GREY))
        return body + b"\0" * (-len(body) % 4)

    (out / "by_id").mkdir(parents=True, exist_ok=True)
    (out / "spatial0").mkdir(exist_ok=True)
    (out / "info").write_text(json.dumps({
        "@type": "neuroglancer_annotations_v1",
        "dimensions": {a: [1e-9, "m"] for a in axes},
        "lower_bound": lower, "upper_bound": upper,
        "annotation_type": "AXIS_ALIGNED_BOUNDING_BOX",
        "properties": [
            {"id": "level", "type": "uint8", "description": "pyramid factor of the crop",
             "enum_values": levels, "enum_labels": [str(f) for f in levels]},
            {"id": "split", "type": "uint8", "enum_values": list(range(len(SPLITS))), "enum_labels": list(SPLITS)},
            {"id": "color", "type": "rgb", "description": "by level"}],
        "relationships": [],
        "by_id": {"key": "by_id"},
        "spatial": [{"key": "spatial0", "grid_shape": [1] * len(axes),
                     "chunk_size": [u - l for l, u in zip(lower, upper)], "limit": len(boxes)}]}, indent=1))
    for box in boxes:
        (out / "by_id" / str(box[0])).write_bytes(encode(box))   # no relationships, so the record is the whole file
    random.Random(0).shuffle(boxes)   # the spatial index wants a random order
    (out / "spatial0" / "0_0").write_bytes(struct.pack("<Q", len(boxes)) + b"".join(encode(b) for b in boxes)
                                           + b"".join(struct.pack("<Q", b[0]) for b in boxes))
    return len(boxes)
