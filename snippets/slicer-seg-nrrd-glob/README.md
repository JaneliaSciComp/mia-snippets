# slicer-seg-nrrd-glob

Reads a set of 3D Slicer segmentation files (`.seg.nrrd`), each the labelmap of one 2D image, and presents it
as one label array: one file per z plane, or no z axis for a single file. A `.seg.nrrd` is an NRRD file whose
header names the segment of each stored label value (`Segment<N>_Name`, `Segment<N>_LabelValue`). Slicer numbers
the segments per file, in the order the annotator made them, so the same value can mean different classes in two
files. This snippet returns class ids fixed by the binding instead: every stored value is renamed through its
own file's header.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax: `*`,
  `?`, `[...]`; `*` also crosses `/`). A glob is expanded with `io.expand` and sorted by name; several URLs are
  concatenated in binding order, and a file named twice raises. A raw array bound with `image-tiles-glob` over
  `<dir>/*.TIF` and a label bound here over `<dir>/*.seg.nrrd` pair plane for plane when both globs match the
  same stems. The snippet cannot check that across arrays, so the binding's `reasons` must say why the names
  pair.
- **Files.** NRRD with attached data (`NRRD000x` magic, header, blank line, data), `dimension: 3`, `sizes: nx ny
  1`, an integer `type` of at most 32 bits, `encoding: raw` or `gzip`, and Slicer's `Segment<N>_Name`,
  `Segment<N>_LabelValue` and `Segment<N>_Layer` keys with every segment in layer 0. Every file of one array
  must have the first file's plane size; one that differs raises at `read`.
- **`segments`** (required, the only param). The segment names in class id order: the id returned for a name is
  its position in the list, from 1, e.g. `[Glycogen, Background]` gives 1 and 2. An entry may itself be a list
  of names that share one id (two spellings of one class). Names are compared exactly. Take them from the
  record's classes and check them against the headers (`harness describe <address>` prints a header's text).
- **Values.** 0 (a pixel of no segment) stays 0. A stored value whose segment is listed becomes the listed id;
  two segments of one file with the same name both become that id. A plane that holds the value of a segment
  not in `segments` raises, and so does a stored value that no `Segment<N>_LabelValue` names, so no label is
  dropped or guessed. A segment the header names but no pixel has needs no entry: Slicer files often carry an
  empty `Segment_3` or a class that does not occur in that image.
- **Axes.** `zyx`, or `yx` for one file. NRRD's first axis varies fastest, so `sizes: nx ny 1` is read as
  `(y, x)` with x the NRRD's first axis, which is the pixel column of the image Slicer segmented. Nothing is
  transposed or flipped. `space directions` must be axis-aligned and unmirrored and
  `Segmentation_ReferenceImageExtentOffset` must be `0 0 0`; otherwise the file raises, because the stored grid
  would not be the image's.
- **dtype.** Of the ids, not of the file: `uint8` for up to 255 ids, else `uint16`.
- **Access.** `plane`: one file is one z plane, and any region of it costs the whole file. A stored member or a
  Range-capable plain URL is read through `io.open`; a deflated member (or a server ignoring Range) raises
  `NeedsFetch` and is staged whole with `io.fetch`. `open_array` reads the first file's header only (the plane
  size) and inflates nothing; the other files are fetched when `read` asks for them, and the last decoded plane
  is kept.
- **Voxel size.** `None`. Slicer writes spacing 1 (nominally mm) for an image that carries no scale, so the
  header's `space directions` are not a pixel size.

## Known limits

- One plane per file. A volume segmentation (third size above 1) raises, as does a 4D file: Slicer writes one
  when segments overlap (several layers), which is not one id per pixel. Files of Slicer before 4.11, with one
  segment per layer and no `Segment<N>_LabelValue`, raise too.
- A labelmap cropped to its segments' extent (non-zero `Segmentation_ReferenceImageExtentOffset`) raises. A
  cropped file that keeps offset `0 0 0` is only caught by the harness's shape check against the raw.
- Whether the plane lies on the raw image as stored cannot be read from the file: the header names no image.
  The binding must check it on real data (see the example's `reasons`); square images hide a transpose.
- A plane's segment names are checked when it is read, not at open: with many files, a name that is missing
  from `segments` surfaces on the first read of a plane that uses it.
- NRRD `bzip2`, `ascii` and `hex` encodings, detached data files, `line skip` / `byte skip`, 64-bit and
  floating-point types raise. NRRD escapes (`\n`, `\\`) in a segment name are not decoded.
- Each file is read whole into memory, and over HTTP each deflated zip member costs two requests (local
  header, compressed bytes), made lazily per plane; the harness does not retry a `429`. zenodo.org answered
  `429 TOO MANY REQUESTS` after about 40 label files read at one per second while this snippet was written.

## Example

`examples/zenodo-18390286-muscle-glycogen-tem.yaml`: TEM images of human skeletal muscle (2048x2048 `uint8`
TIF, bound with `image-tiles-glob`) and their Slicer segmentations, in three zips: `test.zip` (92 pairs) and
`region.zip` (859) with seven sarcomere region classes, `glycogen.zip` (100) with Glycogen and Background. Raw
and label are `zyx` (92 | 859 | 100, 2048, 2048), label `uint8`. All 92 test headers and 20 of the 859 region
headers name the same seven segments with values 1 to 7. In `glycogen.zip` the stored values differ between
files: one file stores Glycogen as 1 and 3, an empty `Segment_3` as 2 and Background as 4, which this snippet
reads as 1 and 2 like the others. The example binding's `snippets_commit` is the commit before this snippet's
own (a binding can't name the commit that adds it); a binding written by the worker names a commit that
contains the snippet.
