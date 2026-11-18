# via-csv-boxes-png-grid

Reads a set of **VIA (VGG Image Annotator) CSV box annotations, one CSV per image,** as one label array, one
CSV per z plane (`zyx`, `uint16`; `z` is dropped for a single file, as `image-tiles-glob` does for its images).
Each CSV's `rect` regions are rasterised on the grid of **the one PNG in the CSV's own folder**. No catalog
snippet read a CSV box table before this one: `tiff-jpeg-voc-zip-glob` and `jpeg-xml-boxes-numbers` take
VOC-like XML only. It reads labels only; the images are bound with an image snippet such as
`image-tiles-glob`.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`). A glob is expanded with `io.expand`, **sorted by name**, the order `image-tiles-glob`
  uses; several URLs are concatenated in binding order. With one image and one CSV per folder
  (`<id>/<id>.png`, `<id>/region_data_<id>.csv`) the folder decides both orders, so an image glob and a CSV
  glob over the same folders pair plane for plane. The snippet cannot check that across arrays, so the
  binding's `reasons` must say why they pair.
- **The grid** is the PNG beside the CSV: the one file with the extension `image_ext` (default `png`, matched
  case-insensitively) directly in the CSV's folder, found in the zip's member list or the directory's index.
  Its `IHDR` gives (Y, X); no pixel is decoded. None or several such files raise. **The CSV's `filename`
  column is not used to find the image**, because exports that were renamed or regenerated name files that do
  not exist (see the example). It is only checked to be the same in every row: a CSV with rows for several
  images (a whole VIA project in one file) raises, since this snippet reads one CSV per image.
- **The CSV.** The header must have the VIA columns `filename`, `region_count`, `region_shape_attributes` and
  `region_attributes`, found by name; other columns (`file_size`, `file_attributes`, `region_id`) are not read.
  A leading `#` on the header line (`#filename,...`, as VIA 1 wrote it) and CRLF line ends are accepted. Each
  further row is one region:
  - `region_shape_attributes` is JSON `{"name":"rect","x":…,"y":…,"width":…,"height":…}`. Any other shape
    (`circle`, `polygon`, `point`, …) raises. An empty object `{}` is VIA's row for an image without regions
    and is skipped.
  - `region_attributes` is a JSON object; the class is the string under `class_key`. A **bare-word value**
    (`{"particle_class":immature}`, which is not JSON) is taken as that string: some exports lost the quotes.
    A region without the key raises.
  - `region_count` must equal the number of region rows in every row, so a truncated file raises.
  - A CSV with a header only, or only a no-region row, is an **all-background plane**.
- **Boxes** (`boxes`, `classes`, `class_key`: all required):
  - `classes`: every class string the files use, in class id order. A region whose class is not listed raises,
    in both modes, so a class the record does not know is found rather than dropped.
  - `boxes: semantic`: each box is filled with the 1-based position of its class in `classes`.
  - `boxes: instance`: each box is filled with its 1-based row number among the file's regions. `region_id` is
    not used: it starts at 0 in some exports and at 1 in others. Ids restart in every file (plane).
  - Background is 0. A box covers the pixels `[y, y + height) x [x, x + width)`: VIA stores the top-left
    corner in 0-based image pixels and the extent. A fractional corner is moved outwards (near corner floored,
    far corner ceiled): every pixel the box touches. A width or height that is not positive raises.
  - **`overhang`** (default 0): how many pixels a box may reach beyond the image edge. Within it the box is
    clipped to the image; beyond it the file raises. VIA lets a rectangle be dragged a few pixels over the
    edge, but a box far outside is the sign of a CSV drawn on another grid, so the allowance is the binding's
    to state, with the largest overhang found in the files.
  - Boxes are painted largest (as drawn) first, so where two overlap the smaller one is on top (row order
    breaks ties). An overlapped pixel carries one value; the other box's claim to it is lost.
- **The labels are boxes, not masks.** The array says where a rectangle was drawn, not where the particle's
  outline is, and in `semantic` mode touching boxes of one class merge. Whether a binding may call this
  `instance` or `semantic` is a human's decision (the record's encoding is `boxes-table`, for which the
  binding has no `segmentation_type`); see the example binding's `reasons`.
- **Access.** `plane`: one CSV is one plane. A stored member or a Range-capable plain URL is read through
  `io.open` (a PNG: its first 24 bytes); a deflated member raises `NeedsFetch` and is staged whole with
  `io.fetch`, the PNG included, although only its header is used. `io` keeps it, so the raw plane of the same
  crop costs no more. `open_array` reads the first CSV and its PNG's header; `read` checks every plane's PNG
  against the first file's shape and raises, naming the file, when it differs. The last painted plane is kept.
- **dtype** `uint16`. **Voxel size:** `None`; a VIA CSV has none.

## Known limits

- One CSV per image and one PNG per folder. A VIA project CSV covering many images, a folder of many images
  with CSVs beside them, or images that are not PNG are not read.
- `rect` regions only; no polygons, circles, ellipses or points.
- Over HTTP each deflated member costs two requests (local header, compressed bytes), made lazily per plane,
  and a deflated PNG is downloaded whole for its header. Zenodo answered 429 to a quick run of such requests
  while testing; `io` does not retry those.
- Shape, class and overhang checks beyond the first file happen at `read`, not at `open`: opening every member
  of a deflated set would fetch all of it.
- The bare-word repair applies to simple values (letters, digits, `_`, `.`, `-`, spaces) only; any other
  broken JSON raises.
- More than 65,535 regions in one file raise.

## Example

`examples/zenodo-5149062-hiv1-virion-tem-detection.yaml`: the TEMNet HIV-1 detection set, an 849 MB zip (all
members deflated) with one folder per micrograph, `rcnn_dataset_full/{train,val}/<id>/<id>.png` (4000x2620 RGB
PNG with three equal samples) and `region_data_<id>.csv` beside it. The 59 PNGs are bound with
`image-tiles-glob` (`samples: grey`) and the 59 CSVs with this snippet, `boxes: semantic`, classes mature,
immature, eccentric.

What the 59 CSVs hold (all read while testing): 3,534 `rect` regions (1,868 mature, 400 immature, 1,266
eccentric), 20 to 114 per file, none without regions. They come from two exports:

- 33 are plain VIA 2 exports (`filename` header, LF, `region_id` from 0, quoted class values);
- 26 were regenerated (`#filename` header, CRLF, `region_id` from 1, `file_size` `irrelevant`, and unquoted
  class values such as `{"particle_class":immature}`).

The `filename` column says `<id>-copy.png` in 23 files, `<id>-counting.png` in 10 and a path to the CSV itself
in 26; none of these is a member of the zip, hence the pairing by folder. Eight boxes in six train files start
at `y = -3`, three pixels above the image; the binding sets `overhang: 3` and they are clipped at the top
edge. Nothing else leaves the image.

Checked on the data: `harness smoke` (raw and label `(59, 2620, 4000)`), and the painted boxes of one file of
each export (`train/0133009`, `train/0135011`) were laid over their micrographs and sit on the virions. The
Only the PNGs of the planes read in these checks were fetched (the zip is 849 MB); a micrograph of another size
raises at `read`.
The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
