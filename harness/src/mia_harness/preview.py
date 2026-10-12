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


def preview(crops_dir: Path, out: Path) -> None:
    for f in sorted(Path(crops_dir).glob("*/crop.json")):
        d = f.parent
        rgb = np.repeat((read_png(d / "raw.png")[0] >> 8).astype(np.uint8)[..., None], 3, axis=2)
        for i, lb in enumerate(json.loads(f.read_text())["labels"]):
            if re.fullmatch(LABEL_KEY, lb["label_key"]):   # verify reports bad keys; never read outside labels/
                rgb[boundaries(read_png(d / "labels" / f"{lb['label_key']}.png")[0])] = PALETTE[i % len(PALETTE)]
        (Path(out) / d.name).mkdir(parents=True, exist_ok=True)
        write_png(Path(out) / d.name / "overlay.png", rgb, 8)
