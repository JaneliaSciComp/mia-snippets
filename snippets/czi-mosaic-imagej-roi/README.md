# czi-mosaic-imagej-roi

Reads one tiled **Zeiss CZI** mosaic at a plain HTTP(S) URL (S3 included) as a stitched `cyx` image, and, as its
label form, a set of **ImageJ `.roi`** files filled onto the pixel grid of such a mosaic as instance ids.

No CZI or ImageJ ROI library is on the harness allowlist, so `snippet.py` parses both itself (`struct`): the
`ZISRAWFILE` header, the `ZISRAWDIRECTORY` subblock directory, the `ZISRAWSUBBLOCK` segments of the tiles a read
touches, the `ZISRAWMETADATA` XML for the pixel size; and the 64-byte big-endian `.roi` header with its
coordinate block.

## Raw form: `urls: [<one .czi>]`, no params

- **What is read.** Every full-resolution subblock (tile) of the file, placed on one canvas at the X and Y start
  the subblock directory gives it, minus the smallest X and Y start of all tiles. `axes` `cyx`; `c` is the
  directory's C index in ascending order (the order of the XML's channel list); `shape` is the bounding box of
  the tiles; `dtype` is the tiles' pixel type (Gray8 `uint8`, Gray16 `uint16`, Gray32Float `float32`).
- **Stitching is placement only.** Nothing is registered, blended or resampled. Where tiles overlap, the pixel of
  the tile with the higher M (mosaic) index is returned, the order in which libCZI's tile accessor draws them. A
  pixel no tile covers reads 0.
- **Voxel size.** `Metadata/Scaling/Items/Distance[@Id=X|Y]/Value`, which CZI states in metres, times 1e9. `None`
  when the file has no such element.
- **Access `chunk`.** A read fetches and decodes only the tiles that intersect the region, each by one byte
  range, and keeps the last 16 decoded tiles. At open only the header, the directory and the XML are read. A
  server without Range support is staged whole (access `file`).
- **Compression.** Uncompressed, zstd0 (one zstd frame) and zstd1 (a small header, then one zstd frame; its
  flag for 16-bit pixels stored as low bytes then high bytes is honoured).
- **Refused at open**, with the reason: tiles that span more than one scene, z plane, time point or any dimension
  other than X, Y, C and M; JPEG, LZW or JPEG XR tiles; colour or complex pixel types, or channels of different
  pixel types; a multi-part file. Pyramid subblocks (stored size smaller than the extent they cover) are skipped.

## Label form: `urls: [<dir>/::*.roi, <its .czi>]`, `params: {skip_lines: true}` when the set holds lines

The urls are any number of `.roi` files (a glob over an HTTP directory index, or listed one by one; they are
recognised by the `.roi` ending of the url path) and exactly one CZI, in any order. Only the CZI's header,
directory and XML are read (for the shape and the voxel size), never its pixels.

- **The array** is `yx` `uint16` with the CZI mosaic's y and x shape and voxel size. A pixel holds the id of the
  ROI whose outline encloses the pixel's centre, 0 where none does. An ROI's **id is the 1-based position of its
  file in the name-sorted list of all `.roi` urls** (lines included), so ids identify files and are not
  consecutive when lines are skipped. Where two ROIs overlap, the one later in that list is kept.
- **Coordinate frame.** ROI coordinates are taken as pixel coordinates of the stitched mosaic as the raw form
  returns it: x to the right and y down from the corner of the canvas, an integer coordinate being a pixel
  corner (ImageJ's convention for area selections). The `.roi` files do not say which image they were drawn on;
  see "Tested on" for how this was checked.
- **ROI types.** Freehand, polygon and traced outlines (float coordinates when the file has them, else the
  int16 ones) are filled by the even-odd rule; a plain rectangle is filled; an oval is the pixels whose centres
  lie in the ellipse inscribed in its bounding box. Composite (shape), rounded-rectangle, text, arrow, image and
  rotated-rectangle ROIs are refused.
- **`skip_lines`.** A straight line, polyline, freehand line, angle or point ROI encloses no pixels. Such a file
  is an error unless the binding sets `skip_lines: true`, which leaves it out of the label (its id stays
  unused). Use it when the ROI set mixes outlines with measurement lines, and say so in `reasons`.
- **Access `plane`.** Every ROI file is read on the first `read` (one small file each; on a slow server this is
  the cost of the label: 3 to 10 s per file from `ftp.ebi.ac.uk` in testing) and kept as one mask per ROI.
- The ROI header's channel/slice position is ignored: the label is one `yx` plane for all channels.

## Known limits

- Tested on real data: zstd1 Gray8 tiles without byte packing, one scene, 4 channels x 25 tiles; sub-pixel
  freehand ROIs and straight lines. The uncompressed, zstd0, Gray16 (and its zstd1 byte packing), Gray32Float,
  rectangle, oval and integer-coordinate polygon paths were run on synthetic input only.
- Tiles are placed at integer pixel positions from the directory. ZEN's own stitching ("Stitching" processing)
  is not reproduced; a file that needs it shows seams in the overlaps.
- The filled outline is close to, but not pixel-for-pixel, ImageJ's own mask of the same ROI (ImageJ's filler
  rounds polygon edges slightly differently); the difference is confined to outline pixels.
- Scenes are not separated: a multi-scene file is refused rather than read as one huge canvas.

## Tested on

`s-biad3159-llc-tumor-tcell-roi-confocal` (examples/): `Mouse_2_CD8.czi`, 256 subblocks of which 100 are
full-resolution (4 channels x 25 tiles of 2048 x 2048, Gray8, zstd1) on a 5 x 5 grid with a pitch of 1843 or
1844 pixels, stitched to 4 x 9421 x 9421, 103.58 nm per pixel, the SizeX/SizeY the file's XML states; and its
`Mouse_2_CD8 RoiSet/` folder of 74 `.roi` files: 37 sub-pixel freehand outlines (111 to 427 points) and 37 straight lines (13 to 785 pixels long),
so the label has 37 ids and `skip_lines: true`.

Frame check on the first 12 outlines: the mean intensity inside the outlines at the positions the files give is
15.4 (channel 0, AF555-T1) and 14.1 (channel 3, DAPI-T3); with every outline moved 60 pixels up, down, left or
right it is 7.6 to 8.5 and 8.5 to 11.2, and with x and y exchanged 2.4 and 4.4. At most 1 of the 12 outlines is
brighter in channel 0 at a moved position than at its own. So the outlines sit on stained cells in the
stitched-mosaic frame, with no flip, transpose or offset of the order of a cell.
