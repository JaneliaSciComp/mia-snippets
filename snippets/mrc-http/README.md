# mrc-http

Reads one MRC/REC volume (MRC2014) over range requests through `io.open`; works for any URL `io` can open
(https, `s3://` public buckets). Exactly one URL per array.

- **Header.** The 1024-byte header is parsed by the snippet: `nx, ny, nz`, `mode`, `mx, my, mz`, cell
  dimensions, `mapc/mapr/maps`, `nsymbt`, and `MACHST` for byte order (`0x11` big-endian, else little).
  Data starts at `1024 + nsymbt`.
- **Modes.** 0 → int8, 1 → int16, 2 → float32, 6 → uint16, 12 → float16. Anything else is refused.
- **Axes and access.** `zyx`, `access: chunk`. `read({"z": k, "y": slice, "x": slice})` fetches rows
  `y0..y1` of plane `k` as one byte range (whole rows, since rows are contiguous) and slices `x` locally.
  Reads go through `io`'s 1 MiB block cache, so a 256-row patch of a 928-wide float32 tomogram costs one or
  two blocks. A `z` slice reads plane by plane.
- **Voxel size.** cell (Å) / `m` (`n` when `m` is 0), converted to nm; `None` when a cell dimension is 0.

## Params

None.

## Known limits

- Only `mapc/mapr/maps = 1 2 3` (x fastest); other axis orders are refused.
- Mode 0 is read as signed int8 (MRC2014). Old IMOD files that store unsigned bytes in mode 0 read wrong
  (values above 127 wrap); there is no IMOD flag check.
- Extended headers (FEI, SERI, …) are skipped, not parsed. Complex modes (3, 4) and mode 101 (4-bit) are
  refused.
- Byte order comes only from `MACHST`; a big-endian file with a zeroed `MACHST` fails the header sanity
  checks rather than being detected.

## Example

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
