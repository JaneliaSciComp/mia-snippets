# svg-paths-circles-glob

Reads SVG label files, one per image, whose annotations are filled `<path>` polygons and `<circle>` discs, and
rasterises them as class ids on the file's own pixel grid: `uint8`, `zyx` with one file per z plane (`yx` for a
single file), 0 = background. No catalog snippet read SVG before this one; the polygon rasterisers parse
GeoJSON, COCO, LabelMe, VIA or YOLO files and take their grid from an image.

- **URLs.** Zip-member globs (`<zip url>::<glob>`), HTTP directory globs (`<dir url>/::<glob>`) or files listed
  one by one; globs are expanded with `io.expand`, matches sorted by name, several URLs concatenated in binding
  order. A label and the raw it pairs with must sort into the same order: the snippet sees only the SVG names,
  so the binding's author checks that against the listing.
- **Grid.** The root `<svg>`'s `height` x `width`, whole numbers (a `px` suffix is accepted). A `viewBox`
  must be `0 0 width height`, so one user unit is one pixel; anything else raises. `open_array` reads the first
  file for the grid; every other file must have the same one, checked when its plane is read.
- **Shapes.** Only direct children of `<svg>` are read. `<path>` may carry `d` and `fill`, `<circle>` `cx`,
  `cy`, `r` and `fill`; `<defs>`, `<title>`, `<desc>` and `<metadata>` are skipped. Everything else raises,
  whether or not the array asks for that element type, so nothing drawn goes unseen: another element (`<g>`,
  `<rect>`, `<polygon>`, `<ellipse>`, text, `<style>`, ...), another attribute (`transform`, `stroke`, `style`,
  `fill-rule`, ...), `fill="none"`, root attributes other than `width`, `height`, `viewBox`, `version`, and
  XML with a DOCTYPE or entity declaration. The fill colour is not interpreted: the class is the element type.
- **Paths.** `d` is read as straight outlines written with absolute `M`, `L` and `Z` (`M x,y L x,y ... Z`,
  commas or blanks, several subpaths allowed). Curves, arcs, `H`, `V`, relative commands and numbers in
  exponent notation raise. A subpath left open is closed, as SVG fills it. Within one path the nonzero winding
  rule applies (SVG's default), so a subpath wound the other way cuts a hole; different paths are a union.
- **`classes`** (required) names the element types to fill, in id order: `[path, circle]` paints every path
  1 and then every circle 2 on top of them, `[circle, path]` the reverse, `[path]` or `[circle]` a {0, 1} mask
  of one type with the other left out. Use one array per type when the classes overlap and both should be
  kept whole.
- **`origin`** (required) says where coordinate (0, 0) lies in the top-left pixel, which an SVG file cannot
  say about itself:
  - `centre`: coordinates are pixel indices, x = column and y = row, as files written from image-processing
    contours (e.g. OpenCV) have them. Pixel (row, col) is sampled at (x, y) = (col, row).
  - `corner`: SVG user units as a renderer draws them: pixel (row, col) covers `[col, col+1) x [row, row+1)`
    and is sampled at its centre (col + 0.5, row + 0.5).

  How to tell: with `centre` the largest coordinates of shapes that run to the image border are `width-1` and
  `height-1`; with `corner` they are `width` and `height`. The two readings differ by half a pixel.
- **Fill rule.** A pixel is painted when its sample point lies inside a shape **or on its outline**: for a
  path, inside by nonzero winding or on an edge or vertex; for a circle, at a distance of at most `r` from the
  centre. With `centre` and whole-number coordinates the vertices and edges themselves are therefore painted,
  so a rectangle from x 2 to 5 is 4 pixels wide and a circle of radius 10 is 21 pixels across. Shapes that
  reach past the grid are cut at it. Coordinates must be plain decimal numbers below 1e9 in size; a circle's
  `r` must be positive.
- **Access.** `plane`: a file is parsed and filled whole whatever the region (about 0.4 s for a 3536 x 4096
  plane with 750 paths, 0.25 s for 700 circles); the last plane is kept. A deflated zip member raises
  `NeedsFetch` in `io.open` and is staged with `io.fetch`.
- **dtype** `uint8`. **Voxel size:** `None`; the file's units are pixels.

## Known limits

- **The fill is this snippet's rule, not the annotator's raster.** Another rasteriser of the same file (an SVG
  renderer with antialiasing, OpenCV's `fillPoly`, which also draws the outline with a line algorithm) can
  differ by a pixel along outlines.
- Elements are classes by type, not by colour, `id` or `class`; a file that marks classes by fill colour within
  one element type is not read as such, and files carrying such attributes raise.
- No instance ids: all shapes of a type share one id.
- No transforms, groups, styles, strokes, curves or other basic shapes; no `viewBox` scaling. SVG drawings
  exported from Inkscape or Illustrator usually have these and raise.
- One file is one plane; all files of an array must have the same grid. Memory while filling is about six
  bytes per pixel of the plane.
- Grid and shape checks beyond the first file happen at `read`, not at `open`.

## Example

`examples/edmond-hy5syn-ic-metal-layer-sem.yaml`: one 6.4 GB zip at edmond.mpg.de with SEM tiles of the metal-1
and metal-2 layers of an integrated circuit (3536 x 4096 uint8 PNG) and, for the 321 metal-2 tiles, one SVG
each (`dataset/labels/m2/labelNNNN.svg` for `dataset/sems/m2/semNNNN.png`): `<path fill="lime">` polygons for
tracks and `<circle fill="red">` discs for vias, 229,709 paths and 233,564 circles in all, every path a single
`M ... L ... Z` outline of whole numbers. The raw is bound with `image-tiles-glob`, the labels as two {0, 1}
masks, `classes: [path]` (track) and `classes: [circle]` (via), both `origin: centre`: the coordinates run to
4095 and 3535, never to 4096 or 3536, and the track mask sits on the bright metal of the image within 0.15 px
(half a pixel off with `corner`). All 321 files were read: each plane holds tracks (11-27% of the pixels) and
vias (0.8-3.2%); the two-class form `[path, circle]` holds exactly the ids 0, 1, 2.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
