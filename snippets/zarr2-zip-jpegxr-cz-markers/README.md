# zarr2-zip-jpegxr-cz-markers

Two forms, chosen by the urls:

- **Raw**, one url: a Zarr v2 array whose directory is inside a zip, `<zip url>::<array dir>` (or its
  `.zarray` member), with JPEG XR chunks (compressor id `imagecodecs_jpegxr`). Presented in storage order as
  `yx`, or `yxc` when the array has a trailing sample dimension (RGB: 3).
- **Label**, two urls: a Zeiss ZEN (blue) graphics file `.cz` (a zip member or a plain url), then the raw form's
  url. The `.cz` is a `GraphicsDocument` XML; its `Marker` elements (points) or `Rectangle` elements are drawn
  as instance ids on the array's `yx` grid. Only the array's `.zarray` is read for a label, never a chunk.

No globs: one array, or one `.cz` and its array, per binding entry.

## Raw

- `.zarray` gives shape, chunks, dtype and fill value. Chunk members are `<array dir>/<i>.<j>[.0]` (or with the
  `dimension_separator` the file names); the zip's member list says which exist, and a chunk without a member
  reads as `fill_value`.
- Each chunk is one JPEG XR image of the full chunk shape (Zarr v2 pads edge chunks), decoded with
  `imagecodecs.jpegxr_decode`. A chunk that decodes to another shape or dtype is an error. The codec is lossy
  (level 0.6 in S-BIAD1165); the snippet returns what the decoder gives.
- `access` is `chunk`. A stored member is read by range; a deflated one (S-BIAD1165) is staged whole with
  `io.fetch`. Decoded chunks are kept up to 256 MiB per array (three 5000x5000x3 chunks), most recently used.
- `voxel_size_nm` is `None`: these arrays carry no `.zattrs`.
- Refused: anything but rank 2 or 3, chunks that split the sample dimension, filters, `order: F`, any other
  compressor (use `zarr2-http` for blosc/gzip/zlib/zstd arrays at plain urls).

## Label

- `element` (default `marker`): each `Marker`'s `Geometry/X`, `Geometry/Y` marks the pixel that holds the point,
  `(floor(Y), floor(X))`. With `radius: r` a disc of r pixels around that pixel is drawn instead; that is a
  drawing choice for visibility, the file gives no extent.
- `element: rectangle`: each `Rectangle`'s `Geometry` (`Left`, `Top`, `Width`, `Height`) is filled after turning
  it about its centre by `Attributes/Rotation` degrees, clockwise as seen with y down. A pixel belongs to it when
  its centre is inside.
- Ids are the 1-based position of the element among the elements of its kind, in file order; where elements
  overlap the later one wins. dtype is uint16 (uint32 above 65535 elements). 0 is "nothing drawn here".
- Coordinates are taken as pixels of the array, x right and y down from its corner, with no offset or scale.
  An element off the grid (a marker outside it, a rectangle whose centre is outside it) is an error at open: the
  file was not drawn on that image.
- `voxel_size_nm` is the file's `Scaling` (`Distance` X and Y, metres per pixel), when it has one.
- `access` is `chunk`: any box is computed from the element list. Reading a whole plane of a whole-slide grid
  allocates it (tens of GB), so read boxes.

## Checked on S-BIAD1165

- All 22 `.cz` files that have an array in `batch1.zip` open against it: every marker (1460 in all) and
  every rectangle (65) lies on its grid. With x and y exchanged, 199 markers would be off their grids.
  `1-960-Temporal_AT8.cz` has no array in either zip.
- Rotation sense: with the clockwise reading every marker of every file lies inside one of its file's
  rectangles; with the opposite sense 368 of the 1460 fall outside (99 of the 288 of 1-102). The rectangles
  are the regions the annotators worked in, so the clockwise reading is the file's.
- Pixels, on 1-102-Temporal_AT8: 200x200 boxes of the raw around the first 24 markers, read through this snippet,
  each show a neuron cell body ringed by brown AT8 deposit with the marker on it, in the expected colours (brown
  DAB, blue counterstain), so sample order is RGB. Mean blue-channel darkness in a 48 px box is 146.8 at the
  markers, 107 to 116 at the same positions moved 300 px in x or y, and 107.9 with x and y exchanged.

## Limits

- Marker and Rectangle elements only; any other ZEN graphic (polygon, circle, line) is ignored, not drawn.
- The sub-pixel convention (whether ZEN counts pixel centres at .0 or .5) is not known; markers were clicked
  on objects tens of pixels wide, so it could not be told from the data and does not matter at that scale.
- A point label is one pixel per object: sparse on a whole-slide grid, so most random boxes read as all 0.
- A chunk is the smallest read unit: any box costs the whole 5000x5000 chunk(s) it touches (about 0.3 to 5 MB
  over the network each in S-BIAD1165, 75 MB decoded).
