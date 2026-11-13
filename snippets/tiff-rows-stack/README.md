# tiff-rows-stack

Reads single-page greyscale TIFFs, one file per z plane, and stacks them along z. Written for volumes whose
planes are large uncompressed TIFFs on a slow server (EMPIAR FIB-SEM slices: 96 MB each), where decoding a
whole page per 256 px patch does not finish.

- **URLs.** One per plane, in the order given: plain `http(s)`/`s3` URLs, zip members `<zip url>::<member>`,
  or a member glob `<zip url>::<glob>` (`*`, `?`, `[...]`, matched case-sensitively with `fnmatchcase`, `*`
  crosses `/`), which expands to the matching members of the zip's central directory sorted by name. Plain
  URLs cannot be globbed (`io` lists zips only), so they are listed one by one. A file named twice is refused.
- **Axes and shape.** Several files give `zyx` `(n_files, Y, X)`; one file gives `yx`. Shape and dtype come
  from the first file; every other file is checked against it when it is first read (files are opened
  lazily, `open_array` reads the first file's header only).
- **Reading by rows.** When a page is uncompressed, not tiled, has one sample per pixel, no predictor, fill
  order 1 and its strips sit back to back in row order (tifffile's `is_final`, plus the strip byte counts
  adding up to `Y * X * itemsize`), `read` seeks to `strip offset 0 + y0 * row bytes` and reads rows
  `y0..y1` as one byte range, then slices `x` locally. Through `io`'s 1 MiB block cache a 256-row patch of a
  12000 px wide uint8 slice costs about 3 MiB plus the block(s) holding the IFD, not the 96 MB page. Both
  byte orders are handled.
- **Everything else is a plane.** A compressed or tiled page is decoded whole with tifffile. A deflated zip
  member (or a server without Range) raises `NeedsFetch` in `io.open` and is staged whole with `io.fetch`.
- **`access`** is decided by the first file: `chunk` when it is read by range and by rows, `plane` otherwise
  (a staged or compressed file costs the whole file for any y/x region). A later file of another kind is
  still read correctly, each file by what it is.
- **Voxel size.** `None`; resolution tags are not parsed.

## Params

None.

## Known limits

- Single-page `YX` files only. Multi-page stacks are refused (use `tiff-zip-member`), and so are RGB / multi-
  sample pages (use `tiff-zip-glob-rgb`).
- A file whose shape or dtype differs from the first is refused at its first read, not at open: opening
  thousands of headers up front would cost a block per file.
- Each read of a plane re-parses that file's header (served from `io`'s block cache after the first time).
- Rows are whole: a narrow `x` range still fetches the full width of its rows.

## Example

`examples/empiar-10791-mouse-liver-fib-sem-organelles.yaml` binds the first 1000 slices of the lean-liver
volume 6461: raw slices 0000..0999 as plain URLs (names taken from the server's directory listing) and the
matching mitochondria masks as one zip member glob. Listing all 5638 plain URLs made the binding 1.7 MB, which
the mia-data worker could not read from the PR; 1000 URLs keep it near 0.3 MB.
Smoke on it fetched 6.2 MB. The binding's `snippets_commit` is the commit before this snippet's
own (a binding can't name the commit that adds it); a binding written by the worker names a commit that
contains the snippet.
