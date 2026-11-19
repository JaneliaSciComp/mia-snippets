# wsi-czi-svs-rgb

Reads one brightfield **whole-slide image** at a plain HTTP(S) URL (S3 included) as `cyx` `uint8` with
`c` = red, green, blue. The file is one of two kinds, told apart by its first bytes:

- a tiled colour **Zeiss CZI** mosaic (`ZISR…`), as an Axio Scan writes it: Bgr24 tiles compressed with JPEG XR;
- a tiled 8-bit RGB **TIFF** (`II*`/`MM*`, BigTIFF included), such as an **Aperio SVS**: its first page.

Both are read **tile by tile by byte range**. At open only headers are read; a `read` fetches and decodes the
tiles its region touches and nothing else. A server without Range support is staged whole (access `file`),
otherwise access is `chunk`.

`urls` is exactly one file: one slide is one array, because slides differ in shape and cannot be stacked. A glob
url is accepted only when it matches one file. A set of slides is one binding entry per slide.

## CZI

No CZI library is on the harness allowlist, so `snippet.py` parses the file itself (`struct`), as
`czi-mosaic-imagej-roi` does for grey tiles: the `ZISRAWFILE` header, the `ZISRAWDIRECTORY` subblock directory,
the `ZISRAWSUBBLOCK` segments of the tiles a read touches, and the `ZISRAWMETADATA` XML for the pixel size.

- **What is read.** Every full-resolution subblock (tile) of one scene, placed on one canvas at the X and Y
  start the subblock directory gives it, minus the smallest X and Y start of those tiles. `shape` is
  `(3, height, width)` of the tiles' bounding box. Pyramid subblocks (stored size smaller than the extent they
  cover) are skipped.
- **Stitching is placement only.** Nothing is registered, blended or resampled. Where tiles overlap, the pixel
  of the tile with the higher M (mosaic) index is returned, the order in which libCZI draws them. A pixel no
  tile covers reads 0 (black).
- **Sample order.** The file's pixel type is Bgr24; the JPEG XR codec (`imagecodecs.jpegxr_decode`) returns the
  decoded samples as red, green, blue, and that order is `c`. So `c` means the same for a CZI and for a TIFF.
- **Voxel size.** `Metadata/Scaling/Items/Distance[@Id=X|Y]/Value`, which CZI states in metres, times 1e9.
  `None` when the file has no such element.
- **`scene`** (param, optional). A slide scanned as several regions holds several scenes (S). Such a file is an
  error at open, which lists the scenes, unless `scene` names the one to read; the canvas is then that scene's
  bounding box. A file with one scene needs no param.
- **Cache.** The last 8 decoded tiles are kept (an Axio Scan tile is 2236 x 2236 x 3 bytes, 15 MiB).
- **Refused at open**, with the reason: tiles that are not all Bgr24 with JPEG XR compression (grey tiles are
  `czi-mosaic-imagej-roi`'s; uncompressed, JPEG, LZW and zstd colour tiles are not read); tiles that span more
  than one channel, z plane, time point or any dimension other than X, Y, M and S; a multi-part file.

## TIFF / SVS

- **What is read.** The first page (IFD) only, which in an Aperio SVS is the full-resolution image. The reduced
  pyramid levels, thumbnail, label and macro pages are never parsed. `shape` is
  `(3, ImageLength, ImageWidth)`.
- **Tiles** are decoded by `tifffile` (`TiffPage.decode`, with the page's JPEGTables), so any compression it
  decodes is read (JPEG in the tested file; a YCbCr JPEG comes back as RGB). An edge tile is cut at the image
  border. A tile stored with byte count 0 reads 0. Up to 512 decoded tiles (at most 128 MiB) are kept.
- **Voxel size.** The `MPP = <µm per pixel>` field of the Aperio ImageDescription, times 1000, for y and x.
  `None` when the description has no MPP field; the TIFF resolution tags are not used.
- **Refused at open**, with the reason: a first page that is in strips, is not 8-bit, does not have exactly 3
  interleaved samples (RGB or YCbCr), or has depth; `params.scene` on a TIFF.

## Known limits

- One file per array (see above); no label form.
- 16-bit colour (Bgr48), fluorescence or multi-channel CZIs and CZIs with uncompressed or zstd colour tiles are
  not read.
- The pyramid stored in either file is not used: every read is at full resolution.
- Scanners store what they did not scan inside the bounding box in their own way. In the tested Axio Scan CZI
  every position of the 25 x 28 tile grid has a tile, and what the scanner did not scan is black (0, 0, 0), not
  white: about a third of the 700 tiles are entirely black, and tiles at the border of the scanned region are
  partly black. The snippet returns them as stored.
- `scene` was not exercised on real data: the tested files have one scene each.

## How it was checked (S-BIAD1343)

- SVS `iCAIRD-1082_Leica-089.svs`: three regions (interior, the bottom-right corner with cut edge tiles, the
  origin) and a stepped single-channel read are identical to the same regions read through tifffile's own
  store for that page (`TiffPage.aszarr`).
- CZI `iCAIRD -1083-HE-ZeissAxioZ1-Grant-07_06_2021.czi`: a 1200 x 1200 region over the corner where four tiles
  meet, averaged 3 x 3, against the file's own 3x pyramid subblock of the same area: correlation 0.96 (0.42 with
  x and y exchanged, 0.47 shifted by 24 pixels). Sample order: the tile streams are tagged 24bppBGR and decode
  to means of (205, 129, 187) over tissue, red > blue > green as eosin-stained tissue is.
