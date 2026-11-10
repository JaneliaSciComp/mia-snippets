# tiff-series-http

Reads a z-stack stored as one single-page grey TIFF per slice at plain HTTP(S) URLs and presents it as one
`zyx` array (`yx` for a single file). What it adds over the other TIFF snippets is the **numbered series**:
the binding gives the first file and the number of the last one, not one URL per slice, so a stack of
thousands of files stays a binding of one line.

- **URLs and `params.last`.**
  - With `last`: exactly one URL, the first file. The counter is the last run of digits in the URL's file
    name; it is counted up by 1 to `last`, keeping the zero padding when the first number has a leading zero
    (`amst_crop_0101.tif`, `last: 2037` → `amst_crop_0101.tif` … `amst_crop_2037.tif`). z index = number −
    first number. Every number in between must exist; take `last` and the absence of gaps from a complete
    file listing and say so in the binding's `reasons`. A zip member URL (`::`) is refused.
  - Without `last`: the URLs are the planes, in binding order.
- **Files.** Each must be a single-series, single-page `YX` TIFF with the first file's shape and dtype; one
  that differs raises at `read`, naming the URL. RGB tiles belong to `tiff-zip-glob-rgb`, multi-page stacks to
  `tiff-zip-member`.
- **Access.** `open_array` reads only the first file's header. `read` opens the files of the planes it is
  asked for. When the first file's pixels are stored uncompressed in one contiguous run, `access` is `chunk`
  and a y range is read as that range of rows (a 256-row patch of a 2718×2718 uint8 slice is 0.7 MB, not the
  7.4 MB file); otherwise `access` is `plane` and the page is decoded whole. The choice is re-made per file,
  so a compressed file inside a raw series still reads correctly. A server that ignores Range raises
  `NeedsFetch` and the file is staged whole with `io.fetch`.
- **dtype.** The file's, in native byte order. **Voxel size:** `None`; plain TIFF resolution tags describe
  print size, not pixels. Take it from the record.

## Known limits

- **Only the first file is in the binding's `urls`.** The harness derives what it knows about sources from
  `urls`: a crop's `crop.json` carries validators for the files `io` touched among them (the first file, read
  at open), and `harness stage` prefetches and records in `derived_from` only that file; the other slices
  are then read by range through `io` (still counted and host-checked, and their validators are in
  `io.validators`), and an offline re-stage from the cache cannot reach them. Staging this kind of dataset
  with full provenance needs the harness to expand the series, as it does a `<zip>::<glob>` URL; until then
  list every URL (no `last`) when that matters.
- A missing number inside the series is found only when its plane is read (HTTP 404 from `io`).
- The series step is 1 and the counter is the last number of the file name; other numbering schemes need
  explicit URLs.
- No pyramid levels, no tiles, no colour, no voxel size.

## Example

`examples/empiar-11399-dinoflagellate-fib-sem.yaml`: EMPIAR-11399, a FIB-SEM volume of a dinoflagellate cell,
1,937 uncompressed 2718×2718 uint8 slices (`amst_crop_0101.tif` … `amst_crop_2037.tif`, 14.3 GB) read as
`zyx` 1937×2718×2718. No labels are bound: the deposited segmentation is surface meshes, and the voxel masks
of the MoBIE project are N5 at half resolution on another host. The example binding's `snippets_commit` is
the commit before this snippet's own (a binding can't name the commit that adds it); a binding written by the
worker names a commit that contains the snippet.
