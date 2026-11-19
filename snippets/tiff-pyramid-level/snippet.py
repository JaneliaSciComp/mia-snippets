"""One page (pyramid level) of a tiled, multi-page TIFF or BigTIFF at a plain URL, read tile by tile.

A whole-slide image is a pyramid: page 0 is the scan (tens of gigabytes decoded) and each further page is a
smaller copy of it. The catalog's other TIFF readers present tifffile's first series, which for such a file is
page 0 alone, and decode a whole page per read. This snippet presents the page the binding names (`level`) and,
for a read, fetches and decodes only the tiles that overlap the requested region. A mask pyramid on half the
slide's grid is paired with the slide by naming the level of each that has the same shape. With `class_id` a
class-id mask is presented as the uint8 {0,1} mask of that one class."""
import numpy as np
import tifffile

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

UNIT_NM = {2: 25.4e6, 3: 1e7}     # TIFF ResolutionUnit: 2 inch, 3 centimetre; 1 (none) and absent mean no size
GAP = 1 << 20                     # tiles closer than this in the file are fetched by one read (io's block size)
RUN = 64 << 20                    # and one read is at most this long


class _Level:
    access = "chunk"

    def __init__(self, tif: tifffile.TiffFile, level: int, class_id: int | None):
        self.tif = tif
        try:
            page = tif.pages[level]
        except IndexError:
            raise ValueError(f"params.level {level} is out of range: the file has {len(tif.pages)} pages "
                             "(0-based)") from None
        if not isinstance(page, tifffile.TiffPage) or not page.is_tiled:
            raise ValueError(f"page {level} is not tiled; this snippet reads tiled pages only")
        if page.imagedepth != 1 or page.tiledepth != 1:
            raise ValueError(f"page {level} has depth {page.imagedepth} (tile depth {page.tiledepth}); "
                             "this snippet reads 2D pages only")
        samples = page.samplesperpixel
        if samples > 1 and page.planarconfig != tifffile.PLANARCONFIG.CONTIG:
            raise ValueError(f"page {level} stores its {samples} samples in separate planes; this snippet reads "
                             "interleaved (contiguous) samples only")
        self.page = page
        self._th, self._tw = page.tilelength, page.tilewidth
        self._across = -(-page.imagewidth // self._tw)       # tiles per row of tiles
        down = -(-page.imagelength // self._th)
        if len(page.dataoffsets) != self._across * down or len(page.databytecounts) != self._across * down:
            raise ValueError(f"page {level} lists {len(page.dataoffsets)} tile offsets and "
                             f"{len(page.databytecounts)} byte counts for a {down}x{self._across} tile grid")
        self._class_id = class_id
        if class_id is not None and (samples > 1 or np.dtype(page.dtype).kind not in "iu"):
            raise ValueError(f"params.class_id is set, but page {level} holds {samples} sample(s) of "
                             f"{np.dtype(page.dtype).name}; a class-id map is one integer sample per pixel")
        self.dtype = np.dtype(page.dtype if class_id is None else np.uint8)
        self.axes = "yxc" if samples > 1 else "yx"
        self.shape = (page.imagelength, page.imagewidth) + ((samples,) if samples > 1 else ())
        self.voxel_size_nm = _voxel_size(page)

    def __del__(self):          # the harness never closes arrays; the range file's handle goes with the array
        self.tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        unknown = set(region) - set(self.axes)
        if unknown:
            raise ValueError(f"region names axes {sorted(unknown)}, the array has {self.axes!r}")
        y0, y1, ys = _span(region.get("y", slice(None)), self.shape[0], "y")
        x0, x1, xs = _span(region.get("x", slice(None)), self.shape[1], "x")
        out = np.zeros((y1 - y0, x1 - x0) + self.shape[2:], self.page.dtype)   # the box that holds the selection
        if out.size:
            self._fill(out, y0, x0)
        if self._class_id is not None:
            out = (out == self._class_id).astype(np.uint8)
        return out[(ys, xs) + ((region.get("c", slice(None)),) if "c" in self.axes else ())]

    def _fill(self, out: np.ndarray, y0: int, x0: int) -> None:
        """The tiles overlapping out's box (whose corner is y0, x0), decoded into it. A tile the file does not
        store (byte count 0) stays 0."""
        page, th, tw = self.page, self._th, self._tw
        y1, x1 = y0 + out.shape[0], x0 + out.shape[1]
        wanted = [ty * self._across + tx for ty in range(y0 // th, (y1 - 1) // th + 1)
                  for tx in range(x0 // tw, (x1 - 1) // tw + 1)]
        stored = sorted((page.dataoffsets[i], page.databytecounts[i], i) for i in wanted if page.databytecounts[i])
        fh = self.tif.filehandle
        while stored:
            run = [stored.pop(0)]                     # tiles in file order, close enough to come by one read
            start, end = run[0][0], run[0][0] + run[0][1]
            while stored and stored[0][0] - end <= GAP and stored[0][0] + stored[0][1] - start <= RUN:
                run.append(stored.pop(0))
                end = max(end, run[-1][0] + run[-1][1])
            fh.seek(start)
            data = fh.read(end - start)
            if len(data) != end - start:
                raise ValueError(f"tile data at bytes {start}..{end} is cut short: the file ends at {start + len(data)}")
            for offset, count, i in run:
                tile = page.decode(data[offset - start:offset - start + count], i, jpegtables=page.jpegtables)[0]
                if tile is None:
                    continue
                tile = np.asarray(tile)[0]            # (depth 1, rows, columns, samples) -> (rows, columns, samples)
                ty, tx = divmod(i, self._across)
                ya, xa = max(y0, ty * th), max(x0, tx * tw)
                yb, xb = min(y1, ty * th + tile.shape[0]), min(x1, tx * tw + tile.shape[1])
                part = tile[ya - ty * th:yb - ty * th, xa - tx * tw:xb - tx * tw]
                out[ya - y0:yb - y0, xa - x0:xb - x0] = part if out.ndim == 3 else part[..., 0]


def _span(sel: int | slice, n: int, axis: str) -> tuple[int, int, int | slice]:
    """A selection on an axis of length n as the run [lo, hi) of the axis that holds it, and the selection to
    apply to that run."""
    if isinstance(sel, slice):
        r = range(n)[sel]
        if len(r) == 0:
            return 0, 0, slice(0, 0)
        # the run's ends are r[0] and r[-1], so stepping through all of it (from its far end when the step is
        # negative) lands on exactly r's elements
        return min(r[0], r[-1]), max(r[0], r[-1]) + 1, slice(None, None, r.step)
    k = int(sel) + n if int(sel) < 0 else int(sel)
    if not 0 <= k < n:
        raise IndexError(f"{axis} {sel} is out of range for {n}")
    return k, k + 1, 0


def _voxel_size(page: tifffile.TiffPage) -> dict[str, float] | None:
    """From this page's own XResolution/YResolution (pixels per ResolutionUnit); None when the unit is not a
    length or a resolution is missing or zero."""
    unit = page.tags.get("ResolutionUnit")
    scale = UNIT_NM.get(int(unit.value)) if unit is not None else None
    out = {}
    for a, name in (("y", "YResolution"), ("x", "XResolution")):
        tag = page.tags.get(name)
        num, den = tag.value if tag is not None else (0, 0)
        if scale and num and den:
            out[a] = den / num * scale
    return out if len(out) == 2 else None


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"tiff-pyramid-level reads one file, got {len(src.urls)} urls")
    level, class_id = src.params.get("level", 0), src.params.get("class_id")
    if isinstance(level, bool) or not isinstance(level, int) or level < 0:
        raise ValueError(f"params.level must be a page index from 0, got {level!r}")
    if class_id is not None and (isinstance(class_id, bool) or not isinstance(class_id, int)):
        raise ValueError(f"params.class_id must be an integer, got {class_id!r}")
    return _Level(tifffile.TiffFile(io.open(src.urls[0])), level, class_id)
