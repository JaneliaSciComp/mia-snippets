# jpeg-png-zip-rgb-glob

Reads a set of RGB photographs, members of a zip, as one array: one image per z plane, red, green and blue as
the c axis (`zcyx`). It is for sets where **the file name does not say what the file is**: each file is read
as a JPEG or as a PNG by its first bytes, so a PNG saved under a `.jpg` name is read as the PNG it is, and both
kinds can sit in one array. `jpeg-zip-glob` and `jpeg-zip-canvas-glob` raise on such a member (`is not a
JPEG`); `image-tiles-glob` reads it but exposes a PNG's alpha as a fourth c plane, which no JPEG has.

**Nothing is added to the pixels.** An array has one shape, so every image of an array must have the first
image's height and width. A set that mixes sizes is bound as **one array per size**, the files of each chosen
by file number with `numbers`; there is no canvas and no fill (compare `jpeg-zip-canvas-glob`, which is for
sets where nearly every file has its own size).

- **URLs.** Each URL names one zip that holds images: a plain `<zip url>`, or `<outer zip url>::<inner zip
  member>` for a zip nested in another. An outer member glob (`<outer zip url>::*/images.zip`) is expanded with
  `io.expand` and stands for every inner zip it matches. Several URLs are concatenated in binding order, and a
  zip named twice raises.
- **`glob`** (required) picks the images inside each zip: an `fnmatch` pattern over member names (`*` also
  crosses `/`), matches sorted by name within a zip, as strings (`Image100.jpg`, `Image1000.jpg`, ...,
  `Image101.jpg`). Folder entries are left out. A zip with no match raises. The pattern is a param and not a
  `::<glob>` part of the URL so that the URL is the zip: the harness's `fetch` and `stage` fetch every binding
  URL, and one zip is one download, where a member glob would be one request per image. A URL with two `::`
  raises with that explanation. Anchor the pattern at the image folder: zips made on macOS carry
  `__MACOSX/.../._name.jpg` sidecars that a bare `*.jpg` matches, and those raise at read (`neither a JPEG nor
  a PNG`).
- **Files.** Told apart by their first bytes, whatever the name:
  - JPEG (`FF D8 FF`), baseline or progressive, three samples, as `imagecodecs.jpeg_decode` returns it (YCbCr
    is converted to RGB by libjpeg). A grey or four-sample (CMYK) JPEG raises.
  - PNG, 8 or 16 bit, RGB or RGBA. **Alpha is dropped only when it says nothing:** an RGBA PNG whose alpha is
    the maximum (255, or 65535) at every pixel gives its R, G, B samples; one with any other alpha value
    raises, naming the file and the minimum, because its transparent pixels would be read as if they were
    photograph. An RGB PNG with a transparent colour (`tRNS`) decodes with an alpha sample and is treated the
    same way. Grey, grey+alpha, palette and 1/2/4-bit PNGs raise.
  - Anything else raises, with the file's first bytes.
- **`numbers`** (optional) keeps only the matches whose file number is listed: numbers and inclusive `'a-b'`
  runs, e.g. `['1-1059']` or `[3, 7, '60-61']`. A file's number is the last run of digits in its file name
  (folders not counted), so `Image01.jpg` is 1 and `s3_Image07.jpg` is 7. Every number listed must be a file's
  (a file that went missing raises instead of shifting z), two matches with the same number raise, and so does
  a match with no digits in its name. The order of the planes stays the name order, whatever the order of the
  list. Without `numbers` the array holds every match and names need no digits.
- **`count`** (optional) is the number of files the array must end up with; open fails on any other number.
- **One shape and dtype per array.** The first image gives them. Another image with a different height, width
  or dtype raises when `read` reaches it, naming the file, its size and the first file's. JPEG and PNG files of
  the same size and dtype share an array freely.
- **Axes.** `zcyx`, c = 0, 1, 2 for red, green, blue; `z` is there for one image too. The z axis is a stack of
  independent photographs in name order, not depth.
- **Access.** `plane`: an image is decoded whole whatever the region. The last image decoded is kept, so its
  three c planes cost one decode. A zip that `io.open` can read by range (a plain URL, a stored member of an
  outer zip) is read that way: its directory at open, each image's bytes when `read` asks (in `io`'s 1 MiB
  blocks), deflated members included, which `zipfile` inflates and CRC-checks. A deflated inner zip, or a
  server ignoring Range, raises `NeedsFetch` and the whole zip is staged with `io.fetch` at open.
  `open_array` reads every bound zip's directory and decodes the first image (shape, dtype).
- **dtype.** What the decoder returns: `uint8` (`uint16` for a 16-bit PNG). **Voxel size:**
  `None`; a JPEG's JFIF density and a PNG's `pHYs` are print sizes.

## Choosing params

- `glob`: one folder per array where the folder means something (a class, a split), anchored at the folder.
- `numbers`: needed only when the matches are not all one size. The sizes are in the file headers, and the
  snippet does not look, because that means reading every image at `open` (the images' headers are spread over
  the whole zip, so it costs the zip's size on every job). Read every member's header once (`harness describe
  '<zip url>::<member>'` per file, or download the zip and decode the headers), group the file numbers by
  size (whatever the format), and bind one array per size. A wrong list cannot give a wrong plane, only an error
  naming the first file of another size.
- `count`: from the same listing, so that a zip that later gains or loses a file is noticed.

## Known limits

- The size groups are not discovered: someone has to read every header once and write `numbers`. A set with
  many sizes needs as many arrays (or `jpeg-zip-canvas-glob`'s canvas, for JPEG-only sets).
- Files are chosen by number only; a set whose names carry no number, or the same number twice among the
  matches, can be split only by glob.
- JPEG is lossy and PNG is not: an array that holds both has planes with and without compression noise, and
  nothing in the array says which plane came from which format.
- The shape, dtype, format and alpha checks happen at `read`, image by image, not at `open` (except for the
  first image).
- A PNG with any non-opaque alpha is refused, not read; there is no param to drop a meaningful alpha.
- Colour management is ignored: samples are returned as stored, whatever `sRGB`, `gAMA` or `iCCP` chunk or
  ICC profile the file carries. EXIF orientation is ignored too (pixels are returned in stored order).
- Each image is decoded whole (`3 x height x width` bytes per plane for `uint8`); reading a z range stacks
  that many planes.
- Read by range, an image costs the 1 MiB blocks it lies in; reading every plane costs the zip's size.
- A deflated inner zip is fetched whole at open even when one image is wanted, since its directory is at its
  end.
- No labels: this snippet reads images only. A class given by the folder name (the example) has no
  representation in the binding format; one array per folder keeps the classes apart.
- 16-bit PNGs, alpha that is not opaque, grey and palette files, several zips in one array, nested zips and a
  server without Range were exercised on synthetic data only; the example is 8-bit JPEGs and opaque 8-bit
  RGBA PNGs in a plain zip URL with deflated members.

## Example

`examples/dataverse-o2wvwa-tanzania-malaria-blood-smears.yaml`: Harvard Dataverse doi:10.7910/DVN/O2WVWA,
`Thick_Infected.zip` (datafile 10577427, 835.5 MB), 1,139 Giemsa-stained thick blood smear photographs, all
named `Thick_Infected/Image<N>.jpg`. Reading every member's header showed `Image01.jpg` to `Image1059.jpg` to
be 8-bit three-component JPEGs of 2160x3840 and `Image1060.jpg` to `Image1139.jpg` to be 8-bit RGBA PNGs of
922x1640 with alpha 255 at every pixel. Bound as two raws on the one zip and glob: `numbers: ['1-1059']`,
`zcyx (1059, 3, 2160, 3840)`, and `numbers: ['1060-1139']`, `zcyx (80, 3, 922, 1640)`, both `uint8`.

All 1,139 planes were read through the snippet and compared with a separate decoding of the downloaded zip
(JPEG with `imagecodecs`, PNG with `pypng`): every plane equals its file's RGB samples, and every member is in
exactly one array. From dataverse.harvard.edu (which redirects to dvn-cloud-iqss.s3.amazonaws.com), `harness
smoke` fetched 5.0 MB and `harness crop --n 4` 7.1 MB; 40 of 40 requested crops were made with none rejected,
and 8 of 8 from the PNG array bound alone.

The dataset's three other zips are not in the example because the record has no URL for them. Their headers
were read as well, and the 922x1640 files of `Thin_Uninfected.zip` (5 JPEG and 154 PNG, chosen by `numbers`)
were read through the snippet as one `(159, 3, 922, 1640)` array and compared the same way, which is the
mixed-format case on real data.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
