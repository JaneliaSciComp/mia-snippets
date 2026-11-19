# roi-json-outlines-tiff-grid

Reads one **ROI JSON** file, a bare list of outlines as ImageJ ROIs are exported to JSON,

```json
[
    {"type": "freehand", "points": [[1034, 348], [1033, 349], ...]},
    {"type": "polyline", "points": [[412, 77], [431, 80], ...]}
]
```

as a label on the pixel grid of the **TIFF** image the outlines were drawn on: `yx` `uint16` instance ids, one per
entry. The file has no image size, no `shapes`/`features` wrapper, no labels and no file name inside, which is why
`labelme-json-polygons`, `via-json-polygons`, `coco-json-polygons` and the GeoJSON snippets do not read it
(`czi-mosaic-imagej-roi` and `tiff-imagej-points` read binary `.roi` files).

## Urls and what is read

`urls: [<the .json>, <its TIFF>]`, in any order: exactly one url whose path ends in `.json` and exactly one other,
which must be a TIFF. Each is a plain URL or a zip member `<zip url>::<member>` (a glob is expanded with
`io.expand` and must stand for one file).

- **The TIFF gives the grid and nothing else.** The shape is the ImageLength x ImageWidth of its first page, and the
  voxel size is the OME-XML's `PhysicalSizeX/Y`, else ImageJ's unit and resolution, else `None` (what
  `tiff-zip-member-4` reports for the same file). No pixel is decoded. A Range-capable plain URL or a stored zip
  member costs its header blocks; a deflated member (`NeedsFetch`) is staged whole with `io.fetch`, which is the
  file the raw's snippet stages too, so in a binding that also reads the raw it comes from the cache.
- **The JSON is read and parsed whole at `open_array`** (the files are small: 0.1 to 2.9 MB in the example), so a
  file this snippet cannot read fails at open, not at the first crop. Nothing is filled until `read`.
- **The array** is `yx`, `uint16`, `access` `plane`. `read` fills only the region asked for, from the outlines
  whose bounding boxes touch it; a whole 2993 x 2824 plane of 135 outlines takes 0.03 s.
- **Pairing is the binding's.** The snippet does not compare the two file names: the binding names both urls, and
  its `reasons` say why they belong together.

## How outlines become pixels

- **Entry types.** `type` is ImageJ's ROI type name. Only the types that are a list of points are read:
  - `polygon`, `freehand`, `traced`: closed outlines, always filled.
  - `polyline`, `freeline`: open lines. They enclose nothing as they stand, and a file cannot say whether its
    author traced an outline back to its start or drew a line along a structure, so **an open entry is an error at
    open unless `params.open_outlines` is set** (the error counts them and names the first):
    - `open_outlines: close` closes each with a straight segment from its last point to its first and fills it
      exactly like a polygon;
    - `open_outlines: skip` leaves them out; the other entries keep their ids.
  - Any other type (`rectangle`, `oval`, `line`, `point`, ...) raises: its `points` would not be an outline.
  - An entry that is not an object with a string `type` and 3 or more finite `[x, y]` points raises.
- **Coordinate frame.** `points` are `[x, y]` in pixels of the paired image, x to the right and y down, an integer
  being a pixel corner (ImageJ's convention: pixel (column c, row r) covers `[c, c+1) x [r, r+1)`). A pixel
  belongs to an outline when its **centre** `(c + 0.5, r + 0.5)` lies inside it by the **even-odd rule** (ImageJ's
  own polygon fill), the outline being closed from its last point to its first whether or not the file repeats
  the first point. This is the convention of `labelme-json-polygons` and `czi-mosaic-imagej-roi`.
- **Ids.** A pixel holds the entry's **1-based position in the list**, counting every entry (so with `skip` the
  ids are not consecutive). 0 is background. More than 65,535 entries raise. A list with no entries is read as an
  all-zero label.
- **Overlap.** Outlines are painted largest first (by enclosed area; file order breaks ties), so where two overlap
  the smaller one is on top and an outline drawn inside another stays visible. An overlapped pixel carries one id.
- **Frame check.** Parts of an outline outside the grid are clipped. If the filled outlines reach more than 16
  pixels beyond the TIFF's grid on any side, `open_array` fails: the coordinates are then not this image's pixels
  (a wrong pairing or another scale). Hand-drawn outlines do leave the canvas a little (8 px at most in the
  example), which is what the 16 px allow. Images of similar size pass this check whatever they show, so it does
  not replace the binding's reason for a pairing.

## Params

| param | meaning |
|---|---|
| `open_outlines` | Absent: an entry of an open type (`polyline`, `freeline`) is an error. `close`: closed with a straight segment and filled. `skip`: left out. Set it from what the open entries of the dataset are, measured (do their ends meet? do they enclose what the closed entries enclose?), with the reason in the binding. |

## Known limits

- `rectangle`, `oval`, `line`, `point` and composite ROIs are not read; the format seen has only `type` and
  `points`, and how such a ROI would be written in it is not known.
- With `close`, an open entry is closed however far apart its ends are; the snippet does not judge whether it was
  meant as an outline. That is the binding's claim.
- A self-intersecting outline follows the even-odd rule: a loop the outline makes over itself twice is a hole. In
  the example this differs from a nonzero-winding fill on 0.02 % of the filled pixels (0.39 % in the worst file).
- An id can have more than one 4-connected part: where the two sides of an outline come within a pixel of each
  other or cross along a thin cell process, the fill pinches off. In the example 1,986 of 2,652 ids are one part
  (11 parts at most); on four files measured, 0.33 % of the labelled pixels lie outside their id's largest part,
  mostly fragments of 1 or 2 pixels.
- Only `type` and `points` are read; any other key of an entry is ignored. One file is one `yx` array: files are
  not stacked into planes.
- Run on real data: `polygon`, `freehand` and `polyline` with `open_outlines: close`, in deflated zip members, on
  ImageJ TIFFs without a pixel size. Run on synthetic files only (compared pixel for pixel with a brute-force
  crossing-number test of every pixel centre, 60 random sets of outlines, and every region read against the whole
  plane): `traced`, `freeline`, `skip`, fractional coordinates, the error paths, plain Range-read URLs, and the
  OME and ImageJ voxel sizes.

## Tested on

`figshare-25604421-ntera2-cells-bf-segmentation` (examples/): brightfield images of Ntera-2 cells, 36 training
TIFFs (`training_dataset.zip`, ImageJ, one float32 plane each, 2826 to 3040 rows by 2759 to 2953 columns, deflated
members of 10 to 13 MB) and one ROI JSON each (`Training_ROIs_json.zip`). `harness smoke` of the example binding
(34 raws with `tiff-zip-member-4`, 34 labels with this snippet): ok, every label on its raw's grid, no errors,
397 MB fetched (the 34 TIFF members; the JSONs are 2 MB). `harness crop --n 4`, `verify` and `preview` ran on it
too; verify's only complaint was `crop.json has no licence`, which the issue's record block does not carry.

- **All 36 pairs were read**, the two the binding leaves out included: 2,652 entries (1,286 `polygon`, 937
  `polyline`, 429 `freehand`), 18 to 252 per file. Every entry keeps pixels of its own; labels cover 3.6 to 25.5 %
  of a plane; overlap between outlines is 0.0 to 1.3 % of the labelled area per file.
- **The polylines are closed outlines** (`open_outlines: close`): the gap between a polyline's first and last
  point is 0 px for 293 of the 937, 1 px in the median, at most 5 px for 865 and 68 px at most, against a median
  outline diagonal of 252 px; their median enclosed area is 6,514 px, the polygons' 5,532 px. 20 of the 36 files
  hold polylines and 8 hold nothing else; overlaid on its image, a file of 135 polylines outlines 135 cells.
- **Frame.** Outlines span x 0..2939, y -8..2988 and none passes its image's right or bottom edge. Score = mean
  gradient magnitude inside the filled outlines over that outside (4x binned image; cells are textured, the
  background is flat). On five pairs: 2.04 to 2.64 as read, 1.94 to 2.49 moved 8 px, 1.73 to 2.16 moved 20 px,
  0.97 to 1.30 flipped in y or x, 1.07 to 1.42 with x and y exchanged.
- **Pairing.** With every JSON scored against every image, each of the 36 scores highest on the image it is paired
  with: 2.01 to 2.91 there, 1.58 at most elsewhere. 35 pairs follow from the names (`<stem>[.MP].tif`,
  `<stem>_ROI.json`). The 36th does not: `PXL_20220930_113717286_ROI.json` and `PXL_20220930_113717826.tif` are
  the two files left over, their names differ by two exchanged digits, and this JSON scores 2.04 on that image
  against 1.20 at most on the other 35. The binding binds them as a pair and asks a human to confirm it.
- **Two raws are left out of the binding** (their labels read like the others): `PXL_20220927_150744847.tif` has
  NaN and values up to 3.4e38 in rows 622 to 625 and `PXL_20220927_151312486.MP.tif` in rows 296 to 298, as
  published (the zip's CRC passes). See the binding's `reasons`.
