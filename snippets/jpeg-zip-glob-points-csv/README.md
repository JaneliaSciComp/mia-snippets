# jpeg-zip-glob-points-csv

Reads the JPEG members of a zip as one array, one image per z plane, and reads the **point annotations** of
those images from a CSV table in the same zip as a label array on the same planes. No catalog snippet read a
CSV or a points table before this one; `jpeg-zip-glob` reads the same images but has no labels.

One snippet reads both so that a label and its raw share one member order: both take the same `urls` and the
same `glob`, and the label adds `table`.

- **URLs.** Each url names one zip: a plain URL, or `<outer zip url>::<inner zip member>`. Several urls are
  concatenated in binding order. The images inside are picked by `params.glob` (`fnmatch` over the member
  names; `*` also crosses `/`) and sorted by member name within each zip. The urls themselves are not globs.
- **Images** (no `table`). 8-bit JPEGs decoded by libjpeg (`imagecodecs`): a colour file is three `c` planes
  (`zcyx`, c = R, G, B), a grey file none (`zyx`); `z` is dropped when the glob matches one file. Shape comes
  from the first file's frame header; a file that decodes to another shape raises at `read` and names itself.
- **Points** (`table` set; `label`, `classes` and `radius` are then required, and refused without `table`).
  - `table` matches exactly one member of each zip, a CSV whose header names the columns `imageName`,
    `objectType`, `xCoord`, `yCoord` (any order, other columns ignored; UTF-8, a BOM is accepted). Each row is
    one point at column `xCoord`, row `yCoord` of the image whose **file name** (the member name after its last
    `/`) is `imageName`. With several urls, each zip's table describes that zip's images.
  - The array is `zyx` `uint16` on the grid of the first image, one plane per image matched by `glob`. An
    image without rows is an empty plane (all 0).
  - Each point is painted as a disc: the pixels whose centre is at most `radius` pixels from the point
    (`radius: 0` is the one named pixel), cut at the image border. The disc is a **marker**, not the object's
    outline: the table says where an object is, not how large it is. Choose `radius` smaller than the object
    and say why in the binding.
  - `label: instance` fills a disc with the point's 1-based number among the kept rows of its image, in table
    order; ids restart in every plane. `label: semantic` fills it with the 1-based position of the row's
    `objectType` in `classes`. Background is 0.
  - `classes` lists the `objectType` values that are painted; `skip` lists values whose rows are left out
    (neither painted nor numbered). A row whose `objectType` is in neither raises, so a class the record does
    not know is found rather than dropped.
  - Where discs overlap, a pixel takes the value of the nearest point (the earlier row on a tie), so close
    points keep one region each and no disc hides another completely unless two rows name the same pixel.
  - Coordinates are used as 0-based pixel indices (`x` = column, `y` = row of the decoded JPEG). The table
    format does not say whether its authors counted from 0 or 1; if from 1, markers sit one pixel right and
    below. EXIF orientation is not applied to images or points.
  - Checked at open, on the whole table: the header; every row has the header's number of fields; whole-number
    coordinates inside the image; every `imageName` is the file name of a member of the zip. A row for an image
    of the zip that `glob` leaves out is ignored, so a glob may select a subset (one contrast, say) and the
    label follows it; a row naming a file that is not in the zip raises.
- **Access.** `plane`: an image plane costs one member (inflated and decoded whole); a label plane costs
  nothing after open. `open_array` reads each zip's directory and the first image's bytes (frame header only,
  nothing decoded); for labels also the table. The zip is read by range through `io.open`; a zip that cannot be
  (a deflated inner zip, a server without Range) is staged whole with `io.fetch`. The last plane made is kept,
  so reading the three `c` planes of one image decodes it once.
- **Voxel size.** `None`: a JPEG's JFIF density is a print size, and the table has none.

## Known limits

- Column names are fixed (`imageName`, `objectType`, `xCoord`, `yCoord`); a table with other names needs a new
  snippet. Non-integer coordinates raise instead of being rounded.
- All images of one array must have the first image's shape; points are checked against that shape only.
- If `glob` matches two images of the same file name in different folders of one zip, a label array raises:
  the table names images by file name only.
- At most 65,535 kept points per image; `radius` at most 255.
- No `samples` selection (see `jpeg-zip-glob`), no 12-bit or CMYK JPEG, no glob urls.
- A label plane is built whole (one `uint16` image plus a same-size distance buffer) for any region of it.

## Example

`examples/zenodo-13368072-schistoscope-haematobium-eggs.yaml`: *Schistosoma haematobium* eggs in urine imaged
with a mobile phone microscope, two zips (field visits) of 2,280 and 2,088 JPEGs, 3024x4032 RGB, brightfield
and darkfield of each field of view. Each zip is one `zcyx` raw. Its annotation table (1,623 and 7,460 points,
`objectType` `S.haematobium` or `doubtful`) is bound twice: `label: instance` with `skip: [doubtful]` (the
certain eggs, numbered per image) and `label: semantic` with both classes (1 egg, 2 doubtful), as discs of
radius 12 on eggs about 100 x 50 pixels in size. Points were checked by eye against the images in both zips. How
a points table should be bound (`segmentation_type` has no value for it) is a human's decision; see the
binding's `reasons`. The example binding's `snippets_commit` is the commit before this snippet's own (a binding
can't name the commit that adds it); a binding written by the worker names a commit that contains the snippet.
