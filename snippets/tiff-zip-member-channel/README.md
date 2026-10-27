# tiff-zip-member-channel

Reads one TIFF (multi-page or single page) with `tifffile`, from a zip member (`<zip url>::<member>`) or a
plain URL, and collapses its channel axis to one plane. Exactly one URL per array. Everything else (access,
axes mapping, voxel size) is as in `tiff-zip-member`; what this snippet adds is the `c` collapse.

Why it exists: `tiff-zip-member` exposes a multi-channel file as `c…yx`, and the harness reads `c=0` of a
channelled label paired with a channel-less raw. An ImageJ hyperstack that stores one binary mask per channel
(one class per channel) therefore reaches the crops with its first class only. With this snippet each channel is
bound as its own `yx` label, or, when the channels never overlap, the stack becomes one class map.

- **Collapse** (`params`, exactly one of the two):
  - `channel: <k>`: the 0-based index on the file's channel axis (tifffile `C`, or `S` for RGB samples) of the
    one plane to return. dtype is the file's. Out of range fails at open.
  - `channels: onehot`: each pixel is non-zero in exactly one channel. Returns `uint8` class ids `1..C` in
    channel order (argmax + 1), `0` where no channel is set. A pixel set in several channels raises at `read`
    (the harness then drops that crop and records the error), so check the overlap before choosing it; the
    class names behind the ids come from the record, not the file.
- **Axes.** tifffile's `series[0].axes` mapped to `czyx` (`Z`/`Q`/`I` → `z`, `C`/`S` → `c`, `Y`, `X`), size-1
  axes other than y/x dropped, then `c` removed: a `CYX` hyperstack is `yx`, a `ZCYX` stack is `zyx`, an RGB
  `YXS` file is `yx`. A file whose mapped axes have no `c` fails at open with a pointer to `tiff-zip-member`.
  `axes` (optional) overrides the mapping the same way as in `tiff-zip-member`; give one letter per kept axis,
  including the `c` that is collapsed, and a reason in the binding.
- **Access.** A stored zip member or a Range-capable plain URL is opened with `io.open` and read page by page
  (`access: plane`). A deflated member (or a server without Range / Content-Length) raises `NeedsFetch`; the
  snippet then stages the whole file with `io.fetch` (`access: file`). `open_array` reads headers only;
  `read` decodes the pages the region needs, whole.
- **Voxel size.** OME `PhysicalSize{X,Y,Z}` (with units), or ImageJ `unit` + `X/YResolution` + `spacing`.
  Otherwise `None` (plain TIFF resolution tags describe print size, not voxels).

## Known limits

- Only `series[0]`; other series and pyramid levels are ignored.
- A generic multi-page TIFF (not ImageJ/OME/shaped) makes tifffile read every IFD at open; over HTTP that
  can cost one 1 MiB block per IFD when IFDs are spread through the file.
- Each page is decoded whole, even for a small region (tiled TIFFs included). Binding the same file once per
  channel, as the example does, opens it once per label; a deflated member is staged once and reused from
  `io`'s cache.
- `onehot` tests `!= 0`; a soft (probability) mask is not one-hot and raises when several channels are set.
- `voxel_size_nm` units other than nm/µm/mm are treated as unknown.

## Example

`examples/zenodo-15608267-factin-conformations-sted-neurons.yaml`: STED images of F-actin in hippocampal
neurons, 79 pairs of `<uuid>.tif` (yx 512x512 uint16, read with `tiff-zip-member`) and `<uuid>_mask.tif`
(ImageJ hyperstack cyx 3x512x512 uint16, each channel a {0,1} mask) in one 9.3 MB zip of deflated members.
The mask is bound three times with `channel: 0`, `1`, `2` as the binary labels nanoclusters, compact
assemblies and protrusions (the record's class order; the files do not name their channels). `channels:
onehot` was not used: 16 of the 79 masks have pixels set in more than one channel. The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
