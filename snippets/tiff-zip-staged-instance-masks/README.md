# tiff-zip-staged-instance-masks

Reads 2-D grey TIFF members of a zip whose server sends no `Content-Length`, and can merge a set of
one-object binary masks into one instance-id plane.

The other zip snippets reach a member through `io.open` or `io.fetch` on the `<zip url>::<member>` url. Both
need the zip's central directory by range, and so the zip's size from a HEAD request. A DSpace bitstream
(OPARA, TU Dresden) answers HEAD without `Content-Length`, and `io.open`, `io.fetch`, `io.list` and
`io.expand` of a member all raise `NeedsFetch`. `io.fetch(<zip url>)` of the zip itself does work there. This
snippet stages the whole zip that way and reads the members from the local file with `zipfile`.

- **URLs.** Each url is `<zip url>::<member>` or `<zip url>::<glob>` (`fnmatch` syntax: `*`, `?`, `[...]`;
  `*` also crosses `/`). All urls of one array must be in the same zip. The snippet expands a glob itself,
  against the staged zip's member names, because `io.expand` cannot list such a zip. The members are taken
  once each in **natural name order** (digit runs compare as numbers: `2.tif` before `10.tif`), whatever the
  order of the urls. A url or glob that matches nothing raises.
- **Files.** Every member must be a single-series TIFF with one grey `YX` plane (size-1 axes are dropped). RGB
  files and stacks raise.
- **One member** (no params): the urls must name exactly one member, returned as stored, in the file's dtype.
- **`instances: true`**: each member is the binary mask of one object, all on the same grid (integer or
  boolean, non-zero = object; a member whose shape or dtype differs from the first raises at `read`). The
  array is one `uint16` plane: the pixel value is the 1-based position of the mask's member in natural name
  order (`0.tif` → 1, `1.tif` → 2, …, `10.tif` → 11), and 0 where no mask is set. Bind the label with the
  glob of one image's mask folder and pair it with that image.
- **Overlap.** A pixel set in several masks gets the highest of their ids. This is a fixed convention; the
  mask files do not say which object is in front.
- **`classes` + `class_id`** (with `instances`): `classes` is the member name (not a url, no glob) of a 1-D
  integer `.npy` with one class id per mask, in the masks' natural order; only masks whose entry equals
  `class_id` are merged. The kept objects keep the ids they have without the filter, so two class planes of
  one image never share an id. Where masks of two classes overlap, each class plane keeps its own object
  whole, which the unfiltered plane cannot. A class with no object gives a plane of zeros. The `.npy` is
  loaded with `allow_pickle=False`, and a length other than the number of masks raises.
- **Access.** `file`: `open_array` stages the whole zip with `io.fetch` (its full size counts against the
  job's byte cap once; later arrays and later jobs with the same dataset cache hit the cache), then reads the
  first member's TIFF header and the `.npy`, no pixels. The first `read` inflates the member, or every mask
  of the plane, and keeps the decoded plane in memory; later reads slice it.
- **dtype.** The file's dtype for one member; `uint16` for `instances` (more than 65,535 masks raises), so
  smoke warns when the record lists the masks' own dtype.
- **Voxel size.** `None`: plain TIFF resolution tags describe print size, not pixels.

## Tested on

`opara-462-inline-alumina-particles-copy-paste`: one 661,181,494-byte zip, 22 synthetic copy-paste images
(2056 × 2464 `uint8`) with 3 to 103 masks each in `masks/<N>/<k>.tif` (values 0/1) and the classes in
`masks/<N>/<N>.npy` (1 = particle, 2 = bubble). That entry `k` of the `.npy` belongs to `<k>.tif` was checked
on images 0 and 13: the class-2 masks are discs. Masks overlap in 12 of the 22 images, 5 to 6,365 pixels per
image, never more than two deep, in every class combination.

## Known limits

- The job's byte cap must admit the whole zip, even for one small member. Use the other zip snippets when
  the server reports the zip's size.
- Harness commands that resolve the member urls themselves still meet `NeedsFetch` on such a server:
  `harness list` and `harness describe <zip>::<member>` stop with it, and `harness fetch` reports each url
  as `stream` instead of caching it (run on the example binding). `harness stage` prefetches every url the
  same way before it opens the arrays and has no such tolerance in its code, so it is expected to stop
  there; that was read, not run. `smoke`, `crop` and `describe --binding` go through the snippet and work.
- One plane per array: no z, no channels, no multi-page TIFFs. Each image with its own mask folder is one
  raw and its labels in the binding.
- The instance plane is in memory once read (2 bytes per pixel), and its first read inflates every mask of
  the image.
- Overlapping objects cannot both be kept in one plane; the per-class filter only separates overlaps between
  classes.
