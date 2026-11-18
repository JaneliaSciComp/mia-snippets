# tiff-pages-glob-txt-points

Reads a set of multi-page TIFFs whose pages are channels, one file per z plane, and the **point lists** that
headerless text files beside them hold for one channel, marked on that channel's grid as a label. It was
written for the CenFind datasets: max projections `<field>_max.tif` (4 or 5 pages of 2048x2048 uint16), nucleus
masks `<field>_max_C0.tif` and centriole foci `<field>_max_C<k>.txt`, all deflated members of one zip.
`tiff-channels-glob` refuses these files (tifffile axes `QYX`, not `CYX`), `tiff-zip-member-3` reads them one
file per array, and no snippet read a headerless `a,b` list from zip members.

One snippet reads images, masks and points so that a label and its raw share one file order.

- **URLs.** Each url is a zip member `<zip url>::<member>`, a member glob `<zip url>::<pattern>`, a plain file
  or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*` also crosses `/`). A
  glob is expanded with `io.expand` and sorted by name; several urls are concatenated in binding order; a file
  named twice raises. Each file is one z plane; `z` is dropped when there is one file.
- **Files.** One series of single-sample `YX` pages whose axis the file does not name: tifffile axes `QYX` or
  `IYX` (a 'shaped' TIFF as `tifffile.imwrite` makes it from a 3D array), `CYX`, or `YX` for a single page.
  That the pages are channels is the binding's statement (give the reason). A file that names its pages `Z`
  or `T`, has several series, or has RGB samples raises. Every file must have the first file's page count,
  shape and dtype; it is checked when the file is read, and files of another page count are another array (a
  narrower glob).
- **Images** (no `points`). All pages as channels, `[z]cyx`; `channel: k` reads page k only, `[z]yx`. A
  single-page file (a mask) is `[z]yx`. Values are returned as stored.
- **Points** (`points` set; `columns` is then required, `radius` optional, and `channel` required when the
  files have several pages). The array is `[z]yx` `uint16` on the grid of the same images:
  - The urls are the images, the same as the raw the label pairs with. `points` is a member name template that
    gives each image's text file in the image's zip: `{name}` is the image's file name without its directory
    and extension, `{c}` is `channel`, e.g. `set/annotations/centrioles/{name}_C{c}.txt`.
  - A text file has one point per line, two comma-separated whole numbers and no header (blank lines are
    skipped; UTF-8). `columns: xy` reads a line as `column,row`, `columns: yx` as `row,column`. The files do not
    say which; decide it by laying the points over the image (bright foci under them one way, background the
    other). Coordinates are 0-based pixel indices.
  - A point is marked with its 1-based line number in its file (ids restart in every plane); background is 0.
    `radius: 0` (default) marks the point's pixel, `radius: N` every pixel whose centre is at most N pixels
    from it, cut at the image border. Where discs overlap, a pixel takes the nearest point (the earlier line on
    a tie). A disc is a marker, not the object's outline.
  - **Every image must have its text file**; `open_array` raises when one is missing and names it. A missing
    file may mean "not annotated" (in CenFind it does), which an empty plane would turn into "no points here".
    Bind the annotated images only, as plain urls, in the label and in the raw it pairs with. A text file with
    no lines is an empty plane.
  - Raised at `read`, naming file and line: a line that is not two numbers, a coordinate that is not a whole
    number, a point outside the image, more than 65,535 points.
- **Access.** `plane`: one file per z and one page per channel. A stored member or plain file is read by range,
  one page per plane; a deflated member is fetched whole with `io.fetch` when one of its planes is first read.
  `open_array` expands the urls and reads the first file's header (so a deflated first file is fetched then);
  for points it also reads the zip's directory, and each text file when its plane is read. The last page
  decoded and the last point plane made are kept.
- **Voxel size.** `None`: a shaped TIFF has none, and no other pixel size metadata is read.

## Known limits

- Point text files are looked up among the members of the image's zip, so a points array needs zip member
  urls. Images are read from plain urls and directory globs too, but only zip members were tested on data.
- Fixed text layout: two comma-separated columns, no header, whole numbers. Other separators or a header raise.
- A points array is one channel's label, `[z]yx`; there is no `[z]cyx` label over all channels.
- All files of one array must agree in page count, shape and dtype. Pages are decoded whole (no tile reads).
- At most 65,535 points per plane; `radius` at most 255.

## Example

`examples/figshare-21581358-cenfind-centriole-foci-widefield.yaml`: five folders of 24 or 25 fields of
immunostained human cells (widefield max projections, DNA dye in channel 0, centriole markers in channels 1-3).
Each folder is bound as four raws, one per channel, because the dataset annotates channels separately:
channel 0 pairs with the nucleus masks (instance ids), channels 1-3 with their foci (`columns: xy`,
`radius: 2`). Channels 1 and 3 have a point file for every field; channel 2 has one for 14 fields per folder,
the ones whose `train.txt`/`test.txt` entry names channel 2, so its raws and labels list those fields as plain
urls. The binding's `reasons` give the measurements behind `columns` and `radius`. The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
