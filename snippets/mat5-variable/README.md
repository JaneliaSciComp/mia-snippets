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
- **Few requests, and a 429 waited out.** A host may count requests, not bytes: zenodo.org allows 133 a minute
  to everything that comes from one address and answers the next one 429, which the harness does not retry.
  So when opening finds one of several names, it asks at once for the stretch the names still looked for
  would fill if they came next at the same size (at most 8 MiB, one request): for masks saved together that
  is the case, their headers are then not a request each, and the bytes are the ones the first `read` needs.
  When the guess is wrong, up to 8 MiB per name were fetched for nothing. A variable is read in pieces of
  32 MiB, which `io` asks for as four ranges of 8 MiB at once, not one request per MiB. `Case1Vf.mat` (eight
  masks of 1.1 MB each behind a 16 MB variable) takes 4 range requests this way, not 11. Since the limit is
  shared with whatever else comes from the address, a 429 can still meet the first request: the snippet then
  sleeps and makes the read again, up to 7 times per read, so a refused job costs minutes, not a failed run.
  The sleep is the answer's `Retry-After` plus a second (60 s when there is none), but at least 2, 4, 8, … s
  from one refusal to the next and 120 s at most, about 4 minutes in all. The floor is there because Zenodo's
  `Retry-After` is the time until one more request is let through, not until the address is under its limit:
  0 to 11 s were seen while other clients of the address kept `x-ratelimit-remaining` between 0 and 10, and
  then the request made a second later is refused again. Ranges that did
  arrive are in `io`'s cache and are not asked for twice. Any other error is raised as it comes.
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

`examples/zenodo-4064835-glass-fibre-composite-sem-bundles.yaml`: the Case 2 stitched backscatter SEM mosaic
(11381×15406 uint16 TIFF) with `Case2Vf.mat`, whose logical variables `BW1` and `BW2` mark the fibre bundle
regions on the mosaic's grid; the binding reads them as one binary mask. Checked on this data: every variable
has the mosaic's shape, and the row/column order is fixed by the file's own `BW`, which the script computes as
a threshold of the image (`BW = I>th`, `I = uint8(O/256)`): over 300 full rows of Case 2 (4.6 million pixels)
`BW` read through this snippet equals `TIFF // 256 > 53` at every pixel, while the same mask flipped or
transposed agrees on about half. The example binding's `snippets_commit` is the commit before this snippet's
own (a binding can't name the commit that adds it); a binding written by the worker names a commit that
contains the snippet.

The raw of the example is read with `tiff-series-glob`, for the same request limit: `tiff-zip-member-2`
reads the same pixels but fetches the page whole (44 requests), and the crop job on that binding was refused
with 429 on the TIFF. The mosaic is uncompressed, so `tiff-series-glob` reads a crop's rows by range and asks
again after a 429, three times within 7 s. That is not always enough: the crop job at 743343b made 4 crops
and lost the fifth to a refusal that outlasted those 7 s (it fetched 48,773,972 bytes, which is the 40,385,364
of its four crops and one 8 MiB range of the fifth patch, whose second range was refused), while the same job
run again made 8 of 8. Waiting longer on the raw side takes a successor of
`tiff-series-glob` or the harness; this snippet's own reads wait as said above. Counted on a crop job of 8 crops: 18 requests and 68 MB with this binding (14 for the TIFF,
4 for the MAT-file), 49 requests and the whole 351 MB TIFF before. The threshold check above gives the same answer through
`tiff-series-glob`.

**Case 1 of the same record is left out of the binding, for the host's request limit only.** The snippet reads
`Case1Vf.mat` as well: `BW1..BW8` are 7559×41693, the shape of the Case 1 mosaic, their union has 46,631,147
pixels set, and `BW1` and `BW6` overlap, so `ids` refuses that file. But its raw is an 809.6 MB LZW TIFF, which
no snippet can read by row range: it is fetched whole, about 100 requests of 8 MiB, within a minute that
allows 133 from the whole address, and the crop job on the binding with both pairs was refused with 429 on
that TIFF. Once the harness waits out a 429, Case 1 is these two entries:

```yaml
raw:
  - snippet: tiff-series-glob
    urls: ["https://zenodo.org/api/records/4064835/files/Case1-Panorama%20-%20correlation%20matching.tif/content"]
    params: {}
labels:
  - snippet: mat5-variable
    urls: ["https://zenodo.org/api/records/4064835/files/Case1Vf.mat/content"]
    params: {variable: "BW1,BW2,BW3,BW4,BW5,BW6,BW7,BW8"}
    pairs_with: 1          # the index of the Case 1 raw
    label_key: manual_gt-fibre_bundle-semantic
    segmentation_type: binary
```
