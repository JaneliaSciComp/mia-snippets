# tiff-zip-member-box-i90

Reads one TIFF (multi-page or single page) with `tifffile`, from a zip member (`<zip url>::<member>`) or a
plain URL. Exactly one URL per array. Supersedes `tiff-zip-member`. It keeps every param of
`tiff-zip-member-2` (`axes`, `channel`, `z_first`, `z_last`) with the same meaning, and with only those it
presents the same array as that snippet (axes, shape, dtype, voxel size, access, values); with no params, or
only `axes`, the same array as `tiff-zip-member`. It adds one thing a binding may ask for:

- **A box of the file** (`y_first`, `y_last`, `x_first`, `x_last`, beside `z_first`, `z_last`). The array is the
  file's rows `y_first..y_last` and columns `x_first..x_last` (0-based, both included) of its planes
  `z_first..z_last`; its index 0 on each axis is the file's `*_first`. Any end may be left out and then is the
  file's own.

Why it exists: `tiff-zip-member` presents the whole file. A volume whose manual segmentation traces a few
objects is unlabelled everywhere else, and its raw is full of structures of the same kind there. Crops drawn
over the whole of it pair those with an empty label. That is what a reviewer rejected for
`empiar-12244-cryo-sem-mouse-cortex-axon-seg` ("Fix the labels"): the label stack was read correctly and on the
raw's grid, but 99.83% of it is 0, 96 of its 180 planes hold nothing, and the 23 ids lie in four places. A z run
alone (`tiff-zip-member-2`) leaves about 89% of the crop positions on planes 0-83 without an annotation. Naming the
boxes in the binding, the same box for a raw and its label and one pair of entries per box, keeps the two voxel
for voxel on one grid and leaves the choice visible and reviewable.

A box is a fixed part of the file that the binding declares, as the z run is; the snippet still does not sample
or choose anything, and the harness draws every crop inside the array it is given (SPEC §2, pure readers). A
reviewer who reads that rule as forbidding a y/x box should say so on the PR: the alternative for a sparsely
annotated volume is a harness that places every crop on an annotation.

- **Access.** A stored zip member or a plain file on a server that honours Range is opened with `io.open`
  and read page by page (`access: plane`): only the TIFF header, the IFDs tifffile needs and the requested
  pages are fetched, so planes outside the box are never read. A deflated member (or a server without Range /
  Content-Length) raises `NeedsFetch`; the snippet then stages the whole file with `io.fetch` (`access: file`).
- **Rows of a page.** Of a page stored as it is (uncompressed whole-byte samples, one per pixel, no predictor,
  its strips in file order with no gap, which is how ImageJ writes a stack) only the rows a read covers are
  fetched, as one byte range, and the columns are cut from them. Any other page (LZW, deflate, tiled, RGB) is
  decoded whole by tifffile and then cut, as in `tiff-zip-member`. The values are the same either way. It
  matters on a slow or unsteady host: a plane of the example's box 302 rows high is 3 MiB of range requests
  where the whole 1635-row float32 page is 13 MiB, and every request is one more chance of a dropped
  connection.
- **Axes.** tifffile's `series[0].axes`, mapped to `czyx`: `Z`/`Q`/`I` → `z`, `C`/`S` → `c`, `Y`, `X`.
  Size-1 axes other than y/x are dropped. The native order is kept (an RGB `YXS` file is `yxc`). Any other
  axis (`T`, …) or two letters mapping to the same axis fails with a message asking for `params.axes`. With
  `channel` the `c` is then removed.
- **Voxel size.** OME `PhysicalSize{X,Y,Z}` (with units), or ImageJ `unit` + `X/YResolution` + `spacing`.
  Otherwise `None` (plain TIFF resolution tags describe print size, not voxels). A box does not change it.

## Params

- `axes` (optional): one `czyx` letter per axis of tifffile's series after the size-1 non-y/x axes are
  dropped, in tifffile's order, including a `c` that `channel` collapses. Use it only when the file's own axes
  are wrong (e.g. a z-stack saved as `TYX`), and give the reason in the binding's `reasons`. The box params
  apply to the axes as named after the override.
- `channel` (optional): 0-based index on the channel axis (tifffile `C`, or `S` for RGB samples). Fails at open
  when the file has no channel axis or the index is out of range. In a member of a multi-file OME-TIFF (one
  file per channel, each declaring the whole set) it is the index of the channel that file holds.
- `z_first`, `z_last`, `y_first`, `y_last`, `x_first`, `x_last` (optional): the box of file indices that forms
  the array. Fail at open when a z end is set on a file with no z axis, or `0 <= first <= last < size` does not
  hold on an axis. **Give a raw and its label the same values**: the harness compares their shapes, not their
  offsets, so boxes of equal size at different places would pair the wrong voxels without an error. Choose the
  box from the data and say how in `reasons`. Keep it at least one crop (256 px) wide in y and x; a narrower
  array is cropped whole at its own size. A crop's `plane.z` and `box.y`/`box.x` in `crop.json` count from the
  box's start: file index = the crop's index + the binding's `*_first`.

## Known limits

- The box does not make a sparse segmentation dense: structures inside it that the annotators did not trace
  stay unlabelled, and a crop inside it can still hold no annotation (see the example's numbers).
- One box per binding entry, measured on that file. Several annotated places are several raw/label pairs of
  entries on the same URLs; `io`'s cache holds a file's bytes after the first. Boxes may overlap, and the
  harness's overlap rule is per raw entry, so two crops from overlapping boxes can show the same voxels.
- A compressed, tiled or multi-sample page is decoded whole, even for a small box or region. Of an
  uncompressed page the rows are cut in the file but not the columns: a read fetches full-width rows.
- Only `series[0]`; other series and pyramid levels are ignored.
- A generic multi-page TIFF (not ImageJ/OME/shaped) makes tifffile read every IFD at open; over HTTP that
  can cost one 1 MiB block per IFD when IFDs are spread through the file.
- `voxel_size_nm` units other than nm/µm/mm are treated as unknown.
- `tiff-zip-member-2` also supersedes `tiff-zip-member`, and its line ends at `tiff-zip-member-4`, which stays
  in the catalog beside this snippet. This one reads everything `tiff-zip-member-2` and `tiff-zip-member-3`
  (`y_first`, `y_last`) read, but not the one-IFD ImageJ stack over 4 GiB that `tiff-zip-member-4` reads.
- An empty selection (e.g. `z: slice(2, 2)`) is not supported, as in `tiff-zip-member`.

## Example

`examples/empiar-12244-cryo-sem-mouse-cortex-axon-seg.yaml`: EMPIAR-12244, a cryo FIB-SEM volume of mouse
cortex. Raw `brain_for_seg.tif` (ImageJ stack, zyx 180x1635x1920 float32, 6.34 x 6.34 x 50 nm, 2.26 GB,
uncompressed) and label `brain_for_seg_b_bck.labels.tif` (plain palette TIFF, 180 pages of 1635x1920 uint8,
LZW, 1.3 MB; its Amira `BoundingBox` description spans the same 12166.5 x 10359.6 x 8950 nm), both at plain
https URLs. Measured on the label file:

- 564,091,469 of 565,056,000 voxels are 0. Planes 84-179 are empty; planes 0-83 hold 1,219 to 21,229 labelled
  pixels each. Ids 1-23, 1,507 to 296,913 voxels each.
- The label is on the raw's grid as stored. On planes 0, 20, 35, 60, 70 and 83 the label outlines lie on
  intensity edges of the raw (7.8 to 23.5 standard deviations above the same outlines moved to random
  offsets); flipped in y or x, or reversed in z, they do not (under 2, one plane 4.1), and one plane off is
  already worse.
- Each id sits in one compact place. Grouping ids whose bounding boxes overlap in z, y and x gives four
  groups, and each becomes a box: the group's first to last annotated plane, and its y/x bounding box widened
  by 128 px (half a crop) and cut at the image edge.

| ids | z | y | x | array (zyx) | 256x256 positions holding an annotation |
|---|---|---|---|---|---|
| 1-3 | 0-28 | 1164-1634 | 595-1015 | 29x471x421 | 93% |
| 4-6 | 5-65 | 0-301 | 395-891 | 61x302x497 | 88% |
| 7-13 | 0-43 | 0-531 | 934-1546 | 44x532x613 | 94% |
| 14-23 | 40-83 | 742-1508 | 717-1448 | 44x767x732 | 64% |

Over the whole volume 5.1% of the 256x256 positions hold an annotation, and 11.2% on planes 0-83. Every plane
of every box holds one, each box holds only its own group's ids, and no two boxes share a voxel. The label is bound as
`segmentation_type: instance`: an id is one traced object (the record leaves the encoding unknown and has no
class map). The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't
name the commit that adds it); a binding written by the worker names a commit that contains the snippet.
