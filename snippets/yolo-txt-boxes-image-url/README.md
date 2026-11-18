# yolo-txt-boxes-image-url

Reads **YOLO detection label files** as label planes: a text file per image with one row per object,
`class x_center y_center width height`, the four numbers being fractions of the image's width and height
(`0 0.855797 0.128209 0.057566 0.060055`). Each file is rasterised (`uint16`, filled boxes, 0 background) on the
pixel grid of **the image the binding names for it by URL**.

The txt holds no image size, so a YOLO plane cannot be made without its image. The other box snippets
(`jpeg-xml-boxes-numbers`, `tiff-jpeg-voc-zip-glob`) read XML and find the grid in `<size>` or in the JPEG of the
same name; this one is for YOLO txt, and for files that cannot be paired by name: a repository such as Mendeley
Data serves every file under an opaque id, so the binding writes each pair out.

- **URLs.** `urls` are the label files, `params.images` the image of each, in the same order and of the same
  number. Each is a plain URL or a zip member `<zip url>::<member>`. **Globs are refused**: the two lists pair
  by position, and two globs would pair by sort order without anything to check it against. A URL named twice
  in either list raises.
- **Images** are JPEG. Only the header is read (the first 64 KiB, and the whole file only when the
  start-of-frame segment lies beyond that), for the height and width; nothing is decoded. Give the URLs of the
  raw array the labels pair with: `io` caches them, so the label costs no bytes beyond the raw's.
- **Axes.** One file is `yx`; several are `zyx`, one file per z plane in the order of `urls`. That is what
  `image-tiles-glob` does with the images (`cyx` for one file, `zcyx` for several), so the two pair. Every
  image of one array must have the first one's shape; another raises at `read`, naming the file.
- **Rows.** Exactly 5 whitespace-separated fields; blank lines are skipped, and a UTF-8 BOM and CRLF are
  accepted. The class is a whole number. `x_center`, `y_center` must be in 0..1 and `width`, `height` in
  (0, 1] (YOLOv5's own loader refuses labels outside 0..1). Anything else raises with the line number: a row
  with more fields (a segmentation polygon, or a prediction with a confidence) is not read as a box. A file
  that is empty or blank is an **all-background plane**.
- **Boxes** (`boxes` and `classes`, both required):
  - `classes`: every class id the files use. A row with an id that is not listed raises, so a class the record
    does not know is found rather than dropped. `0` and `'0'` are both taken.
  - `boxes: instance`: each box is filled with its 1-based row number in its file (blank lines not counted);
    ids restart in every file. `boxes: semantic`: each box is filled with the 1-based position of its class id
    in `classes` (`classes: [0, 1]` paints class 0 as 1 and class 1 as 2).
  - **Which pixels.** The box's corners are `(x_center ± width/2) * X` and `(y_center ± height/2) * Y`, clipped
    to the image, and the box covers the pixels whose **centres** lie in it (each corner rounded to the nearest
    pixel edge). The corners are not moved outwards as `jpeg-xml-boxes-numbers` does with XML corners: a YOLO
    txt stores six decimals of a fraction, so a corner that was a whole pixel comes back as, say, 1984.9999 or
    1985.0001, and outward rounding would add a row or column on that noise. The result is within half a pixel
    of the stored box on every side. A box that covers no pixel centre raises.
  - Boxes are painted largest first, so where two overlap the smaller one is on top (file order breaks ties).
  - **Exif orientation.** A label file *with* rows whose JPEG has an Exif `Orientation` other than 1 raises:
    the stored pixels (which the raw snippet returns) are turned against the displayed picture, and the txt does
    not say which of the two the boxes were drawn on. A file without rows is not affected.
- **The labels are boxes, not masks**: where a box was drawn, not the object's outline. Whether a binding may
  call that `instance` or `semantic` is a human's decision (see the example binding's `reasons`).
- **Access.** `plane`: a region of a plane costs its txt and the head of its image. Both are read through
  `io.open`; a deflated zip member or a server without Range raises `NeedsFetch` and is staged whole with
  `io.fetch`. `open_array` reads the first txt and the first image's header; the other files are read when
  `read` asks for their plane, and their rows and shape are checked then. The last painted plane is kept.
- **dtype.** `uint16` (more than 65,535 rows in a file raise). **Voxel size:** `None`; the txt has none and
  JPEG densities are print sizes.

## Known limits

- The pairing of a txt with its image is the binding's word; the snippet cannot check it. A wrong pair of the
  same shape gives boxes in the wrong place, so `reasons` must say where the pairing comes from.
- JPEG images only: a PNG or TIFF image raises. No segmentation (polygon) or oriented-box rows, no confidence
  column.
- No class names: YOLO txt has ids only (the names live in a `data.yaml` or `classes.txt` the snippet does not
  read).
- An image whose txt is missing altogether (unlabelled, as opposed to an empty txt) cannot be bound as a plane;
  leave that image out of the raw this label pairs with.
- One URL per file in the binding: thousands of pairs make a long binding.
- Tested on plain HTTPS URLs. Zip members, `NeedsFetch`, several planes, the refusals and a header beyond
  64 KiB were exercised on synthetic files only.
- A server that answers an error page without an error status hands the snippet that page as the label file.
  A page that is not YOLO rows is refused (`line 1 has 2 fields, not the 5 ...`); an empty answer would pass
  as a txt without boxes. The cure is in the binding: name the file where it is stored, not a redirecting
  download link that can fail that way (Mendeley Data's `public-files/.../file_downloaded` did, once, with
  `{"err":"Unhandled error"}`).

## Example

`examples/mendeley-457x9c7rp3-fossil-diatom-eucampia-yolo.yaml`: one 2400x2400 RGB brightfield tile of a
diatom slide (Mendeley Data 457x9c7rp3, *Eucampia antarctica* in Southern Ocean sediment) and its YOLO txt,
one row of class 0. The raw is read by `image-tiles-glob` (`cyx`, 3x2400x2400); the label is `yx` 2400x2400 with
one box of id 1 over y 236..380, x 1985..2123 (144x138 px), and in the harness's overlay of two crops the
box encloses the diatom valve on all four sides. The record names only this pair (the other 4,201 tiles have
no per-file URL in it), so the binding has one plane. Both files are bound by the S3 object their Mendeley
download links redirect to. The example binding's `snippets_commit` is the commit
before this snippet's own (a binding can't name the commit that adds it); a binding written by the worker names
a commit that contains the snippet.
