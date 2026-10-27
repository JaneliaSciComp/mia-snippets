# tiff-zip-glob-rgb

Reads a set of single-page TIFF tiles with `tifffile` and presents it as one array: one file per z plane
(`zyx`), or `yx` for a single file. Each file's sample axis (the `S` of an RGB TIFF) is collapsed to one
plane, which is what this snippet adds over `tiff-zip-member`: that snippet exposes `S` as `c`, so a grey
image saved as RGB yields three duplicate planes and a one-hot RGB mask yields three binary planes that the
harness pairs with the raw's samples.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, or a zip member glob
  `<zip url>::<pattern>` (`fnmatch` syntax: `*`, `?`, `[...]`; `*` also crosses `/`). A glob is expanded with
  `io.list` and sorted by member name; several URLs are concatenated in binding order. Two arrays bound with
  globs over sibling directories (e.g. `images/*.tif` and `labels/*.tif`) pair plane for plane when the two
  directories hold the same file names. The snippet cannot check that across arrays, so the binding's
  `reasons` must say why the names pair.
- **Files.** Every file must be a single-series, single-page `YX` or `YXS` TIFF, all with the same shape and
  dtype; a file that differs from the first raises at `read`. Multi-page stacks belong to `tiff-zip-member`.
- **Collapse** (`params`):
  - `samples: grey`: the samples are identical copies (a grey image saved as RGB). Sample 0 is returned;
    samples that differ raise, so a real colour image is not silently reduced to its red channel.
  - `samples: onehot`: each pixel is non-zero in exactly one sample. Returns `uint8` class ids `1..S` in
    sample order (argmax + 1), `0` where no sample is set; a pixel set in several samples raises. The class
    names behind the ids come from the record, not the file.
  - `sample: <k>`: one sample plane, e.g. one class's one-hot plane for a `binary` label. Exclusive with
    `samples`.
  - A `YX` file needs no param; `YXS` without a param raises.
- **Access.** `plane`: one file is one plane. A stored member or a Range-capable plain URL is read through
  `io.open`; a deflated member (or a server ignoring Range) raises `NeedsFetch` and is staged whole with
  `io.fetch`. `open_array` reads only the first file (shape, dtype, and the chosen collapse is tried on it);
  the other planes are fetched when `read` asks for them, so opening a 1,404-member set costs one zip
  directory read and one member.
- **dtype.** The file's dtype, except `onehot`, which is `uint8`. **Voxel size:** `None`; plain TIFF
  resolution tags describe print size, not pixels.

## Known limits

- Over HTTP each deflated member costs two requests (local header, compressed bytes), made lazily per plane.
  A job that reads every plane of a large set makes many small requests; Zenodo answered 504 to a few of
  them while this snippet was written, and the harness does not retry.
- `onehot` tests `!= 0`; a soft (probability) mask is not one-hot and raises when several samples are set.
- No pyramid levels, tiles larger than memory, or voxel size.
- A harness whose crop edge is fixed at 256 reports tiles smaller than that as ineligible; the pinned harness
  crops a small raw at its own extent.

## Example

`examples/zenodo-5553771-detecdiv-yeast-trap-segmentation.yaml`: a 6.3 MB zip of 1,404 brightfield yeast-trap
crops (60x59, grey as RGB, `samples: grey`) and 1,404 one-hot RGB masks with the same names
(`samples: onehot`, class ids 1 background, 2 mother, 3 other). The record notes that the order of samples 1
and 2 (mother, other) follows the Zenodo text and is not documented in the files. The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
