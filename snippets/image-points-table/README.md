# image-points-table

Reads one image, or the points table that annotates it, for datasets that publish each image as its own item
with a text table of flagged points beside it. Written for the eHistology Kaufman Atlas items on Edinburgh
DataShare (`image.jpg` or `image.tif`, and `terms.txt`).

- **URLs.** Plain URLs only (no zip members, no globs).
  - **Raw:** one url, the image.
  - **Label:** two urls, the points table and the image it annotates, in either order: the snippet tells them
    apart by their first bytes (JPEG or TIFF magic is the image). The image gives the label its grid; only its
    header is read. The snippet cannot check that the two files belong together, so the binding's `reasons`
    must say why they do.
- **Image.** An 8-bit JPEG of 1 or 3 components, or a single-series, single-page `YX` / `YXS` TIFF. Shape and
  dtype come from the header at `open_array` (the JPEG frame header is found by walking the segments before it;
  about 1 MiB is fetched). Three samples are a `c` axis, `cyx` with c = R, G, B; one is `yx`. The first `read`
  fetches and decodes the whole file and keeps it, so the three `c` planes cost one decode. A JPEG that does not
  end with the end-of-image marker raises instead of decoding to grey below the cut. Pixels are the stored
  ones: an Exif orientation is not applied.
- **Points table.** Tab-separated text. The first line names the columns and must begin
  `Plate, Image, MK #, EMAP:ID, X, Y` (fields are stripped of spaces); a line of `=` and blank lines are
  skipped; every other line is one flag, with whole numbers in its `MK #`, `X` and `Y` columns. Anything else
  raises with the line number. The other columns (EMAPA id, descriptions) are not read.
- **Label array.** `yx`, `uint16`, the image's Y and X. 0 is background. A flag at table `X`, `Y` is the
  `scale` x `scale` image pixels starting at `x = X * scale`, `y = Y * scale`, and they hold the flag's `MK #`:
  the annotation number printed on the atlas plate, which `terms.txt` maps to an EMAPA term. Flags with the same
  `MK #` (one structure flagged in several places) share the id. Where two flags cover the same pixel the later
  row is kept. `MK #` must be 1..65535.
- **`scale`** (default 1). The number of image pixels per table pixel. The eHistology tables are not on the
  full-resolution grid of `image.jpg`: for Plate 38b image b the flags fit the anatomy at `scale: 2` and lie on
  empty background at 1. Nothing in the files states the factor, so it has to be checked against the image
  (draw the flags, or compare the largest X and Y with the image size) and the binding must say how. A flag that
  falls outside the image fails the open, which catches a scale that is too large but not one that is too small.
- **`count`**. The number of rows the table must have; another number fails the open. Use it: a table cut at a
  row boundary is otherwise read as a shorter table.
- **Access.** `plane` for both. Any region of the image costs the whole file. The label is made from the table
  held in memory; `plane` lets the harness search the whole label plane for flags when it aims crops.
- **Voxel size.** `None`: JFIF density and TIFF resolution tags describe print size.

## Known limits

- **Memory.** The decoded image is held whole: 48605 x 30091 x 3 is 4.4 GB (smoke peaked at 4.6 GB). A
  harness that searches the whole label plane for annotations allocates that plane too (2.9 GB as uint16, plus
  its masks).
- **DataShare URLs.** Bind `https://datashare.ed.ac.uk/server/api/core/bitstreams/<uuid>/content`, not
  `https://datashare.ed.ac.uk/bitstreams/<uuid>/download`. A HEAD of the latter is answered by the site's HTML
  page (Content-Length 4264 for every bitstream), so the harness reads only the first 4264 bytes of the file.
  The end-of-image check and `count` are there to catch this.
- Flags are points inside a structure, not its outline, and are tiny on a whole-slide image (`scale` x `scale`
  pixels). The snippet does not enlarge them.
- Ids are per image: `MK #` 83 on one plate is not the structure numbered 83 on another. The label says where
  the numbered flags are, not which EMAPA class a pixel has.
- An `MK #` that is not a whole number (if any plate has one) raises; such a table needs a new snippet.
- The TIFF path was tested on synthetic files only: the record names no `.tif` item. A TIFF is read whole with
  `tifffile`; pyramidal or tiled whole-slide TIFFs with several series are refused.
- CMYK or 12-bit JPEGs are refused. Exif orientation is ignored for image and table alike.
