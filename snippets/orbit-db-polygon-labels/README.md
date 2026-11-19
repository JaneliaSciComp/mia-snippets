# orbit-db-polygon-labels

Reads the manual annotations that [Orbit Image Analysis](http://www.orbit.bio) keeps in its SQLite database
(`orbit.db`) for **one slide**, and fills them onto that slide's full-resolution grid: `yx`, `uint16`,
0 = not annotated, every outline its own id.

## urls

Two, in any order:

- the database (`<zip>::orbit.db` or a plain url), fetched whole (46.8 MB here);
- the slide's TIFF, the one url ending in `.tif`/`.tiff`. It gives the grid: the size of the first series'
  full-resolution level, and the pixel size when the resolution tags carry a physical unit (inch or
  centimetre). Only the header is read when the file can be read by range; a deflated zip member is fetched
  whole (the raw reader fetches the same file, so the job's cache serves it once). No pixel is decoded.

No globs and no params: one binding entry per slide.

## How a slide finds its annotations

`RAW_DATA_FILE.FILENAME` equals the TIFF's file name (the folder is ignored; if two rows share a name, the
one whose `FILESIZE` is the TIFF's length is taken). A `FILESIZE` that differs from the TIFF's length is an
error: the database annotated another file. `RAW_ANNOTATION` rows with that `RAW_DATA_FILE_ID`, ordered by
`RAW_ANNOTATION_ID`, are the slide's annotations, and **an annotation's id is its 1-based position in that
order**, so ids are stable but not consecutive when rows are left out.

## The blobs

`RAW_ANNOTATION.DATA` is a Java Object Serialization stream (magic `aced0005`): a 4-byte block (the colour),
one `com.actelion.research.orbit.imageAnalysis.models.PolygonExt` (a `java.awt.Polygon` with `npoints`,
`xpoints`, `ypoints`, plus `scale`, `closed`, `inverse`, `onlyPoints`, `viewPortOffs`), and an 8-byte block
(two ints: Orbit's annotation subtype and group). The snippet walks the stream grammar itself and builds only
dicts, strings and numpy arrays. Nothing named in a blob is imported or instantiated; classes with custom
write methods, externalizable classes, enums, proxies and exceptions are refused.

- **Coordinate frame.** Points are stored in the zoom the outline was drawn at: `scale` is that zoom in
  percent, and a full-resolution pixel is `point * 100 / scale`. (In the tested dataset `scale` runs from 1.75
  to over 200, and raw points reach x = 46453 on a slide 26880 pixels wide.) x runs right and y down from the
  slide's corner. A pixel is labelled when its centre lies inside the polygon (even-odd rule).
- **Which annotations.** Only subtype 0, Orbit's plain annotation, is filled. Subtypes 1 to 3 (ROI, exclusion,
  inclusion; names from Orbit's source as I remember them, not read from the file) outline regions or objects
  set aside, not objects of the class, and are left out, as are outlines of fewer than 3 points. A blob without
  the trailing block counts as subtype 0.
- **Overlaps.** Outlines are filled in id order, so where two overlap the higher id stays.
- Anything else is an error rather than a guess: a shape that is not a `PolygonExt`, `inverse`, `onlyPoints`,
  an open polygon, a non-zero `viewPortOffs`, a scale that is not positive.

## Limits

- One class: every plain annotation is an instance of the same thing. The database does not name the class
  (`DESCRIPTION` is `Annotation 1/<n>`), so the binding's `label_key` says what it is.
- Coverage is whatever the annotator drew. In the tested slide only one of the two kidney sections is
  annotated, so 0 means "not annotated", not "background".
- Ellipses, rectangles and other Orbit shapes are not handled (none occur in the tested database).
- `access` is `plane`: the database is fetched whole and the slide's outlines are all parsed when the array is
  opened, so no read costs further bytes. A region is filled from the polygons that touch it; a whole plane of a
  large slide is a large array (uint16, 757 MB for the tested 14080x26880 slide).
