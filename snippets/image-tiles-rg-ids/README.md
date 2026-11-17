# image-tiles-rg-ids

Reads a set of one-image files, PNG or single-page TIFF, as one array: one file per z plane, or no z axis for a
single file. It is `image-tiles-glob` for grey files, with two additions:

- **Instance ids spread over two samples.** `ids: rg` reads a colour PNG whose id is R + 256\*G as one `uint16`
  id plane, and `category_id` keeps the instances of one class, taken from the class JSON beside the mask.
- **A zip the harness cannot read by member.** When the server states no size on `HEAD` or ignores `Range`,
  `io.expand`, `io.open` and `io.fetch` of a member all raise `NeedsFetch`. The snippet then stages the whole
  zip once with `io.fetch(<zip url>)` and reads its members from the local copy.

## What it reads

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>` or a zip member glob
  `<zip url>::<pattern>` (`fnmatch` syntax: `*`, `?`, `[...]`; `*` also crosses `/`). A glob is expanded and
  sorted by name; several URLs are concatenated in binding order, and a file named twice raises. An HTTP
  directory glob `<dir url>/::<pattern>` is expanded by `io.expand` as usual. Two arrays bound with the same
  globs over sibling folders (`images/*.tif` and `labels/masks/*.png`) pair plane for plane when the folders
  hold the same stems. The snippet cannot check that across arrays, so the binding's `reasons` must say why the
  names pair.
- **Files.** PNG (8 or 16 bit) or a single-series, single-page TIFF, told apart by the first bytes, not the
  name. Every file of one array must give the same shape and dtype as the first; one that differs raises at
  `read`. Bind files of different size or dtype as separate arrays.
- **Without params** a file must have no sample axis (`YX`) and its values are returned as decoded. A colour
  file raises: `image-tiles-glob` reads colour as a `c` axis.
- **`ids: rg`.** Each file must decode to 8-bit `(Y, X, S)` with at least 3 samples (RGB or RGBA). The array
  is `R + 256*G` as `uint16`, 0 = background. A nonzero blue sample raises, because it would be the third byte
  of the id and the encoding would not be the one this param names. Alpha is ignored.
- **`category_id: <int>`** (needs `ids`). For a mask `<dir>/masks/<stem>.<ext>` the snippet reads
  `<dir>/classes/<stem>.json`, `{"annotations": [{"id": <instance id>, "category_id": <class>}, ...]}`, from
  the same zip or URL folder. Instances whose `category_id` equals the param keep their id; all others become
  0. Ids are not renumbered, so two labels of one mask with different `category_id` have disjoint ids. A mask
  id with no annotation raises (its class is unknown), as does a file that is not in a `masks` folder, an id
  outside 1..65535 or an id listed with two categories. What a category value means comes from the record's
  classes, not from the file.
- **Axes.** `zyx`, or `yx` for one file. The z axis is a stack of independent images in name order, not depth.
- **Access.** `plane`: one file is one z plane, and any region of it costs the whole file. A stored member or a
  Range-capable plain URL is read through `io.open`; a deflated member is staged with `io.fetch`; if that also
  raises `NeedsFetch`, the whole zip is staged and read with `zipfile`, which checks each member's CRC.
  `open_array` decodes only the first file (shape, dtype, and the params are tried on it); the other files are
  read when `read` asks for them. The last decoded plane is kept.
- **dtype.** The file's, or `uint16` with `ids`. **Voxel size:** `None`. The files of one array need not share
  a calibration, and PNG/TIFF resolution fields usually describe print size.

## Known limits

- The whole-zip fallback costs the zip's full size against the job's byte cap the first time (632,684,521 bytes
  for the example); later jobs with the same dataset cache read it for 0 bytes. The harness fetches a file of
  unknown size in one request and cannot resume it. `harness fetch` reports such member urls as `stream`, and
  `harness list` / `harness describe` cannot read the zip at all; that is the harness's limit, not something a
  binding can change.
- Only R + 256\*G is implemented. A mask that uses blue as a third id byte raises instead of being read.
- The class JSON's place is fixed (`masks/` -> `classes/`, same stem, `.json`); there is no param for another
  layout.
- A palette PNG or a 1/2/4-bit grey PNG raises: the decoder would replace the stored indices by colours or
  rescale them.
- Each file is read whole into memory, so this is for single images, not for large volumes. No JPEG, no
  multi-page TIFF, no pyramid levels.
- An ImageJ TIFF with damaged metadata still reads (tifffile logs `imagej_metadata raised ...` and falls back to
  the page's own shape); some of the example's LZW ImageJ files do this.

## Example

`examples/tyc-trapped-yeast-cells-instance-segmentation.yaml`: the labeled set of the TYC dataset, 105
brightfield images of yeast cells in microfluidic traps (`labeled_set.zip`, deflated, on a DSpace server that
answers `HEAD` without `Content-Length`). The images come in three kinds, so the binding has three raws:
`uint16` 2048x2048 (60 files), `uint8` 2048x2048 (the 18 `SPD_*` files) and `uint16` 2304x2304 (27 files). Each
raw has two labels from the same RGBA/RGB mask PNGs: `category_id: 1` (cells) and `category_id: 0` (traps).

Checked on every bound plane, read through this snippet: each cell and trap label is disjoint from the other
and together they equal the mask's full id map; 4,405 cell and 14,541 trap instances in total (2,807 / 10,981,
937 / 2,383 and 661 / 1,177 for the three raws).

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
