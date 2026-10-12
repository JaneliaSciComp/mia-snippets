# hdf5-zip-member

Reads one dataset of one HDF5 file with `h5py`, from a zip member (`<zip url>::<member>`) or a plain URL
(https, public `s3://`). Exactly one URL per array; the dataset is chosen by the required `key` param, so a
file holding both `/raw` and `/label` is bound twice, once per key.

- **Access.** A stored zip member or a plain file on a server that honours Range is opened with `io.open`
  and handed to h5py's file-object driver: only the superblock, the object headers h5py walks and the
  requested hyperslab are fetched, through `io`'s 1 MiB block cache. The array reports `access: chunk` for
  a chunked dataset and `access: plane` for a contiguous one (a y/x region of one plane costs the rows it
  spans). A deflated member (or a server without Range / Content-Length) raises `NeedsFetch`; the snippet
  then stages the whole file with `io.fetch` (`access: file`) and opens the local path.
- **Axes.** By rank, in storage order: 2 → `yx`, 3 → `zyx`, 4 → `czyx`. Any other rank, or a file whose
  storage order differs, needs `params.axes`.
- **Voxel size.** The `element_size_um` attribute (ImageJ HDF5 plugin / ilastik convention: µm per spatial
  axis in `zyx` order), read from the dataset's attributes, else from the file root. Converted to nm. `None`
  when absent, when its length does not match the spatial axes, when a value is not positive, or when the
  binding sets `ignore_element_size_um`.
- **dtype and shape** are the dataset's own; nothing is cast.

## Params

- `key` (required): HDF5 path of the dataset, e.g. `raw` or `/label`.
- `axes` (optional): one `czyx` letter per dataset dimension, in storage order. Use it only when the
  default by rank is wrong (e.g. a `zcyx` file), and give the reason in the binding's `reasons`.
- `ignore_element_size_um` (optional, bool): `true` reports `voxel_size_nm: None` even though the file has
  the attribute. For files whose attribute is known to be wrong (e.g. a placeholder `[1, 1, 1]`), with the
  reason in the binding.

## Known limits

- One dataset per array; groups, soft/external links to other files and virtual datasets are not resolved
  beyond what h5py does by default (external links need a file h5py cannot reach through `io`).
- Only positive slice steps in `read`; a negative step raises.
- Over Range, h5py's metadata walk (superblock, object header, B-tree or chunk index) costs one 1 MiB block
  per distinct region it touches; files with metadata scattered through the data can cost several blocks
  to open.
- No other voxel-size conventions (`resolution`, `pixelResolution`, OME-NGFF-in-HDF5) are read.

## Example

`examples/zenodo-6546550-mouse-embryo-nuclei-membranes.yaml`: a zip of 41 `.h5` files, each with `/raw`
(uint16, `zyx`) and `/label` (uint16 or uint32 instance IDs, same grid). The members are deflated, so each
file is staged once with `io.fetch` and both arrays open it from the dataset cache. The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
