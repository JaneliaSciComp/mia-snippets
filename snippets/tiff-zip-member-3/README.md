# tiff-zip-member-3

Reads one TIFF (multi-page or single page) with `tifffile`, from a zip member (`<zip url>::<member>`) or a
plain URL. Exactly one URL per array. Supersedes `tiff-zip-member-2`: with no params, or only `axes`,
`channel`, `z_first` and `z_last`, it presents the same array (axes, shape, dtype, voxel size, access, values).
It adds one thing a binding may ask for:

- **A run of rows** (`y_first`, `y_last`). The array is the page's rows `y_first..y_last` (0-based, both
  included), in file order; its row 0 is the page's row `y_first`. Either end may be left out and then is the
  page's own.

Why it exists: `tiff-zip-member-2` presents every row of the page. Acquisition software often saves a
micrograph with a **data bar** in the same page, below the image: a table of instrument settings (operator,
date, magnification, voltage) or a scale bar on a flat band. Those rows are in the file but they are not
specimen, and a crop that reaches them shows a flat band with a sharp edge, or text. That is what a reviewer
doubted for `gnode-s4bsnu-synaptic-vesicles-tem` (a uniform black band along the bottom of a crop): 78 of its
87 TEM images are a 2048-row micrograph followed by 39, 45 or 67 rows of data bar, and the OME-XML of the 53
OME-TIFF ones declares the image as 2048x2048 inside a 2087- or 2093-row page. Naming the rows in the binding
keeps the choice visible and reviewable, as `z_first`/`z_last` do for planes. Reading a run of rows is not a
crop in the harness's sense: the array is the image the page holds, and the harness still draws every crop.

- **Access.** A stored zip member or a plain file on a server that honours Range is opened with `io.open`
  and read page by page (`access: plane`): only the TIFF header, the IFDs tifffile needs and the requested
  pages are fetched. A deflated member (or a server without Range / Content-Length) raises `NeedsFetch`; the
  snippet then stages the whole file with `io.fetch` (`access: file`).
- **Axes.** tifffile's `series[0].axes`, mapped to `czyx`: `Z`/`Q`/`I` → `z`, `C`/`S` → `c`, `Y`, `X`.
  Size-1 axes other than y/x are dropped. The native order is kept (an RGB `YXS` file is `yxc`). Any other
  axis (`T`, …) or two letters mapping to the same axis fails with a message asking for `params.axes`. With
  `channel` the `c` is then removed.
- **Voxel size.** OME `PhysicalSize{X,Y,Z}` (with units), or ImageJ `unit` + `X/YResolution` + `spacing`.
  Otherwise `None` (plain TIFF resolution tags describe print size, not voxels). A run of planes or rows does
  not change it.
- **OME size against page size.** When an OME-TIFF's page is larger than the size its OME-XML declares,
  tifffile presents the page ("OME series cannot handle discontiguous storage"), and so does this snippet
  unless the binding gives a row run. The snippet does not apply the OME size by itself: the same data bar
  occurs in plain and ImageJ TIFFs that declare nothing, so the rows are always the binding's statement.

## Params

- `axes` (optional): one `czyx` letter per axis of tifffile's series after the size-1 non-y/x axes are
  dropped, in tifffile's order, including a `c` that `channel` collapses. Use it only when the file's own axes
  are wrong (e.g. a z-stack saved as `TYX`), and give the reason in the binding's `reasons`.
- `channel` (optional): 0-based index on the channel axis (tifffile `C`, or `S` for RGB samples). Fails at open
  when the file has no channel axis or the index is out of range. In a member of a multi-file OME-TIFF (one
  file per channel, each declaring the whole set) it is the index of the channel that file holds; the other
  channels' pages are in sibling files and are not read.
- `z_first`, `z_last` (optional): the run of file planes that forms the array. Fail at open when the file has
  no z axis or `0 <= z_first <= z_last < planes` does not hold. **Give a raw and its label the same values**:
  the harness compares their shapes, not their offsets, so runs of equal length but different start would pair
  the wrong planes without an error. A crop's `plane.z` in `crop.json` counts from the run's start: file plane
  = `plane.z + z_first`.
- `y_first`, `y_last` (optional): the run of page rows that forms the array. Fail at open when `0 <= y_first
  <= y_last < rows` does not hold. Choose the run from the data and say how in `reasons`: the OME-XML `SizeY`
  when the file has one, and in any case a look at the rows on both sides of the cut (the example's data bars
  all start with a row of constant 0 directly below the last specimen row). A label on the raw's grid takes the
  same values. A crop's `box.y` in `crop.json` counts from the run's start: page row = `box.y + y_first`.

## Known limits

- Only `series[0]`; other series and pyramid levels are ignored.
- Only rows. A data bar beside the image (a run of columns) is not handled.
- An annotation burned into the image itself stays. In the example's 53 OME-TIFF files the acquisition
  software also drew a small scale-bar box inside the 2048x2048 image (bottom right, about 23x116 px in 49
  files and 43x166 px in 4); it is within the image the OME-XML declares and is presented as stored.
- The row run is one per binding entry and is not found by the snippet (that would mean reading pixels at
  open). Files with bars of different heights need their own values.
- A generic multi-page TIFF (not ImageJ/OME/shaped) makes tifffile read every IFD at open; over HTTP that
  can cost one 1 MiB block per IFD when IFDs are spread through the file.
- Each page is decoded whole, even for a small region (tiled TIFFs included); rows outside the run are fetched
  with their page and dropped.
- `voxel_size_nm` units other than nm/µm/mm are treated as unknown (so an ImageJ file calibrated in `cm`
  reports `None`; in the example those files state 10 pixels per cm, a placeholder).
- No one-hot collapse of channels to class ids: `tiff-zip-member-channel` (`channels: onehot`) still does that.

## Example

`examples/gnode-s4bsnu-synaptic-vesicles-tem.yaml`: 87 single-page uint8 TEM images of presynaptic terminals,
stored members of the G-Node DOI zip, one raw entry each. All 87 pages were read whole to choose the rows:

| files | page (rows x cols) | kind | rows 2048 and below | binding |
|---|---|---|---|---|
| 49 | 2087 x 2048 | OME-TIFF (ImageSP 1.2.8.19), OME size 2048x2048 | row 2048 constant 0, then a white settings table and scale bar (38 rows) | `y_last: 2047` |
| 4 | 2093 x 2048 | OME-TIFF (ImageSP 1.2.9.70), OME size 2048x2048 | the same, 44 rows | `y_last: 2047` |
| 25 | 2115 x 2048 | 23 ImageJ, 2 plain TIFF | rows 2048-2062 constant 0, a white scale bar and its label on black in rows 2063-2100, rows 2101-2114 constant 0 | `y_last: 2047` |
| 9 | 1336 x 2004 | ImageJ | no such rows: no constant row anywhere, specimen to the last row | no params |

In each of the 78 files row 2047 is specimen (not constant) and row 2048 is the first constant row of the
page. With the binding, 78 arrays are yx 2048x2048 and 9 are yx 1336x2004; every array equals the page's rows
`0..y_last` byte for byte. The example binding's `snippets_commit` is the commit before this snippet's own (a
binding can't name the commit that adds it); a binding written by the worker names a commit that contains the
snippet.
