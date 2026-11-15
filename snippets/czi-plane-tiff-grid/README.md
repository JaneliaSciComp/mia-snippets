# czi-plane-tiff-grid

Reads one 2D greyscale **Zeiss CZI** image at a plain HTTP(S) URL (S3 included) as `yx`, and, as its label form,
one single-page integer **TIFF** placed on the pixel grid of such a CZI.

No CZI library is on the harness allowlist, so `snippet.py` parses the ZISRAW container itself (`struct`): the
`ZISRAWFILE` header, the `ZISRAWDIRECTORY` subblock directory, the `ZISRAWSUBBLOCK` segment of the one image, and
the `ZISRAWMETADATA` XML for the pixel size.

## Raw form: `urls: [<one .czi>]`, no params

- **What is read.** The file's one full-resolution subblock: `axes` `yx`, `shape` from its X and Y sizes, `dtype`
  from its pixel type (Gray8 `uint8`, Gray16 `uint16`, Gray32Float `float32`). The dtype is the subblock
  directory's, not the XML's: the tested file says `Gray8` in its XML and holds Gray16.
- **Voxel size.** `Metadata/Scaling/Items/Distance[@Id=X|Y]/Value`, which CZI states in metres, times 1e9. `None`
  when the file has no such element.
- **Access `chunk`.** The subblock is uncompressed, so its pixels are one little-endian block. `read` fetches one
  byte range, the rows asked for at full width, and slices x from it. Nothing but the header, the directory, the
  subblock's 288-byte head and the XML is read at open. (`io` fetches in 1 MiB blocks, so in a small file the
  first block also holds the first pixel rows.) A server without Range support is staged whole (access `file`).
- **Refused at open**, with the reason: more or fewer than one full-resolution subblock (tiles of an unstitched
  mosaic, several channels, scenes, z planes or time points); a compressed subblock (JPEG, LZW, JPEG XR, zstd);
  a colour or complex pixel type; a multi-part file; a subblock whose data size is not `y * x * itemsize`.
  Pyramid subblocks (stored size smaller than the extent they cover) are skipped, not counted; this rule was
  tested on a synthetic file only, the tested dataset has none.

## Label form: `urls: [<label .tiff>, <its .czi>]`, `params: {label_factor: k}`

For a segmentation stored on a k-times coarser grid than its image, as when it was made on a reduced copy. The
harness needs a label with its raw's shape on every axis, so the array this form returns has the **CZI's** shape:

```
label[y, x] = tiff[min(y // k, tiff_y - 1), min(x // k, tiff_x - 1)]
```

- The two URLs may come in either order; the files are told apart by their first four bytes. Only the CZI's
  header, directory and XML are read (for the shape and voxel size), never its pixels.
- `label_factor` is required and checked: the TIFF's shape must equal the CZI's divided by `k`, rounded down,
  on both axes; anything else raises. `k = 1` is a TIFF already on the CZI's grid.
- The TIFF must be one series of one page with axes `YX` and an integer (or bool) dtype, which is kept.
- `voxel_size_nm` is the CZI's, because the array is on the CZI's grid; the TIFF's own tags are not consulted.
- Access `plane`: the TIFF page is read whole on the first `read` and kept.

**Why this is in a reader.** A snippet must not resample (SPEC §2). This form does not: every value returned is
a value of the file at the one TIFF pixel that covers the raw pixel, nothing is interpolated, averaged or
dropped, and the TIFF is recovered exactly by taking every k-th pixel. It states where the file's pixels lie on
the raw grid, as `tiff-jpeg-voc-zip-glob` states where VOC boxes lie. Whether that belongs in a snippet or in the
harness is the reviewer's call; if the harness learns to pair a label by a scale, bind the TIFF with a TIFF
snippet instead and supersede this form.

**Known limits of the label form.**

- *Origin and edge are assumed.* TIFF pixel (0, 0) is taken to cover raw pixels (0..k-1, 0..k-1). When the raw
  size is not a multiple of `k`, the last 1 to k-1 raw rows or columns lie past the TIFF and take its last row
  or column. If the producer instead resized the whole field of view (scale `raw / tiff`, 4.0007 in the tested
  pair), the two readings differ by under one label pixel (under k raw pixels) at the far edge. The dataset
  does not document how the reduced grid was made.
- *Borders are blocky.* Label borders on the raw grid are k-pixel staircases, k times coarser than the raw.
- Each label array needs its own CZI URL, so the files of one pair must be known (here: the `metadata.csv` row).

## Tested on

`allen-hipsc-12x-overview-colony-masks` (examples/): a stitched 12x brightfield well overview, 5640 x 5845
Gray16, one uncompressed subblock, 1083.33 nm per pixel; colony segmentation TIFF 1410 x 1461 int32 with
`label_factor: 4`. Checked on the two files: the raw equals the subblock's bytes read directly; the label equals
the TIFF repeated 4 x 4; label outlines drawn over the 4x block-averaged raw follow the colony borders; each
nonzero id is one connected region (hence `segmentation_type: instance`).
