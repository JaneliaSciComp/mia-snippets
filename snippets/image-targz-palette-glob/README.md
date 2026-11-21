# image-targz-palette-glob

Reads a set of one-image files, PNG or JPEG, that are members of gzip-compressed tar archives (`.tar.gz`), and
presents it as one array: one member per z plane, or no z axis for a single member. It is `image-tiles-glob`
for tar.gz archives, plus the stored indices of palette PNGs. `image-targz-glob` also reads PNG/JPEG members
of a tar.gz by glob, by range reads of one archive at a plain URL; it refuses palette PNGs and does not open
a zip that wraps the tar.gz, which this snippet reads.

- **URLs.** Each URL is the plain URL of an archive, with no `::`. The staged file's first bytes say what it is:
  - a gzip stream: one tar.gz;
  - a zip: every member named `*.tar.gz` or `*.tgz` is an archive, the other members (a `MANIFEST.TXT`) are
    ignored. This is what Dataverse's bundle download `https://<host>/api/access/datafiles/<id>[,<id>...]`
    sends: a zip, built on request, of the named datafiles.
- **`members`** (required): a glob over the tar member names (`fnmatch` syntax: `*`, `?`, `[...]`,
  case-sensitive; `*` also crosses `/`), applied to every URL. It is a param, not `<url>::<glob>`, because `io`
  and the harness read `::` as a zip member. The regular files that match are sorted by member name within one
  URL (pooled over the archives of a zip; a name found twice raises), and several URLs are concatenated in
  binding order. Two arrays whose globs match the same base names (`ZT80_38_A/ZT80_38_A_*.jpg` and
  `.../mask1_ZT80_38_A_*.png`) pair plane for plane. The snippet cannot check that across arrays, so the
  binding's `reasons` must say why the names pair. Write the glob tightly: macOS tars carry `._<name>`
  AppleDouble members beside the real files, and one that matches raises as "not a PNG or JPEG".
- **Files.** By each member's first bytes, not its name: JPEG, or a PNG that is 8 or 16 bit grey or colour.
  Every member of one array must give the same shape and dtype as the first; one that differs raises at `read`.
- **`palette: index`** (optional): a palette (indexed-colour) PNG member, any bit depth, is returned as its
  stored palette indices, `uint8` with no `c` axis: the ids of a mask saved as an indexed PNG. A decoder would
  return the palette's colours instead, so the file is re-wrapped in memory as a greyscale PNG with the same
  `IDAT` bytes (the method of `png-palette-index-glob`) and the decoder's stretch of 1/2/4-bit samples is undone
  exactly. Without the param a palette PNG raises, so an indexed colour picture is never read as ids by
  accident. Other members are not affected by it.
- **Axes.** A member without samples is `zyx`. A member with samples is `zcyx`, `c` in the file's sample order
  (R, G, B). With one member the `z` axis is dropped (`yx`, `cyx`).
- **Values** are the decoder's (`imagecodecs`), unchanged; palette indices as stored. Nothing is remapped: an
  id map whose "unlabelled" id is not 0 is returned as it is (see the example).
- **Reaching the members.** A tar.gz has no index and its members cannot be read by range, and a glob needs
  every member name, so each URL is staged whole with `io.fetch` (counted against the job's cap once, then in
  the dataset cache). `open_array` inflates each archive once to list it and decodes the first match (shape and
  dtype). `read` inflates up to the member it needs and decodes it; the gzip stream is kept open, so members
  read in archive order cost one pass, and a member that lies before the last one read starts the pass again.
  The last decoded member is kept, so the three `c` planes of one image decode it once.
- **Access.** `plane`. **dtype.** The file's. **Voxel size:** `None`; PNG/JPEG resolution fields describe print
  size.

## Why the bundle URL for Harvard Dataverse

`io` asks every URL for its validators with a HEAD request before it reads a byte. A Dataverse datafile URL
(`/api/access/datafile/<id>`) redirects to an S3 URL presigned for GET only, and S3 answers that HEAD with
`403 Forbidden` (a ranged GET of the same URL answers 206), so `io.open` and `io.fetch` both fail on it and a
snippet has no other way to bytes. The bundle URL `/api/access/datafiles/<id>` redirects to Dataverse's own
zip service (`rserve.dataverse.harvard.edu` for Harvard), which answers HEAD and GET with 200. The job
therefore needs **`dataverse.harvard.edu` and `rserve.dataverse.harvard.edu`** among its allowed hosts, and
not the S3 host.

The zip is streamed without `Content-Length`, `ETag` or `Last-Modified`, so:

- it cannot be read by range or by zip member (`io.open` and `io.fetch` of `<url>::<member>` raise
  `NeedsFetch`); the snippet stages the whole zip and opens the tar.gz inside it locally;
- `io` records empty validators for it: a changed upstream file is not noticed, and the cache entry of a
  download that was cut short without a transport error would be kept. Such a file fails loudly here (no zip
  directory, or a CRC error of the zip member or the gzip stream), it is not read as data.

## Known limits

- Bytes: every archive a URL stands for is fetched whole, even for one member (245 MB for the 74 images of
  the example). Members deep in very large archives are what `czi-targz-member`'s range reads are for.
- Time: reading planes out of archive order re-inflates from the archive's start. All 74 planes of the
  example's 245 MB archive, in z order, took 38 s.
- Each member is read whole into memory (3 x 3100 x 3100 `uint8` in the example); no pyramid levels.
- A 1/2/4-bit grey PNG raises (the decoder rescales its samples to 0..255). TIFF members are not read.
- Samples are exposed as stored: an RGBA file has 4 `c` planes. A crop holds one `c` plane.
- Only gzip: a plain `.tar`, `.tar.bz2` or `.tar.xz` is refused by its first bytes. A zip is searched for
  `.tar.gz`/`.tgz` members by name only; images stored directly in a zip belong to `image-tiles-glob`.
- One open file handle per archive is held for the array's lifetime.

## Example

`examples/harvarddv-ocycmp-prostate-tma-gleason.yaml`: H&E prostate tissue-microarray spots with Gleason
annotation masks (Harvard Dataverse doi:10.7910/DVN/OCYCMP), 15 tar.gz archives. The binding reads the 74 spot
images of `ZT80_38_A.tar.gz` (RGB JPEG, `zcyx` 74 x 3 x 3100 x 3100 `uint8`) and their pathologist-1 masks from
`Gleason_masks_test_pathologist1.tar.gz` (`zyx` 74 x 3100 x 3100 `uint8`). The masks are 4-bit palette PNGs
whose indices are the class ids (palette: 0 green, 1 blue, 2 yellow, 3 red, 4 white), read with
`palette: index`; over the 74 masks the ids are 0 benign 1.6%, 1 Gleason 3 23.7%, 2 Gleason 4 23.1%,
3 Gleason 5 6.1%, 4 unlabelled 45.5% of the pixels.

The ids are returned as stored, so **benign is 0 and unlabelled is 4**. The harness takes nonzero pixels as
annotated: every crop of this binding counts as labelled, the unlabelled area included, and a crop of benign
tissue alone counts as unlabelled. The snippet does not renumber ids; whether this dataset needs that is for
the binding's reviewer.

Smoke fetched 252,186,592 bytes (both bundles). The example binding's `snippets_commit` is the commit before
this snippet's own (a binding can't name the commit that adds it); a binding written by the worker names a
commit that contains the snippet.
