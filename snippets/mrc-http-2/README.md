# mrc-http-2

Reads one MRC/REC volume (MRC2014) over range requests through `io.open`; works for any URL `io` can open
(https, `s3://` public buckets). Exactly one URL per array. Supersedes `mrc-http`; the only difference is how
mode 0 is decoded in a file written by IMOD.

## What was wrong with mrc-http

`mrc-http` read every mode 0 file as the stored int8. IMOD (`tif2mrc`, `newstack`, …) keeps 8-bit data, 0..255,
in mode 0 as `value - 128` and marks that in its header stamp; IMOD adds the 128 back when it reads. Read
without it, a 0/255 mask is -128/127: background is not 0, so the harness's binary label encoding
(`value != 0`) made every crop of such a mask all foreground. That is what happened to
`empiar-11380-organoid-fib-sem`, whose 19 files are all `tif2mrc` output.

## What it reads

- **Header.** The 1024-byte header is parsed by the snippet: `nx, ny, nz`, `mode`, `mx, my, mz`, cell
  dimensions, `mapc/mapr/maps`, `nsymbt`, `imodStamp`/`imodFlags` (bytes 152 and 156), and `MACHST` for byte
  order (`0x11` big-endian, else little). Data starts at `1024 + nsymbt`.
- **Modes.** 1 → int16, 2 → float32, 6 → uint16, 12 → float16. Anything else but 0 is refused.
- **Mode 0.**

  | header | stored bytes | returned |
  |---|---|---|
  | `imodStamp` = 1146047817 and `imodFlags` bit 0 set (IMOD ≥ 4.2.15) | `value - 128`, signed | uint8, stored + 128 |
  | `imodStamp` = 1146047817 and `imodFlags` bit 0 clear (older IMOD) | unsigned | uint8, as stored |
  | no IMOD stamp | signed (MRC2014) | int8, as stored |

  The first two rows give the values IMOD itself shows for the file, which are those of the 8-bit images it
  was made from. The IMOD stamp changes nothing in the other modes.
- **Axes and access.** `zyx`, `access: chunk`. `read({"z": k, "y": slice, "x": slice})` fetches rows
  `y0..y1` of plane `k` as one byte range (whole rows, since rows are contiguous) and slices `x` locally.
  Reads go through `io`'s 1 MiB block cache. A `z` slice reads plane by plane.
- **Voxel size.** cell (Å) / `m` (`n` when `m` is 0), converted to nm; `None` when a cell dimension is 0.

## Params

None. The mode 0 decoding comes from the file's own header, so a binding has nothing to choose. Its `reasons`
should say when the dtype differs from the record's for this reason (a record made with `mrcfile` reports the
stored int8, and smoke warns about the difference).

## Known limits

- Only `mapc/mapr/maps = 1 2 3` (x fastest); other axis orders are refused.
- A mode 0 file with unsigned bytes and no IMOD stamp (not MRC2014, not IMOD) reads as int8, values above 127
  wrapped. A mode 0 file with `value - 128` storage but no IMOD stamp reads as the stored int8, and a mask in
  it has a non-zero background; neither can be told from a real int8 volume by the header.
- IMOD's "4-bit values swapped" flag (bit 4, mode 101 only) does not apply: mode 101 is refused.
- Extended headers (FEI, SERI, …) are skipped, not parsed. Complex modes (3, 4) are refused.
- Byte order comes only from `MACHST`; a big-endian file with a zeroed `MACHST` fails the header sanity
  checks rather than being detected.

## Example

`examples/empiar-11380-organoid-fib-sem.yaml`: four FIB-SEM volumes and their 15 binary organelle masks, all
`tif2mrc` output with `imodFlags` 13. Raw reads as uint8 grey values, masks as 0 (background) / 255.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
