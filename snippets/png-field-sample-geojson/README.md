# png-field-sample-geojson

Reads one **field of view stored as a folder of PNG files with a GeoJSON annotation**, from plain HTTP(S) URLs
or zip members: the channels, one plane of an RGB-coded mask, or the polygons filled onto the field's grid. One
binding entry is one array of one field, so fields of different shape or bit depth are bound side by side
instead of stacked. PNGs are decoded with `imagecodecs` (libpng); the GeoJSON is parsed with `json` and filled by
the snippet itself (no geometry library is on the allowlist).

The form is chosen by the urls and params of the entry. Globs (`<zip url>::<glob>`, `<dir url>/::<glob>`) are
expanded with `io.expand`, sorted by name, in place; the same file twice is refused.

## Channels form: PNG urls, no params

Grey 8- or 16-bit PNGs (IHDR colour type 0), one file per channel. One url is `yx`; several are `cyx` with `c` =
position in the urls after glob expansion, so name the channel order in the binding's `reasons`. Stored values
are returned unchanged (`uint8` or `uint16`). All files must have the first file's size and bit depth; one that
differs raises at `read`, naming the url. A colour PNG is refused at open (use the sample form).

## Sample form: one PNG url, `params: {sample: <k>}`

One RGB, RGBA or grey+alpha PNG (colour types 2, 6, 4; 8 or 16 bits). Returns sample `k` (0-based, in file
order: R, G, B, alpha) as `yx`, values unchanged. This is for a mask whose classes are coded in the colour
planes: each plane is bound as its own label. Which plane holds what is not in the file, so measure every plane
and say what was found in `reasons`. An index outside the file's samples, a grey PNG, or several urls raise at
open.

## Polygons form: one `.json`/`.geojson` url and one PNG url, optional `params: {y_up: true}`

The GeoJSON url is recognised by the ending of its path; the other url is any PNG of the same field and gives
only the grid (its header's height and width; none of its pixels is read).

- **The array** is `yx` `uint16` on that grid. A pixel holds the id of the feature whose polygon encloses the
  pixel's centre, 0 where none does. A feature's **id is its 1-based position in the file's `features` list**.
  Where two features overlap, the later one is kept.
- **Geometry.** `Polygon` and `MultiPolygon` features of a `FeatureCollection`; rings are filled by the
  even-odd rule, so a ring inside the exterior ring is a hole. Any other geometry type (points, lines, a null
  geometry) raises, as does a ring of fewer than 3 positions or with a non-finite coordinate. A closing vertex
  that repeats the first is accepted but not required. Parts of a polygon outside the grid are cut off.
- **Coordinate frame.** A position is `[x, y]` in pixels and an integer coordinate is a pixel centre. Without
  params `x` is the column and `y` the row (y down). With `y_up: true` the y axis points up from the bottom edge
  of the image: a vertex `(x, y)` is at column `x`, row `height - y`. GeoJSON does not say which it is; check
  it against the image or a mask of the same field and give the numbers in `reasons`. A centre exactly on an
  outline belongs to the polygon whose interior is to its right and below.
- **Not read:** feature `properties` (every feature is filled, whatever its label), `bbox`, `crs`.
- More than 65,535 features raise.

## Common to all forms

- **Access `file`.** A PNG is one deflate stream, so a file is downloaded and decoded whole and the last decoded
  file is kept; the polygon label is read and filled once, on the first `read`, and kept. `open_array` reads
  only one PNG header (signature + `IHDR`). A stored zip member or a Range-capable URL is read through `io.open`;
  a deflated member raises `NeedsFetch` and is staged whole with `io.fetch`, header read included.
- **A throttling host.** A step that makes requests (a glob expansion, a header, a file) is tried up to 6 times
  while the host answers `429 TOO MANY REQUESTS`, which the harness takes as final. The wait is the answer's
  `Retry-After` in seconds plus one, or 30, 60, 90 s when it names none; 90 s at most. The error is told by
  class name, as a snippet may not import the HTTP client. What an earlier try fetched (cached blocks, the
  zip's directory, staged members) is not asked for again. Every other error is raised at once; dropped
  connections and 5xx answers are retried by the harness.
- **Voxel size** `None`: a PNG's `pHYs` chunk is a print resolution and GeoJSON has none.
- Refused: palette PNGs and bit depths below 8.

## Known limits

- One field per binding entry. A dataset of hundreds of field folders needs one raw and its labels listed per
  field; the snippet cannot return several arrays from one glob (SPEC §2: one `open_array`, one array), and it
  does not stack fields because their shapes differ.
- Each array opened costs at least one zip member (two requests for a deflated one), and a field read whole
  costs 12 requests. Zenodo counts requests per address in windows of about a minute (`x-ratelimit-limit: 133`
  on this record's zip) and other jobs behind the same address use the same count: it answered `429` when nine
  fields (54 members) were read in one go, and to the third request of a crop job. The snippet waits a 429 out
  (above); a limit that stays used up for more than about five minutes still fails the job.
- The polygon filler samples pixel centres; it is not anti-aliased and a polygon thinner than a pixel can vanish.
- Tested on real data: 8- and 16-bit grey channels, 8-bit RGB masks, single-ring `Polygon` features with
  `y_up: true`, deflated zip members. Holes, `MultiPolygon`, y-down, RGBA, grey+alpha and plain URLs were run on
  synthetic input only.

## Tested on

`zenodo-4665863-hpa-cell-segmentation` (examples/): the Human Protein Atlas cell segmentation set, a 5.2 GB zip
of field folders with `er.png`, `microtubules.png`, `nuclei.png`, `protein.png`, `cell_border_mask.png` and
`annotation.json`. The example binds four of the nine `valid/` fields; all nine were measured through this
snippet:

| field | channels | cells | sample 0 / 1 / 2 pixels set | IoU(polygons, sample 2) y up | y down |
|---|---|---|---|---|---|
| 14060_105_G9_1 | 4x1728x1728 uint8 | 13 | 0 / 2,403 / 693,541 | 0.9580 | 0.1373 |
| 14060_107_G9_1 | 4x1728x1728 uint8 | 21 | 0 / 15,270 / 1,721,976 | 0.9708 | 0.3623 |
| 20026_985_B12_1 | 4x2048x2048 uint16 | 23 | 0 / 6,493 / 1,704,314 | 1.0000 (45 px differ) | 0.3431 |
| 2990_1586_E11_1 | 4x2048x2048 uint16 | 14 | 0 / 1,985 / 2,264,647 | 1.0000 (21 px) | 0.3583 |
| 30901_1656_F1_1 | 4x2048x2048 uint16 | 35 | 0 / 19,312 / 1,593,662 | 1.0000 (11 px) | 0.2105 |
| 40520_1775_F1_1 | 4x3072x3072 uint16 | 15 | 0 / 2,234 / 1,645,503 | 1.0000 (55 px) | 0.1586 |
| 44227_si31_G10_7 | 4x2048x2048 uint16 | 33 | 0 / 971 / 1,249,752 | 0.9491 | 0.2250 |
| 48363_780_E12_2 | 4x2048x2048 uint16 | 17 | 0 / 2,191 / 2,147,941 | 0.9716 | 0.3305 |
| 6885_1032_G10_1 | 4x2048x2048 uint16 | 30 | 0 / 80,107 / 3,693,494 | 1.0000 (10 px) | 0.7914 |

- **The mask's planes.** Sample 0 (R) is zero everywhere. Sample 2 (B) is the cell area. Sample 1 (G) is a thin
  set, far too small to be every cell's outline; in the three fields checked (2990_1586_E11_1, 30901_1656_F1_1,
  40520_1775_F1_1) every one of its pixels lies within 6 pixels of two different polygon ids, against 0.04% to
  0.8% of all pixels. So sample 1 marks the borders where neighbouring cells touch.
- **The polygons' frame.** With `y_up: true` (row = height - y) the filled polygons equal sample 2 to within 10
  to 55 pixels in five fields. On field 2990_1586_E11_1 the next best offsets, half a pixel away in x or y,
  differ in 6,401 to 8,270 pixels, and y down, the other flips, rotations and transposes give IoU 0.36 to 0.44.
  In the other four fields the IoU is 0.95 to 0.97 at every sub-pixel offset tried, so there the stored mask
  was not drawn from the polygons as they are in the file (my reading: a different revision of the annotation);
  y up still fits far better than y down. In every field the channels are much brighter inside the polygons
  than outside (nuclei channel of 2990_1586_E11_1: mean 4,083 inside, 0.4 outside).
- The shapes and bit depths differ between fields (1728, 2048 and 3072 pixels square; 8 and 16 bits), which is
  why each field is its own array.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
