# mrc-http-2

Reads one MRC/REC volume (MRC2014) over range requests through `io.open`; works for any URL `io` can open
(https, `s3://` public buckets). Exactly one URL per array. It is `mrc-http` with one addition, unsigned
mode 0, and supersedes it: with no params, a file reads exactly as it did with `mrc-http` unless its header
carries the IMOD stamp with the "bytes are signed" flag clear.

- **Header.** The 1024-byte header is parsed by the snippet: `nx, ny, nz`, `mode`, `mx, my, mz`, cell
  dimensions, `mapc/mapr/maps`, `nsymbt`, `imodStamp`/`imodFlags` (bytes 152-159), and `MACHST` for byte
  order (`0x11` big-endian, else little). Data starts at `1024 + nsymbt`.
- **Modes.** 0 → int8 or uint8 (below), 1 → int16, 2 → float32, 6 → uint16, 12 → float16. Anything else is
  refused.
- **Mode 0 signedness.** In order: the `mode0` param if set; else uint8 when `imodStamp` is 1146047817
  (`IMOD`) and bit 0 of `imodFlags` is clear (IMOD's "bytes are stored unsigned"); else int8, as MRC2014
  defines it.
- **Axes and access.** `zyx`, `access: chunk`. `read({"z": k, "y": slice, "x": slice})` fetches rows
  `y0..y1` of plane `k` as one byte range (whole rows, since rows are contiguous) and slices `x` locally.
  Reads go through `io`'s 1 MiB block cache. A `z` slice reads plane by plane.
- **Voxel size.** cell (Å) / `m` (`n` when `m` is 0), converted to nm; `None` when a cell dimension is 0.

## Params

- `mode0` (`uint8` | `int8`, optional): how to read mode 0 bytes, overriding the header. Set it only when
  the record contradicts what the header gives, and say why in the binding's `reasons`. The usual case is an
  old IMOD file (before the stamp existed, IMOD wrote unsigned bytes in mode 0 and left those header words
  0): the record's `dtype` is `uint8`, or its `value_range` goes above 127, or read as int8 the image wraps
  to negative values in its bright regions. Any other value, or setting it for a file that is not mode 0, is
  refused.

## Known limits

- Only `mapc/mapr/maps = 1 2 3` (x fastest); other axis orders are refused.
- A stampless mode 0 file is int8 unless the binding says otherwise; the snippet does not guess from the
  header labels or from the data.
- Extended headers (FEI, SERI, …) are skipped, not parsed. Complex modes (3, 4) and mode 101 (4-bit) are
  refused.
- Byte order comes only from `MACHST`; a big-endian file with a zeroed `MACHST` fails the header sanity
  checks rather than being detected.

## Example

`empiar-10070-muscle-mitochondria-fib-sem`: a FIB-SEM volume written by IMOD `tif2mrc`/`newstack` in 2013,
mode 0 with no IMOD stamp, values 39-160 as unsigned bytes; bound with `mode0: uint8`. The STL meshes that
are the dataset's only annotations are not read (no labels in the binding).

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
