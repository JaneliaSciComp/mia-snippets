# mrc-http-3

Reads one MRC/REC volume (MRC2014) over range requests through `io.open`; works for any URL `io` can open
(https, `s3://` public buckets). Exactly one URL per array. Supersedes `mrc-http-2`: without params it reads
every file exactly as `mrc-http-2` does, and it adds one binding param, `mode0`, for the case `mrc-http-2`
lists under its known limits.

## What mrc-http-2 could not read

MRC2014 defines mode 0 as int8. `mrc-http-2` follows IMOD's header stamp when a file has one (IMOD's 8-bit
data comes back as uint8 0..255) and otherwise reads mode 0 as the stored int8. Some mode 0 files hold unsigned
bytes and carry no stamp: old IMOD output and other pre-MRC2014 writers. Read as int8, every value above 127
wraps negative (128 becomes -128), which tears the bright end of the histogram off and puts it below the dark
end. The header cannot tell such a file from a real int8 volume, so the choice has to come from the binding.
`empiar-10070-muscle-mitochondria-fib-sem` is such a file: mode 0, `imodStamp` 0, 8-bit grey values up to 163.

## What it reads

- **Header.** The 1024-byte header is parsed by the snippet: `nx, ny, nz`, `mode`, `mx, my, mz`, cell
  dimensions, `mapc/mapr/maps`, `nsymbt`, `imodStamp`/`imodFlags` (bytes 152 and 156), and `MACHST` for byte
  order (`0x11` big-endian, else little). Data starts at `1024 + nsymbt`.
- **Modes.** 1 → int16, 2 → float32, 6 → uint16, 12 → float16. Anything else but 0 is refused.
- **Mode 0.**

  | header | `mode0` param | stored bytes | returned |
  |---|---|---|---|
  | `imodStamp` = 1146047817 and `imodFlags` bit 0 set (IMOD ≥ 4.2.15) | absent | `value - 128`, signed | uint8, stored + 128 |
  | `imodStamp` = 1146047817 and `imodFlags` bit 0 clear (older IMOD) | absent | unsigned | uint8, as stored |
  | no IMOD stamp | absent | signed (MRC2014) | int8, as stored |
  | no IMOD stamp | `uint8` | unsigned | uint8, as stored |

  The first three rows are `mrc-http-2`'s. The IMOD stamp changes nothing in the other modes.
- **Axes and access.** `zyx`, `access: chunk`. `read({"z": k, "y": slice, "x": slice})` fetches rows
  `y0..y1` of plane `k` as one byte range (whole rows, since rows are contiguous) and slices `x` locally.
  Reads go through `io`'s 1 MiB block cache. A `z` slice reads plane by plane.
- **Voxel size.** cell (Å) / `m` (`n` when `m` is 0), converted to nm; `None` when a cell dimension is 0.

## Params

- `mode0` (optional; the only value is `uint8`). The stored bytes of an un-stamped mode 0 file are returned as
  they are, as uint8 0..255. No value is changed: it is the same bytes under the unsigned reading.

  Set it only on evidence, and give that evidence in the binding's `reasons`:
  - the record reports uint8 with a value range above 127 and says the int8 reading wraps, or
  - on a few planes, the histogram read as uint8 runs smoothly through 127/128 while read as int8 it has
    mass at both -128 and 127 with a gap between the two ends; neighbouring pixels differ less as uint8.

  A dataset description saying "8-bit images" supports it but is not enough alone: IMOD-style `value - 128`
  storage is also 8-bit data (see the limits).

  It is refused (`ValueError`) where the header already decides, so a binding cannot override the file:
  on a mode 0 file with the IMOD stamp, and on any mode other than 0. Any other value is refused too.
- Absent, everything reads as in `mrc-http-2`.

## Known limits

- Only `mapc/mapr/maps = 1 2 3` (x fastest); other axis orders are refused.
- A mode 0 file with `value - 128` storage but no IMOD stamp still reads as the stored int8 (no param for an
  offset of 128; a new snippet when a dataset needs it). A mask in such a file has a non-zero background.
- `mode0: uint8` is the binding's claim, not something the snippet can check: on a real int8 volume it returns
  negative values as 128..255.
- IMOD's "4-bit values swapped" flag (bit 4, mode 101 only) does not apply: mode 101 is refused.
- Extended headers (FEI, SERI, …) are skipped, not parsed. Complex modes (3, 4) are refused.
- Byte order comes only from `MACHST`; a big-endian file with a zeroed `MACHST` fails the header sanity
  checks rather than being detected.

## Example

`examples/empiar-10070-muscle-mitochondria-fib-sem.yaml`: one aligned FIB-SEM volume, 291 x 1595 x 1675,
mode 0, no IMOD stamp (header label `tif2mrc: Converted to MRC format. 08-Mar-13`), read with `mode0: uint8`.
The dataset's only annotations are STL surface meshes, which this snippet does not read, so the binding has no
labels.

Planes z = 21..118 of that file are all zero as deposited (about a third of the volume; checked on the middle
row of every third plane and by direct range requests at the two ends of the run). The snippet returns them as
they are; a 4-crop test of the binding wrote 4 crops of tissue and rejected 5 candidates on content.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
