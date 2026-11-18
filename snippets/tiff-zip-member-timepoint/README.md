# tiff-zip-member-timepoint

Reads one TIFF (multi-page or single page) with `tifffile`, from a zip member (`<zip url>::<member>`) or a
plain URL, **at one timepoint**. Exactly one URL per array. An `Array` has no time axis, so a time-lapse file
is presented with `params.t` fixed on its time axis: tifffile `TCYX` (61, 4, 2720, 2720) opens as `cyx`
(4, 2720, 2720), `TZCYX` as `zcyx`, `TYX` as `yx`.

Why it exists: `tiff-zip-member-2` maps tifffile's axes to `czyx` and refuses a `T` axis. Its `axes` override
can only relabel the frames as z planes, which is wrong for a 2D time-lapse (the harness would report a z
extent and plane index the file does not have). `ome-zarr2-http-timepoint` already presents one timepoint
per bound array for Zarr; this snippet does the same for TIFF. Apart from the time axis it reads as
`tiff-zip-member-2` does (same axis mapping, voxel size and access), without that snippet's z run
(`z_first`/`z_last`).

- **Access.** A stored zip member or a plain file on a server that honours Range is opened with `io.open`
  and read page by page (`access: plane`): only the TIFF header, the IFDs tifffile needs and the requested
  pages are fetched, so the other timepoints are never read. A deflated member (or a server without Range /
  Content-Length) raises `NeedsFetch`; the snippet then stages the whole file with `io.fetch` (`access: file`),
  every timepoint included.
- **Axes.** tifffile's `series[0].axes`, mapped to `tczyx`: `T` → `t`, `Z`/`Q`/`I` → `z`, `C`/`S` → `c`, `Y`,
  `X`. Size-1 axes other than y/x are dropped. The native order of the remaining axes is kept. Any other axis
  or two letters mapping to the same axis fails with a message asking for `params.axes`. The `t` axis is then
  removed (fixed at `params.t`), and so is `c` when `channel` is set.
- **Time axis.** Not presented; the frame interval (ImageJ `finterval`, OME `TimeIncrement`) is not read.
- **Voxel size.** OME `PhysicalSize{X,Y,Z}` (with units), or ImageJ `unit` + `X/YResolution` + `spacing`.
  Otherwise `None` (plain TIFF resolution tags describe print size, not voxels).
- **dtype.** The file's sample type in native byte order; a big-endian file's values are returned byte-swapped
  to it, unchanged in value.

## Params

- `t` (optional int): 0-based index on the time axis.
  - Time axis longer than 1: **required**. Without it open raises, naming the valid range, rather than pick a
    timepoint silently. Out of range raises.
  - No time axis longer than 1 (a file without `T`, or with one frame, whose size-1 axis is dropped): must be
    left out; the file is then read as `tiff-zip-member-2` reads it.

  A label gets the same `t` as the raw it pairs with; the snippet opens one array and cannot check that.
  Give the reason for the chosen timepoint in the binding.
- `axes` (optional): one `tczyx` letter per axis of tifffile's series after the size-1 non-y/x axes are
  dropped, in tifffile's order, including the `t` that `t` fixes and a `c` that `channel` collapses. Use it
  only when the file's own axes are wrong (e.g. a time-lapse saved as a plain multi-page TIFF, which tifffile
  reports as `IYX` and this snippet would read as `zyx`: `axes: tyx`), and give the reason in the binding's
  `reasons`.
- `channel` (optional int): 0-based index on the channel axis (tifffile `C`, or `S` for RGB samples) of the
  one channel to present. Fails at open when the file has no channel axis or the index is out of range.

## Known limits

- **One timepoint per bound array.** To crop several timepoints of a time-lapse, list the file several times
  in the binding's `raw` (and its label alongside), each with its own `t`, as the example does. A deflated
  member is downloaded once per job, but each bound array opens it again, and the harness re-checks the
  staged file's CRC on each open (a few seconds per GB).
- **Raw and label timepoints are matched by index only.**
- Only `series[0]`; other series and pyramid levels are ignored.
- A generic multi-page TIFF (not ImageJ/OME/shaped) makes tifffile read every IFD at open; over HTTP that
  can cost one 1 MiB block per IFD when IFDs are spread through the file.
- An ImageJ hyperstack over 4 GB (ImageJ writes one IFD and appends the rest of the pixels) has not been
  tested.
- Each page is decoded whole, even for a small region (tiled TIFFs included).
- `voxel_size_nm` units other than nm/µm/mm are treated as unknown.
- No z run (`z_first`/`z_last` of `tiff-zip-member-2`) and no one-hot collapse of channels to class ids
  (`tiff-zip-member-channel`).
- An empty selection (e.g. `c: slice(2, 2)`) is not supported, as in `tiff-zip-member`.

## Example

`examples/zenodo-8235843-paviaatn-hacat-4ch.yaml`: PaviaATN, a two-hour widefield time-lapse of HaCaT cells,
one big-endian ImageJ hyperstack `PaviaATN/OptiMEM100x014.tif` (3,610,548,855 bytes, uncompressed pages)
deflated inside `PaviaATN.zip` on zenodo.org. tifffile reads it as `TCYX` (61, 4, 2720, 2720) uint16; the
binding lists the member three times, with `t: 0`, `t: 30` and `t: 60` (first, middle and last frame, 120 s
per frame), each presented as `cyx` 4x2720x2720 uint16 with voxel size y 65, x 65 nm from the ImageJ
metadata. Channels in file order are nuclei, nuclei, actin, tubulin (the dataset's README). No labels. The
member is deflated, so the whole member is fetched once (2.3 GB over the network) and `access` is `file`.

Tested on that data with `harness smoke` (ok, three raw shapes [4, 2720, 2720], no warnings,
2,309,941,493 bytes fetched, the member once) and `harness crop --n 4` followed by `harness verify` (4 of 4
crops, none rejected; actin fibres visible in a channel-2 crop). Verify passed with a licence added to the
record copy: the record block in the issue carries no `license`, and without one its only error is "crop.json
has no licence". The four crops of a batch come from one of the three bound frames, since the harness makes
8 draws in a volume whose access is `file` before it moves on. The whole frames read through the
snippet at `t` 0, 17, 30 and 60 equal the file's raw bytes at pages `4 t .. 4 t + 3` (big-endian uint16 at the
first page's data offset), and without `t` open raises "has 61 timepoints … set params.t (0..60…)". Between
frame 0 and frame 1 the channels correlate 0.85 to 0.90 pixel for pixel, between frame 0 and frame 60 at most
0.23, which is why the binding spreads its three timepoints over the recording.

Checked against files written by tifffile over a local range server: every `t` of a big-endian ImageJ `TCYX`
hyperstack in a deflated zip against the source data (whole frames, patches, stepped and reversed slices), a
stored member read by range (`access: plane`, under 4 MiB fetched of a 6 MiB file for one page), a `TZCYX`
hyperstack as `zcyx` with z spacing, `t` with `channel`, an OME-TIFF `TCYX`, a plain multi-page file with
`axes: tyx`, the errors for a missing, out-of-range or non-integer `t` and for `t` on a file without a time
axis, and that such a file opens and reads exactly as through `tiff-zip-member-2`.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
