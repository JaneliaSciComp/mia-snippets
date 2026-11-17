# image-npy-zip-member

Reads **one file as one 2D array**: a PNG (decoded with `imagecodecs`), a single-page TIFF (`tifffile`) or a
NumPy `.npy` array (its header parsed with `numpy.lib.format`, its data read as raw bytes, never unpickled).
The file is a zip member or a plain URL, and its format is taken from its first bytes, not its name: the
example dataset has a TIFF named `90.png`.

It exists for image sets whose files differ in size, where a label map may be a `.npy` file. `image-tiles-glob`
stacks the files of a glob along z and needs them all to have the first file's shape; it reads no `.npy`. Here
an array is one file, so every file keeps its own shape, and a mask is paired with its image by the binding's
`pairs_with`, file name by file name.

- **URLs.** Exactly one file per array: a plain URL, a zip member `<zip url>::<member>`, or a glob that
  matches one file. Anything that names two or more files raises. A set of N images is N `raw` entries, and a
  mask is a `labels` entry whose `pairs_with` is the index of the image with the same name. Pair by name, not
  by position in a sorted listing: the folders need not hold the same names (the example's mask folder has 18
  masks without an image), and the binding's `reasons` say how the names pair.
- **PNG.** 8- or 16-bit, grey, grey+alpha, RGB or RGBA, interlaced or not. A grey PNG is `yx`. A PNG with
  samples is `cyx`, `c` in the file's sample order (R, G, B; alpha last when stored).
- **TIFF.** One series of one page, `YX` or `YXS` (RGB), any compression `tifffile` decodes: `yx` or `cyx` like
  a PNG. A multi-page or multi-series file, a planar (`SYX`) or a palette TIFF raises at open; stacks belong
  to `tiff-zip-member`.
- **`samples: grey`** (the only param, PNG and TIFF only). For a file whose samples are identical copies, a
  mask or grey image saved as RGB: sample 0 is returned and the array is `yx`. Samples that differ raise at
  `read`, so a colour image is never silently reduced to its red channel. A grey file passes through. Refused
  for `.npy`.
- **`.npy`.** Format version 1.0 or 2.0, 2-D, dtype bool, integer or float, C or Fortran order, either byte
  order: `yx` with the stored values (a big-endian file is byte-swapped to the native order, `dtype` is the
  native one). Refused at open, with the reason in the message: an object array (it would need pickle), a
  structured or complex dtype, any number of dimensions other than 2.
- **Values** are the stored ones: a mask PNG returns its stored values (0 and 255 in the example), a `.npy`
  class map its ids.
- **Open.** `open_array` reads the header only: a PNG's `IHDR` (size, bit depth, colour type), a TIFF's first
  IFD or the `.npy` header (shape, dtype, order). No pixel is decoded. A stored member or a Range-capable plain URL is read
  through `io.open` (the header costs the file's first 1 MiB block); a deflated member, or a server without
  Range, raises `NeedsFetch` and the file is staged whole with `io.fetch` at open, since nothing of a deflated
  member can be read without all of it.
- **Access.**
  - PNG and TIFF: `file`. A PNG is one deflate stream, so any region costs the whole file, and a TIFF is read
    the same way; the image is decoded at the first `read` and kept, so the `c` planes and further patches
    cost nothing more.
  - `.npy`, C order, read by range (a stored member, a plain URL): `chunk`. `read` asks only for the rows the
    region covers.
  - `.npy` that was staged at open (a deflated member), or in Fortran order: `file`. A staged file is still
    read row by row from disk; a Fortran-ordered one is read whole once and kept.
- **dtype.** PNG: `uint8` or `uint16`. TIFF and `.npy`: the file's, in native byte order. **Voxel size:** `None`; a PNG's
  `pHYs` chunk and a TIFF's resolution tags are print resolutions, and `.npy` has no metadata.

## Known limits

- **One file per entry, each opened by the harness.** The harness opens every array of a binding before it
  reads anything, and opening a deflated zip member downloads it. A binding of N pairs therefore costs all 2N
  files and about 4N requests (a local header and the data, per file) at open. Binding all 302 pairs of the
  example dataset would be 604 arrays, about 1,200 requests and the whole 2.1 GB at every open; the example
  binds 6 pairs.
- **Zenodo answered `429 TOO MANY REQUESTS`** to two of the four smoke runs made while this snippet was written
  (after 2 and after 3 members of that run, with other requests to zenodo.org in the minutes before; its
  headers state 133 requests a minute). The harness does not retry a 4xx, so the run fails; members already
  fetched stay in the cache and the next run goes on from there; the fourth run, with 6 of the 12 files cached
  by then, passed.
- A palette PNG or a 1/2/4-bit grey PNG raises at open: the decoder would turn palette indices into colours and
  rescale low bit depths to 0..255, so the stored ids would be lost.
- A PNG whose `tRNS` chunk makes the decoder add an alpha sample raises at `read` (the decoded shape is checked
  against the `IHDR`).
- `samples: grey` is checked at the first `read`, not at open.
- Alpha is exposed as a `c` plane like any other sample; there is no param to drop it.
- An image is decoded whole into memory (a 3000x4096 RGB image is 37 MB). No pyramid levels.
- `.npy` only: no `.npz`, no 3-D arrays (a `(y, x, samples)` or stack `.npy` raises with its shape).
- The harness draws a crop from one `c` plane of a `cyx` array, so a colour image yields single-sample crops.

## Example

`examples/zenodo-17726414-root-lat-pearl-millet-sorghum.yaml`: laser ablation tomography cross sections of
pearl millet and sorghum roots, one 2.1 GB zip at Zenodo with every member deflated. Two sets: `binary/`
(177 RGB PNG images and 195 RGB-stored tissue/background PNG masks, values 0 and 255) and `multiclass/` (125
RGB images named `.png` and 125 `.npy` class maps, ids 0..8). Image sizes differ from file to file. The binding
takes three pairs of each set: binary `0`, `1`, `100` and multiclass `0`, `1` (pearl millet) and `90` (sorghum,
by the zip's `test_sorghum.txt`). What the six pairs showed:

| pair | image | label |
| --- | --- | --- |
| binary 0 | PNG, `cyx` 3x2074x2089 `uint8` | PNG as RGB, `yx` `uint8`, values 0 and 255 |
| binary 1 | PNG, 3x1714x2054 | same, 1714x2054 |
| binary 100 | PNG, 3x1923x2038 | same, 1923x2038 |
| multiclass 0 | PNG, 3x3000x3753 | `.npy` `int64` 3000x3753 |
| multiclass 1 | PNG, 3x969x1714 | `.npy` `int64` 969x1714 |
| multiclass 90 | **TIFF** (LZW, RGB) named `90.png`, 3x3000x4096 | `.npy` **`uint8`** 3000x4096 |

So the multiclass set is not uniform in format or dtype: at least one image is a TIFF under a `.png` name, and
at least one class map is `uint8` where the record sampled `int64`. Which id is which of the nine tissue classes
is not stated in the record. The example binding's `snippets_commit` is the commit before this snippet's own
(a binding can't name the commit that adds it); a binding written by the worker names a commit that contains
the snippet.
