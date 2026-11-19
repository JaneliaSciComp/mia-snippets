# png-object-masks-glob

Reads an annotation stored as **one PNG per object**: every file is as large as the whole image and holds one
object (a cell) drawn on black. The files of one image are merged into a single `yx` plane of ids on that
image's grid.

## What it reads

- `urls`: the mask files of **one image**, as zip members (`<zip url>::<member>`), member globs
  (`<zip url>::<glob>`, e.g. `masks.zip::masks/<image>/*/*.png`), directory globs (`<dir url>/::<glob>`) or
  plain URLs. Each url is expanded with `io.expand` (a glob's matches sorted by name) and the expansions are
  concatenated in binding order. A glob that matches nothing, or a file named twice, is an error.
- Files: 8- or 16-bit PNG, grey or RGB. A palette PNG or one with an alpha sample is refused: a palette's
  index 0 need not be black and alpha is not part of the drawing, so neither says which pixels are the object.
  Every file must have the first file's height and width.
- An object's pixels are the pixels where the file is not zero; for RGB, where any sample is not zero. The
  colour itself is not used.

## What it returns

`yx`, the files' height and width, `uint16` (`uint32` above 65,535 ids), `access: file`, no voxel size (PNG has
no pixel size).

- Default (`ids: file`): **instance ids**. File `k` of the expanded urls, counted from 1, is drawn with id `k`;
  0 is background. The id is the file's position, not a number in its name (names here count from 0, which
  would collide with background). To restrict to one class folder, bind that folder's glob.
- `ids: url`: **class ids**. A file is drawn with the 1-based position of the binding url it came from. With
  one glob per class folder, e.g. `[…/fed/*.png, …/unfed/*.png, …/Unidentified/*.png]`, the plane holds
  1 = fed, 2 = unfed, 3 = Unidentified. The binding states that order in `reasons`.
- **Overlap**: files are painted in order, so where two objects overlap the pixel keeps the id of the later
  file. An object can lose pixels this way and, if later files cover it entirely, vanish; so can a file with
  no nonzero pixel. Neither is an error. With `ids: url` the later url's class wins.

## Cost

A zip is opened once (`ZipFile(io.open(<zip url>))`) and members are read from it by range in the harness's
1 MiB blocks; zipfile inflates each member and checks its CRC. The files of one image usually lie together in
the zip, so hundreds of small files cost a few requests. Reading each member with `io.fetch` instead is two
requests per deflated member, which Zenodo answered with `429 TOO MANY REQUESTS` after about 50 files.

`open_array` reads the zip's directory and the first file. The first `read` decodes **every** file (any pixel
may belong to any file) and keeps the merged plane in memory; later reads slice it.

## Tested on

`zenodo-8315423-cellular-autophagy-s2-cells`, image `Timepoint_001_220518-ST_K07_s1`: 381 RGB PNGs of
2048x2048 (144 fed, 51 unfed, 186 Unidentified) merge to 381 ids, 4.7 MB fetched. Checked against the
individual files: every id's pixels lie inside its file's object, 9 pixels belong to two files (8 cells lose
1 or 2 pixels each), no cell is hidden and no file is empty. `ids: url` on the three class globs gives the same
foreground with ids 1 to 3. The merged mask lies on the cells of the raw as stored (mean of w1 inside the
mask 731, outside 162; flipped or transposed masks give no contrast).

## Known limits

- One array is one image. Stacking several images' masks along z is not done here.
- The first read costs all the files: 15 to 25 s of PNG decoding for 381 files of 2048x2048.
- A zip on a server without Range is staged whole (`io.fetch` of the zip), which the byte cap may refuse.
- An offline job (a re-stage from the dataset cache) finds the zip only if the whole zip is in the cache; the
  blocks read by range are not kept there.
- A zip nested in another zip is not read.
- Anti-aliased edges count as object: any nonzero value does.
