# jpeg-bmp-tiles-glob

Reads a set of one-image files that are **JPEG or uncompressed 24-bit BMP** and presents it as one array: one
file per z plane, a colour file's samples as the `c` axis. The format is taken from each file's first bytes
(`FF D8 FF` or `BM`), never from its name. It was written for a set whose files are all named `.png`, most of
them JPEG and some BMP, with nothing in the names to tell the two apart, so no glob can bind the JPEGs alone.
`image-tiles-glob` and `jpeg-zip-glob` read the JPEGs of such a set and raise on the first BMP; no other snippet
decodes BMP.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax: `*`,
  `?`, `[...]`; `*` also crosses `/`). A glob is expanded with `io.expand` and sorted by name; several URLs are
  concatenated in binding order, and a file named twice raises.
- **Files.** JPEG, baseline or progressive, grey `(Y, X)` or three-sample `(Y, X, 3)` as
  `imagecodecs.jpeg_decode` returns it (YCbCr is converted to RGB by libjpeg). BMP with a `BITMAPINFOHEADER` or
  later (header size 40 or more), 24 bits per pixel, compression `BI_RGB` (none), bottom-up or top-down. Any
  other file raises, naming its first bytes; so does any other kind of BMP (palette, 16 or 32 bit, RLE, an
  OS/2 core header). Every file of one array must give the first file's shape and dtype, whichever format
  holds it; one that differs raises at `read`.
- **BMP decoding** is done here, with `numpy`, because the format is a header and the pixels: the pixel data
  starts at the header's offset, rows are padded to a multiple of 4 bytes, each pixel is stored B, G, R, and
  the first stored row is the bottom of the picture when the header's height is positive. The snippet returns
  R, G, B with row 0 at the top, the same layout a JPEG decodes to. Bytes after the pixel data are ignored (the
  example's BMPs end in 40 zero bytes that the header's file size does not count). On the example's files and
  on synthetic ones the result equals `imagecodecs.bmp_decode`.
- **Axes.** `zcyx` for colour files, `c` = R, G, B; `zyx` for grey JPEGs. With one file the `z` axis is dropped
  (`cyx`, `yx`). The z axis is a stack of independent images in name order, not depth.
- **Params.** None.
- **Values.** A BMP's stored bytes, unchanged. A JPEG's as libjpeg decodes them.
- **Access.** `plane`: one file is one z plane, and any region of it costs the whole file. A stored member or a
  Range-capable plain URL is read through `io.open`; a deflated member (or a server ignoring Range) raises
  `NeedsFetch` and is staged whole with `io.fetch`. `open_array` reads only the first file (shape, dtype); the
  other files are fetched when `read` asks for them. The last decoded file is kept, so reading the three `c`
  planes of one image decodes it once.
- **dtype.** `uint8` (a 12-bit JPEG would decode as `uint16` and then every file must be one). **Voxel size:**
  `None`; the JPEG density and the BMP pixels-per-metre fields describe print size (0 in the example's files).

## Known limits

- A set that mixes a lossy and a lossless format is not uniform: the JPEG planes carry compression noise and
  the BMP planes do not, and nothing in the array says which plane came from which. The format is knowable
  only from the file (in the example also from the member size: every BMP is 24,883,294 bytes).
- Shape and dtype are checked against the first file at `read`, file by file, not at open. A grey JPEG among
  colour files, or an image of another size, raises there, naming the file.
- A crop holds one `c` plane (the harness samples a plane per crop), so a colour image yields single-sample
  crops.
- Each file is read whole into memory and decoded whole (25 MB for a 3840x2160 BMP, twice that while the
  samples are moved to the front). No pyramid levels.
- An uncompressed BMP is large: a deflated 3840x2160 member costs 11 to 20 MB of the job's byte cap per plane
  in the example, against 1.6 to 4.5 MB for a JPEG.
- Over HTTP each deflated zip member costs a request for its bytes (two when its local header is longer than
  guessed), made lazily per plane.
- No labels: this snippet reads images only. A class given by the folder or the zip (the example) has no
  representation in the binding format; one array per class keeps them apart.
- PNG and TIFF are not read; a set of those, or of those mixed with JPEG, belongs to `image-tiles-glob`.
- Grey JPEGs, top-down BMPs, stored members, HTTP directory globs and plain URLs were exercised on synthetic
  data only; the example is colour JPEGs and bottom-up BMPs in deflated zip members.

## Example

`examples/figshare-24548953-caishi-cervical-ais-he.yaml`: CAISHI (figshare 24548953), H&E photomicrographs of
cervical biopsy sections in two zips, `Abnormal.zip` (4.3 GB, 1,231 images of adenocarcinoma in situ) and
`Normal.zip` (7.4 GB, 1,011 images of normal glands). Every member is deflated and named `<class>-<nnnn>.png`;
none is a PNG: 1,905 are baseline JPEGs and 337 (36 in `Abnormal.zip`, 301 in `Normal.zip`) are bottom-up 24-bit
BMPs, all 3840x2160 (the image header of every member was read). Bound as two raws, one per zip, each `zcyx`:
`(1231, 3, 2160, 3840)` and `(1011, 3, 2160, 3840)` `uint8`. Two BMPs and two JPEGs were read whole through the
snippet and compared with `imagecodecs`; the other members were not decoded. The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
