# tiff-zip-member-4

Reads one TIFF (multi-page or single page) with `tifffile`, from a zip member (`<zip url>::<member>`) or a
plain URL. Exactly one URL per array. Supersedes `tiff-zip-member-3`: it takes the same params (`axes`,
`channel`, `z_first`/`z_last`, `y_first`/`y_last`) and presents the same array (axes, shape, dtype, voxel size,
access, values) for every file that snippet reads. It adds one kind of file:

- **A stack with one IFD for all its planes.** A classic TIFF holds 32-bit offsets, so it cannot point at data
  past 4 GiB. ImageJ saves a larger stack anyway: it writes one IFD, for plane 0, and the other planes directly
  after plane 0's data, uncompressed. How many there are is only in the ImageJ description (`images=`, with
  `channels=`, `slices=`). `tifffile` knows the layout and presents series 0 with the full shape, but the file
  has a single page. This snippet reads plane `k` of such a series as the `plane bytes` at `page 0's data
  offset + k * plane bytes`, where `k` counts the series' outer axes in tifffile's order (`k = z * channels + c`
  for a `ZCYX` hyperstack).

Why it exists: `tiff-zip-member-3` (like `-2`, `-channel` and `-timepoint`) reads plane `k` with
`asarray(key=k, series=0)`, page `k` of the file. On a one-IFD stack that opens with the right shape, reads
plane 0, and fails with `IndexError: list index out of range` on every other plane; smoke, which reads plane 0
of the first raw, passes. That is `figshare-27247629-plant-bf-virtual-staining`: its largest file is
4,857,006,691 B with one IFD and 579 planes behind it.

- **When the offset path is taken.** tifffile marks series 0 as truncated (`TiffPageSeries.is_truncated`): the
  file has one page, stored in final form (uncompressed, 8/16/32/64-bit samples, one contiguous run, no
  predictor, fill order 1), and its ImageJ description (or tifffile's own shape metadata, for a file written
  with `truncate=True`) states more planes than that page. The snippet checks again that the page is in final
  form and that `offset + planes * plane bytes` does not pass the end of the file, and fails the open with a
  `ValueError` otherwise. Every other file takes `tiff-zip-member-3`'s path, page by page, unchanged.
- **Only the rows asked for are read.** A plane read by offset fetches the rows from the first to the last of
  the region's `y` selection (the page's first axis), not the whole plane: `io` rounds that to 1 MiB blocks, so
  a 256-row patch of a 2048x2048 uint16 plane costs about 2 MiB instead of 8 MiB. Columns are cut after the read.
  Samples are returned in native byte order, as tifffile returns pages.
- **Access.** A stored zip member or a plain file on a server that honours Range is opened with `io.open`
  (`access: plane`): the TIFF header, the IFDs tifffile needs and the requested planes are fetched. A deflated
  member (or a server without Range / Content-Length) raises `NeedsFetch`; the snippet then stages the whole
  file with `io.fetch` (`access: file`), and the offset path reads the staged file the same way.
- **Axes.** tifffile's `series[0].axes`, mapped to `czyx`: `Z`/`Q`/`I` → `z`, `C`/`S` → `c`, `Y`, `X`.
  Size-1 axes other than y/x are dropped. The native order is kept (an RGB `YXS` file is `yxc`). Any other
  axis (`T`, …) or two letters mapping to the same axis fails with a message asking for `params.axes`. With
  `channel` the `c` is then removed.
- **Voxel size.** OME `PhysicalSize{X,Y,Z}` (with units), or ImageJ `unit` + `X/YResolution` + `spacing`.
  Otherwise `None` (plain TIFF resolution tags describe print size, not voxels). A run of planes or rows does
  not change it.

## Params

All as in `tiff-zip-member-3`; all work on a one-IFD stack too.

- `axes` (optional): one `czyx` letter per axis of tifffile's series after the size-1 non-y/x axes are
  dropped, in tifffile's order, including a `c` that `channel` collapses. Use it only when the file's own axes
  are wrong (e.g. a z-stack saved as `TYX`), and give the reason in the binding's `reasons`.
- `channel` (optional): 0-based index on the channel axis (tifffile `C`, or `S` for RGB samples). Fails at open
  when the file has no channel axis or the index is out of range.
- `z_first`, `z_last` (optional): the run of file planes that forms the array. Fail at open when the file has
  no z axis or `0 <= z_first <= z_last < planes` does not hold. **Give a raw and its label the same values**:
  the harness compares their shapes, not their offsets. A crop's `plane.z` in `crop.json` counts from the
  run's start: file plane = `plane.z + z_first`.
- `y_first`, `y_last` (optional): the run of page rows that forms the array (a data bar above or below the
  image is left out). Fail at open when `0 <= y_first <= y_last < rows` does not hold. A crop's `box.y` counts
  from the run's start: page row = `box.y + y_first`.

## Known limits

- Only `series[0]`; other series and pyramid levels are ignored.
- The offset path trusts the layout tifffile reports for the series (ImageJ's, or tifffile's own `truncate`
  files): nothing in the file marks where plane `k` starts, so a file whose description states the wrong
  channel or slice count is read with the wrong shape, as ImageJ itself would read it. The check at open is the
  file's length.
- A one-IFD ImageJ stack that is cut short (too few bytes for the images its description states) is not a
  truncated series either: tifffile logs an error, drops the ImageJ series and presents page 0 alone as a 2D
  image, and so does this snippet, as `tiff-zip-member-3` does. The array's shape then disagrees with the
  record's.
- A single compressed page whose ImageJ description states several images is not a truncated series: it opens
  with the stated shape and fails with an `IndexError` past plane 0, as in `tiff-zip-member-3` (finding out at
  open would mean walking every IFD of every multi-page file).
- Files read page by page keep `tiff-zip-member-3`'s limits: each page is decoded whole, even for a small
  region; a generic multi-page TIFF (not ImageJ/OME/shaped) makes tifffile read every IFD at open; only rows
  can be cut, not columns.
- `voxel_size_nm` units other than nm/µm/mm are treated as unknown (an ImageJ file calibrated in `cm` reports
  `None`; the example's BCECF file states 1 pixel per cm, an ImageJ default).
- No one-hot collapse of channels to class ids: `tiff-zip-member-channel` (`channels: onehot`) still does that.
- A deflated zip member is staged whole, however large; an ImageJ one-IFD stack is over 4 GB.

## Example

`examples/figshare-27247629-plant-bf-virtual-staining.yaml`: three ImageJ 1.53e hyperstacks from figshare,
plain URLs, each `zcyx` with 2048x2048 big-endian uint16 planes, uncompressed, no params. `z` indexes
independent frames (bright-field and fluorescence of one field are the channels of a frame).

| url | size (B) | IFDs | series | read |
|---|---|---|---|---|
| `files/49845744` (BY-HR PlasMemG) | 4,857,006,691 | 1 | 193 x 3 x 2048 x 2048 | by offset: plane `z*3 + c` at `2659 + k * 8,388,608` |
| `files/49845732` (BY-2 BCECF) | 2,936,071,271 | 350 | 175 x 2 x 2048 x 2048 | page by page |
| `files/49845963` (E. densa) | 1,996,520,438 | 238 | 119 x 2 x 2048 x 2048 | page by page |

For the first file `2659 + 579 * 8,388,608 = 4,857,006,691`, its exact size. Checked on it through this
snippet: planes at z = 0, 96, 100 and 192 (the last), every channel, read whole. Each has the channel pattern
the record describes (c0 and c1 fluorescence with a median near 110, c2 bright-field with a median of
12,500-14,700), adjacent rows correlate (0.81-0.99, which a read off by a byte or a row would not), and the
middle frame z = 96 has the record's own extremes, measured by its author with a different reader (min 27 in
c0, max 25,173 in c2). Plane 0 equals tifffile's page 0. `harness smoke` passes with no params and with
`z_first: 100, z_last: 100` on all three (the case `tiff-zip-member-3` fails with `IndexError`), and `harness
crop` draws crops from all three files.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
