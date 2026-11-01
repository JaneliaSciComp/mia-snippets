"""`harness preview` (SPEC §6.7): raw.png with every paired label's boundaries drawn in, 8-bit RGB, numpy only."""
import json
import re
from pathlib import Path

import numpy as np

from .binding import LABEL_KEY
from .png import read_png, write_png

PALETTE = [(255, 0, 0), (0, 255, 0), (0, 128, 255), (255, 200, 0), (255, 0, 255), (0, 255, 255)]  # by label order


def boundaries(lab: np.ndarray) -> np.ndarray:
    """Labelled pixels that differ from a 4-neighbour: 1 px inside each object's edge."""
    b = np.zeros(lab.shape, bool)
    b[1:] |= lab[1:] != lab[:-1]
    b[:-1] |= lab[:-1] != lab[1:]
    b[:, 1:] |= lab[:, 1:] != lab[:, :-1]
    b[:, :-1] |= lab[:, :-1] != lab[:, 1:]
    return b & (lab != 0)


def _regular(p: Path) -> Path:
    if p.is_symlink() or not p.is_file():
        raise ValueError(f"{p} is not a regular file")
    return p


def preview(crops_dir: Path, out: Path) -> list[str]:
    """Writes out/<crop_id>/overlay.png per crop; returns one error per crop it could not draw."""
    errors = []
    for f in sorted(Path(crops_dir).glob("*/crop.json")):
        d = f.parent
        try:
            if d.is_symlink() or (d / "labels").is_symlink():
                raise ValueError(f"{d} or its labels/ is a symlink")
            meta = json.loads(_regular(f).read_text())
            edge = int(meta["box"]["h"])   # verify checked it against the source; the cap guards the decoder
            if not 0 < edge <= 4096:
                raise ValueError(f"box edge {edge} out of range")
            rgb = np.repeat((read_png(_regular(d / "raw.png"), edge)[0] >> 8).astype(np.uint8)[..., None], 3, axis=2)
            for i, lb in enumerate(meta["labels"]):
                if not re.fullmatch(LABEL_KEY, lb["label_key"]):   # never read outside labels/
                    raise ValueError(f"bad label_key {lb['label_key']!r}")
                lab = read_png(_regular(d / "labels" / f"{lb['label_key']}.png"), edge)[0]
                rgb[boundaries(lab)] = PALETTE[i % len(PALETTE)]
            (Path(out) / d.name).mkdir(parents=True, exist_ok=True)
            write_png(Path(out) / d.name / "overlay.png", rgb, 8)
        except Exception as e:   # one bad crop doesn't stop the others
            errors.append(f"{d.name}: {type(e).__name__}: {e}")
    return errors
