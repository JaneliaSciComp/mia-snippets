# tiff-zip-member

Reads one TIFF (multi-page or single page) with `tifffile`, from a zip member (`<zip url>::<member>`) or a
plain URL. Exactly one URL per array.

- **Access.** A stored zip member or a plain file on a server that honours Range is opened with `io.open`
  and read page by page (`access: plane`): only the TIFF header, the IFDs tifffile needs and the requested
  pages are fetched. A deflated member (or a server without Range / Content-Length) raises `NeedsFetch`; the
  snippet then stages the whole file with `io.fetch` (`access: file`).
- **Axes.** tifffile's `series[0].axes`, mapped to `czyx`: `Z`/`Q`/`I` → `z`, `C`/`S` → `c`, `Y`, `X`.
  Size-1 axes other than y/x are dropped. The native order is kept (an RGB `YXS` file is `yxc`). Any other
  axis (`T`, …) or two letters mapping to the same axis fails with a message asking for `params.axes`.
- **Voxel size.** OME `PhysicalSize{X,Y,Z}` (with units), or ImageJ `unit` + `X/YResolution` + `spacing`.
  Otherwise `None` (plain TIFF resolution tags describe print size, not voxels).

## Params

- `axes` (optional): one `czyx` letter per axis of tifffile's series after the size-1 non-y/x axes are
  dropped, in tifffile's order. Use it only when the file's own axes are wrong (e.g. a z-stack saved as
  `TYX`), and give the reason in the binding's `reasons`.

## Known limits

- Only `series[0]`; other series and pyramid levels are ignored.
- A generic multi-page TIFF (not ImageJ/OME/shaped) makes tifffile read every IFD at open; over HTTP that
  can cost one 1 MiB block per IFD when IFDs are spread through the file.
- Each page is decoded whole, even for a small region (tiled TIFFs included).
- `voxel_size_nm` units other than nm/µm/mm are treated as unknown.

## Example

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
