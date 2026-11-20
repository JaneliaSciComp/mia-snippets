# tiff-csv-manifest-canvas-i291

Reads a set of single-page TIFF files whose names are the rows of a **CSV manifest**, one file per z plane, grey
(`zyx`) or colour (`czyx`). It is for a dataset whose folders cannot be listed (a HuggingFace dataset repo: its
`tree/` page is not a directory index and `resolve/main/images/` answers 404) but which ships a table of its
files, and whose files differ in height and width.

**This snippet adds pixels the files do not hold** when the files of an array differ in size: an array has one
shape, so every plane is a canvas of `params.canvas` (height, width), with the file's pixels unchanged at its top
left and 0 beyond them. See *What the fill means*; `oversize` and `larger_than` exist to keep it small.

- **URL.** One plain url: the manifest, a CSV with a header line (UTF-8, comma separated, quoted cells allowed).
  No glob, no zip member.
- **`column`** (required). The header of the column that holds each file's name. A name may hold letters, digits
  and `._~-` only; any other character raises (nothing is percent-encoded, so a manifest cannot point the reader
  at another url). A name that occurs twice among the selected rows raises.
- **`files`** (required). The path of a row's file relative to the manifest's folder, with `{name}` once where
  the row's name goes: `images/{name}.tif` for the raw, `masks/{name}.tif` for its labels. No `..`, no leading
  `/`, no scheme: the files are below the folder the manifest is in, on its host.
- **`where`**. `{column: value}` or `{column: [values]}`. A row is read only when its cell in every named column
  is the value, or one of the values, compared as text (`{domain: malnio}`, `{shape: "(2592, 2592)"}`).
- **Order.** The selected rows are sorted by name as strings (`malnio__1, malnio__10, malnio__100, malnio__11`),
  whatever the manifest's row order. z index k is the k-th name. A raw and its labels take the same `column`,
  `where`, `shape_column`, `canvas`, `oversize`, `larger_than` and `count` and differ in `files` only, so they
  hold the same rows in the same order: plane k of the label is the mask of plane k of the raw.
- **`canvas: [height, width]`** (required). The shape of every plane. A file of `h x w` occupies rows `0..h-1`
  and columns `0..w-1`, so pixel (y, x) of a plane is pixel (y, x) of its file, and every other pixel is 0 (in
  every channel). A file taller or wider than the canvas raises; nothing is cropped.
- **`shape_column`**. The header of a column that holds each file's shape as text: `(h, w)`, `(h, w, samples)`,
  `[h, w]` or `hxw`; height and width are the first two numbers. With it the canvas is checked against every
  selected row when the array is opened, and each file is checked against its row when it is read: a file whose
  height or width is not the manifest's raises. The masks of a `(h, w, 3)` image are checked against `(h, w)`.
- **`oversize: skip`** and **`larger_than: [height, width]`** (both need `shape_column`). They divide the rows of
  one manifest by size, so that each array's canvas is close to its files. `oversize: skip` leaves out the rows
  that do not fit the canvas instead of raising. `larger_than` leaves out the rows that fit in the given box
  (height <= its height and width <= its width). A chain of arrays, each with `larger_than` the previous one's
  canvas and all but the last with `oversize: skip`, holds every row exactly once when the canvases grow in both
  directions; when they do not (a landscape and a portrait canvas), add up the `count`s to see that no row is
  left out.
- **`count`**. The number of planes after `where`, `oversize` and `larger_than`. Open fails on another number,
  so a manifest that changed is noticed instead of shifting z.
- **Files.** Single-series, single-page TIFF, `YX`, or `YXS`/`SYX` (colour). The first file (by name) decides the
  array: its dtype, and whether there is a `c` axis and how long it is. Every other file must have the same dtype
  and number of samples, or its `read` raises.
- **Axes.** `zyx` for grey files, `czyx` for colour files, with the stored samples as c in file order (R, G, B).
  Colour is never reduced to one channel. Always a z axis, also for one file. **Voxel size:** `None`.
- **Access.** `plane`: one file is one z plane and any region of it costs the whole file. Every file is staged
  whole with `io.fetch`, into the dataset cache. `open_array` stages the manifest and the first file and reads
  that file's TIFF header only; `read` stages the files it needs. The last plane built is kept, so the channels of
  one colour plane cost one decode.

## What the fill means

- The 0 beyond a file's height and width is not data. In a raw array it is black that the image does not hold;
  in a label array it reads as background, although nothing is known there. A raw file may hold real 0 pixels
  too, so fill cannot be told from data by its value: a plane's file extent is the manifest's shape for its row.
- The harness draws crops anywhere on the canvas, so a crop near the bottom or right edge of a plane can show a
  file's edge with black beside it, and its intensity stretch then includes the fill. A crop that holds fill only
  is constant, and the harness's content filter drops it.
- Keep each canvas tight: the largest height and the largest width among the array's files, no more, and divide
  a manifest whose files differ much in size with `oversize` and `larger_than`. Files of one shape, with their
  shape as the canvas, have no fill at all.
- The planes are independent images. z is the order of the names, not depth.

## Choosing params

- `column`, `files`: from the manifest's header and the dataset's layout; open one file url by hand to see that
  the path is right.
- `where`: one array per group of files that share dtype and colour (here the manifest's `domain`).
- `canvas`, `oversize`, `larger_than`, `count`: from the manifest's shape column, per group. Without a shape
  column the canvas has to be measured from the files (`harness describe <url>` per file), and a canvas that is
  too small is found only when the file that does not fit is read.

## Known limits

- The fill, above.
- The file urls are not binding urls: they are made from the manifest. `crop.json` and the stage's provenance name
  the manifest (with its validators), the binding's params and the plane index, not the file; the file of plane
  k is the k-th selected name in string order. `harness fetch` and the prefetch of `harness stage` stage the
  manifest alone; the files are staged when they are read. An offline job (a re-stage from the dataset cache)
  finds the manifest, each array's first file and every file an earlier job with that cache read, and raises
  `HostNotAllowed` at the first plane it has not.
- A url that names a branch (`resolve/main/`) follows the branch. `count` and `shape_column` notice a manifest
  whose rows or shapes changed; a file replaced by another of the same shape is noticed only by its validators.
  A HuggingFace url can name a commit instead (`resolve/<sha>/manifest.csv`), which pins the manifest and the
  files.
- Names are not percent-encoded: a manifest whose names hold spaces or other characters cannot be read.
- One manifest per array, one file per row, TIFF only; no multi-page files, pyramid levels, PNG or JPEG. A
  palette TIFF is read as its indices.
- Each file is read whole into memory and costs a HEAD and a GET (on HuggingFace each is a redirect first).
- `where` compares text exactly: `(2592, 2592)` is not `(2592,2592)`.
- The first file is fetched at open (whole, though only its header is read), so opening an array of large files
  costs one of them.

## Example

`examples/hf-toxoplasma-plaque-assay-masks.yaml`: the HuggingFace dataset
`einarolafsson/toxoplasma-plaque-dataset`, 488 brightfield images of Toxoplasma plaque assays with one
instance mask each (`images/<name>.tif`, `masks/<name>.tif`), named by `manifest.csv` (columns `name`, `domain`,
`shape`, `dtype`, `n_objects`, ...). The binding reads all 488 pairs as 10 raw and 10 label arrays: the four
domains, and within `malnio` (RGB uint8, 258x259 to 1141x1136), `literature` (grey uint8, 573x581 to
1307x1200) and `patrick` (grey uint16, 1728x1728 or 2592x2592) one array per size class, so that the files
cover 83% to 100% of their canvases. All 976 files were read through the snippet (1.28 GB): every image and
mask has the height and width of its manifest row and the dtype the manifest gives, nothing but 0 lies beyond a
file's extent, the three samples differ in every `malnio` image, and every mask holds the ids 1..N without gaps
with N equal to the manifest's `n_objects` (31 `literature` masks are empty, as their `n_objects` 0 says). Three
pairs (one each of `malnio`, `literature`, `patrick`) were also downloaded apart and decoded with tifffile: the
planes hold exactly those pixels. The `c` axis, the size classes, the errors and the offline read were exercised
on synthetic files over a local server as well. The example binding's `snippets_commit` is the commit before this
snippet's own (a binding can't name the commit that adds it); a binding written by the worker names a commit
that contains the snippet.
