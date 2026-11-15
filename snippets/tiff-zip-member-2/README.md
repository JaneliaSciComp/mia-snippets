# tiff-zip-member-2

Reads one TIFF (multi-page or single page) with `tifffile`, from a zip member (`<zip url>::<member>`) or a
plain URL. Exactly one URL per array. Supersedes `tiff-zip-member`: with no params, or only `axes`, it presents
the same array (axes, shape, dtype, voxel size, access, values). It adds two things a binding may ask for:

- **A run of z planes** (`z_first`, `z_last`). The array is the file's planes `z_first..z_last` (0-based, both
  included), in file order; its plane 0 is the file's plane `z_first`. Either end may be left out and then is
  the file's own.
- **One channel** (`channel`). The file's channel axis is collapsed to the one channel with that index, so a
  `czyx` file is presented as `zyx`.

Why it exists: `tiff-zip-member` presents every plane of a z-stack. A confocal stack acquired well above and
below the specimen is mostly out-of-focus planes, and a 3D segmentation of it is empty there. The harness draws
planes uniformly, so most crops of such a pair are blur with an empty label, and the crops aimed at an
annotation land on the few stray voxels of a nearly empty plane as often as on the structure. That is what a
reviewer rejected for `allen-hipsc-cardiomyocyte-actn2-fish-structure-3d` ("Fix the labels"): the label was
read correctly and on the raw's grid, but 26 of its 50 planes hold no annotation. Naming the planes in the
binding, the same run for the raw and its label, keeps the two plane for plane on one grid and leaves the
choice visible and reviewable. `channel` is here because the raw of that record needs both: it is one member of
a multi-file OME-TIFF, and `tiff-zip-member-channel` has no z run.

- **Access.** A stored zip member or a plain file on a server that honours Range is opened with `io.open`
  and read page by page (`access: plane`): only the TIFF header, the IFDs tifffile needs and the requested
  pages are fetched, so planes outside the run are never read. A deflated member (or a server without Range /
  Content-Length) raises `NeedsFetch`; the snippet then stages the whole file with `io.fetch` (`access: file`).
- **Axes.** tifffile's `series[0].axes`, mapped to `czyx`: `Z`/`Q`/`I` → `z`, `C`/`S` → `c`, `Y`, `X`.
  Size-1 axes other than y/x are dropped. The native order is kept (an RGB `YXS` file is `yxc`). Any other
  axis (`T`, …) or two letters mapping to the same axis fails with a message asking for `params.axes`. With
  `channel` the `c` is then removed.
- **Voxel size.** OME `PhysicalSize{X,Y,Z}` (with units), or ImageJ `unit` + `X/YResolution` + `spacing`.
  Otherwise `None` (plain TIFF resolution tags describe print size, not voxels). A z run does not change it.

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
  the wrong planes without an error. Choose the run from the data and say how in `reasons` (the example keeps
  the planes that each hold at least 1% of the segmentation's foreground). A crop's `plane.z` in `crop.json`
  counts from the run's start: file plane = `plane.z + z_first`, with `z_first` from the binding.

## Known limits

- Only `series[0]`; other series and pyramid levels are ignored.
- A generic multi-page TIFF (not ImageJ/OME/shaped) makes tifffile read every IFD at open; over HTTP that
  can cost one 1 MiB block per IFD when IFDs are spread through the file (about 52 MB for the example's
  308 MB raw).
- Each page is decoded whole, even for a small region (tiled TIFFs included).
- `voxel_size_nm` units other than nm/µm/mm are treated as unknown.
- The z run is one per binding entry, measured on that file. FOVs focused at different depths need their own
  values; the snippet does not find the in-focus planes itself (that would mean reading voxels at open).
- No one-hot collapse of channels to class ids: `tiff-zip-member-channel` (`channels: onehot`) still does that.
- An empty selection (e.g. `z: slice(2, 2)`) is not supported, as in `tiff-zip-member`.

## Example

`examples/allen-hipsc-cardiomyocyte-actn2-fish-structure-3d.yaml`: one FISH field of hiPSC-derived
cardiomyocytes. Raw: `<fov>_C5.tif`, the ACTN2-mEGFP channel of a six-file OME-TIFF set (OME-XML says czyx
6x50x1736x1776 uint16; this file holds channel 5's 50 pages), bound with `channel: 5`. Label: the FOV's 3D
ACTN2 structure segmentation (zyx 50x1736x1776 uint8, values 0/255, deflate). Both are bound with `z_first:
13`, `z_last: 23`, so each is zyx 11x1736x1776. Per-plane foreground of the segmentation is 0 on z 0-10 and
35-49, 331 voxels on z 11, 381,248 at its peak on z 15 and 25 on z 34; planes 13-23 each hold at least 1% of
the 2,217,240 foreground voxels and together 96.3% of them. The example binding's `snippets_commit` is the
commit before this snippet's own (a binding can't name the commit that adds it); a binding written by the
worker names a commit that contains the snippet.
