# mat5-variable

Reads one 2D numeric or logical variable of a MATLAB 5 MAT-file (what `save` writes with `-v6` or `-v7`, the
default; the header starts `MATLAB 5.0 MAT-file`) as a `yx` array, from a plain URL (https, public `s3://`) or a
zip member (`<zip url>::<member>`). Several variables of the same size can be combined into one uint8 mask or
id map. The parser is the snippet's own (`struct`, `zlib`, numpy): the allowlist has no MAT reader.

- **Row/column order.** MATLAB stores a matrix column after column. The snippet reshapes in that order, so
  `array[y, x]` is MATLAB's `V(y+1, x+1)`: rows are `y`, columns are `x`, as `imread` and `imagesc` use them.
  `shape` is the variable's `size`.
- **dtype.** With one name and no `combine`: the variable's MATLAB class (`double`, `single`, `int8` … `uint64`),
  whatever narrower type `save` packed the numbers into. A `logical` is uint8 of 0 and 1. Nothing else is cast.
- **Combining.** With several names (`variable: "BW1,BW2"`) the result is uint8:
  - `combine: union` (the default for several names): 1 where any of the variables is nonzero.
  - `combine: ids`: the k-th name gets id k (from 1, in the order the binding names them) where it is nonzero.
    Variables that overlap are refused at the first read, because one id per pixel cannot hold them.

  `combine` with a single name applies the same rule to it (`union`: its nonzero pixels as 1).
- **Access.** `file`. Opening reads the 128-byte header, then each element's 8-byte tag and, for a compressed
  element, the first bytes of its zlib stream to get the class, dims and name; it stops at the last name it was
  asked for. Each of those is a range read (one 1 MiB block of `io`'s cache per variable passed). The first
  `read` fetches the named variables' elements and inflates them whole, since a zlib stream has no random
  access, and keeps the result in memory (rows × columns bytes for a mask); later reads cost no bytes. A file
  that cannot be read by range (a deflated zip member) is staged whole with `io.fetch`.
- **Voxel size.** Always `None`: the format has no place for it.

## Params

- `variable` (required, str): the variable's name as `whos -file` lists it, or several names separated by
  commas. Names are per file; an unknown name fails with the list of the names the file holds.
- `combine` (optional, str): `union` or `ids`, above. Choose `ids` when each variable is one object of a class and
  the objects do not overlap; `union` when the label is the class.

The binding's `reasons` say what each named variable is (from the script that wrote or loads the file) and why
it is a label: a MAT-file also holds intermediate results (thresholds, masked images) that are not annotations.

## Known limits

- 2D only. A 3D or higher variable is refused (MATLAB's dimension order of a stack is a per-dataset
  convention this snippet has no param for).
- Real numeric and logical arrays only. `char`, `cell`, `struct`, object, sparse and complex variables are
  refused, as are variables nested inside a struct or cell.
- MATLAB 7.3 files (`save -v7.3`) are HDF5 and are refused with a message saying so; MATLAB 4 files are refused.
- The whole variable is held in memory after the first read, and `ids` or `union` hold one more while they
  combine. A 41693×7559 mask is 315 MB.
- Up to 255 names with `ids`.
- Big-endian files (header ending `MI`) and uncompressed `-v6` elements are parsed the same way but were only
  tested on files written for the test, not on a published dataset.

## Example

`examples/zenodo-4064835-glass-fibre-composite-sem-bundles.yaml`: two stitched backscatter SEM mosaics
(11381×15406 and 7559×41693 uint16 TIFF) with `Case2Vf.mat` and `Case1Vf.mat`, whose logical variables
`BW1..BWn` mark the fibre bundle regions on the mosaic's grid; the binding reads them as one binary mask per
image. Checked on this data: every variable has its mosaic's shape, and the row/column order is fixed by the
file's own `BW`, which the scripts compute as a threshold of the image (`BW = I>th`, `I = uint8(O/256)`): over
300 full rows of Case 2 (4.6 million pixels) `BW` read through this snippet equals `TIFF // 256 > 53` at every
pixel, while the same mask flipped or transposed agrees on about half. `BW1` and `BW6` of Case 1 overlap, so
`ids` refuses that file and the binding uses the union. The example binding's `snippets_commit` is the commit before this snippet's own (a binding
can't name the commit that adds it); a binding written by the worker names a commit that contains the snippet.
