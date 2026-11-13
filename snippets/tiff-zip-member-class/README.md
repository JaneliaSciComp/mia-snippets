# tiff-zip-member-class

Reads one integer class-id TIFF (multi-page or single page) with `tifffile`, from a zip member
(`<zip url>::<member>`) or a plain URL, and returns the mask of one class id. Exactly one URL per array.
Access, axes mapping and voxel size are as in `tiff-zip-member`; what this snippet adds is the class selection.

Why it exists: `tiff-zip-member` returns a semantic class-id map whole. When a dataset's labels are named one
per class and each is bound with `segmentation_type: binary`, the harness encodes `a != 0`, so every per-class
key would hold the union of all classes, silently. With this snippet the same file is bound once per class and
each binding returns that class only.

- **Selection** (`params.class_id`, required): an integer id. `read` returns `uint8` `1` where the file equals
  it and `0` elsewhere; `dtype` is `uint8` whatever the file's integer dtype. The file must be an integer
  type (a float file fails at open), and an id the file's dtype cannot hold fails at open. An id that the
  dtype can hold but that never occurs gives an all-zero mask: the file does not list its classes, so the ids
  and their meaning come from the record, with a reason in the binding. `class_id: 0` is allowed and gives
  the mask of the background id.
- **Axes.** tifffile's `series[0].axes` mapped to `czyx` (`Z`/`Q`/`I` → `z`, `Y`, `X`), size-1 axes other
  than y/x dropped. A file whose mapped axes include `c` (tifffile `C` or `S`) fails at open: a class-id map
  has no channel axis, and one mask per channel is `tiff-zip-member-channel`'s case. `axes` (optional)
  overrides the mapping as in `tiff-zip-member`; give a reason in the binding.
- **Access.** A stored zip member or a Range-capable plain URL is opened with `io.open` and read page by page
  (`access: plane`). A deflated member (or a server without Range / Content-Length) raises `NeedsFetch`; the
  snippet then stages the whole file with `io.fetch` (`access: file`). `open_array` reads headers only;
  `read` decodes the pages the region needs, whole, then compares.
- **Voxel size.** OME `PhysicalSize{X,Y,Z}` (with units), or ImageJ `unit` + `X/YResolution` + `spacing`.
  Otherwise `None` (plain TIFF resolution tags describe print size, not voxels).

## Known limits

- One file per array: a z stack stored as one TIFF per section is bound one section at a time (no glob, no
  stacking of several URLs).
- One class id per binding entry; there is no set of ids and no remapping. Binding the same file once per
  class opens and decodes it once per label (`io`'s cache holds the bytes after the first).
- Only `series[0]`; other series and pyramid levels are ignored.
- A generic multi-page TIFF (not ImageJ/OME/shaped) makes tifffile read every IFD at open; over HTTP that
  can cost one 1 MiB block per IFD when IFDs are spread through the file.
- Each page is decoded whole, even for a small region (tiled TIFFs included).
- `voxel_size_nm` units other than nm/µm/mm are treated as unknown.

## Example

`examples/empiar-11746-u2os-fib-sem-organelles.yaml`: EMPIAR-11746, a FIB-SEM volume of a U2-OS cell stored
as one TIFF per section, `Images/U2OS_cell_<nnnn>.tif` (yx 1385x3394 uint8, read with `tiff-zip-member`) and
`Labels/Labels_U2OS_cell_<nnnn>.tif` (same grid, uint8 LZW, class ids 0-8), as plain https URLs. Section 0600's
label file is bound six times, with `class_id` 6, 5, 2, 4, 1 and 8, as the binary labels endoplasmic
reticulum, Golgi, lipid droplets, mitochondria, nuclear envelope and other (ids from the record's classes).
Ids 3 (lysosomes) and 7 (peroxisomes) have no label name in the dataset's downloads and are not bound. Only
the record's sampled section is bound: the record does not give the first file number of the 1168 sections.
The files carry no voxel size (plain TIFF), so `voxel_size_nm` is `None`. The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
