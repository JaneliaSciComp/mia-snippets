# svs-png-colour-mask-grid

Reads one **Aperio SVS** whole-slide image, and the **downsampled colour-coded annotation PNG** published beside
it, as a raw array and a label array on the same grid. The form is chosen by the params:

| params | urls | array |
| --- | --- | --- |
| none | one SVS (plain URL) | raw: `cyx` `uint8`, `c` = red, green, blue, the slide's full-resolution page |
| `colours` and `label_factor` | the mask PNG and the SVS it belongs to (two urls, any order) | label: `yx` `uint8` class ids with the slide's `y` and `x` |

Files are told apart by their first bytes (TIFF or BigTIFF magic, PNG signature), not by their names: figshare
download URLs have no extension. One slide is one array, because slides differ in shape; a set of slides is one
`raw` entry and one `labels` entry per slide. Globs are not expanded.

## Raw form (the SVS)

- **What is read.** The first page (IFD) of the TIFF only, which in an Aperio SVS is the full-resolution image.
  The reduced pyramid levels, thumbnail, label and macro pages are never parsed. `shape` is
  `(3, ImageLength, ImageWidth)`.
- **Tile by tile, by byte range.** `open_array` reads the first IFD (in the tested BigTIFF about 3 MiB, mostly
  the tile offset and byte count tables of its 67,840 tiles); a `read` fetches and decodes the tiles its region
  touches and nothing else. Tiles are decoded by `tifffile` (`TiffPage.decode`, with the page's JPEGTables): JPEG
  in the tested file, where a YCbCr JPEG comes back as RGB. Other codecs go through the same call but were not
  tested. An edge tile is cut at the image border; a tile stored with byte count 0 reads 0. Up to 512 decoded
  tiles (at most 128 MiB) are kept.
- **Voxel size.** The `MPP = <µm per pixel>` field of the Aperio ImageDescription, times 1000, for y and x;
  `None` when the description has no MPP field. The TIFF resolution tags are not used.
- **Access.** `chunk`; `file` when the server gives no ranges and the file was staged whole.
- **Refused at open**, with the reason: a first page that is in strips, is not 8-bit, does not have exactly 3
  interleaved samples (RGB or YCbCr), or has depth.

## Label form (the mask PNG on the slide's grid)

The mask is an 8-bit RGB PNG (colour type 2) in which each class is painted in one colour, `label_factor` times
smaller than the slide. The array has the slide's `y` and `x`, so the harness can pair it with the raw.

- **`label_factor`** (int, required for the label form). Slide pixel `(y, x)` takes mask pixel
  `(y // k, x // k)`: each mask pixel is repeated `k x k`, never interpolated. The mask's height and width must
  each be the slide's divided by `k`, rounded down or up; anything else raises at open with both sizes. With a
  rounded-down mask the last `n % k` slide rows or columns take the last mask row or column.
- **`colours`** (list, required for the label form). The colour table: `[r, g, b]` colours, integers 0..255, at
  most 256, none twice. A mask pixel of `colours[k]` is read as id `k`. The first colour is id 0, which the
  harness treats as unlabelled: put the background ("not annotated") colour first. The snippet knows colours,
  not class names; the binding's `reasons` say what each id means.
- **Colours that are not in the table.** A mask shrunk with interpolation holds mixtures of the neighbouring
  colours at every region border (in the tested dataset 65 of 104 masks, up to 9% of a mask's pixels). The
  nearest table colour is the wrong answer for them: halfway between blue and black lies closest to the indigo of
  another class. They are settled from their neighbours instead, in rounds. In each round, every unsettled pixel
  that touches (8 neighbours) pixels settled before the round takes the id of the nearest colour to its own,
  by squared distance over r, g, b, among **those neighbours' colours and the background `colours[0]`**. A tie
  goes to the lower neighbour id, the background last. So a mixture becomes one of the classes that meet at it,
  or background, and never a third class. The background is always a candidate because a gap between two
  regions that is thinner than a mask pixel is a dark mixture with no background pixel beside it.
- **A listed colour standing alone** (no neighbour of the same colour, in a mask that has unlisted colours) is
  settled the same way. A mixture can equal a third listed colour exactly (half magenta on black is purple), and
  such a pixel stands alone. A real one-pixel region of a class that touches no pixel of its own colour is
  thereby given a neighbour's id or background. In a mask that holds listed colours only, nothing is settled and
  every pixel keeps its colour's id.
- **A missing table colour raises** at `read`: a 3x3 block of one unlisted colour is a painted region, not a
  mixture, and the error names the colour. A mask in which more than half the pixels are unlisted raises too
  (a photograph, or the wrong table). Appending a colour keeps the ids already given.
- **Voxel size.** The slide's (the array is on the slide's grid). **dtype** `uint8` always.
- **Access.** `chunk`: a region costs only its own pixels. `open_array` reads the PNG's 33-byte header and the
  slide's first IFD, no slide pixel; a deflated zip member is staged whole with `io.fetch` for that (the tested
  mask is 38 kB). The first `read` decodes the whole mask once and keeps its ids, 1 byte per mask pixel; the
  full-resolution plane is never built unless a read asks for all of it.

## Known limits

- The label is as coarse as the mask: with `label_factor` 64 a 256 x 256 crop holds 4 x 4 mask pixels, and class
  borders are 64-pixel steps. If the dataset also publishes the outlines as vector annotations, a rasteriser on
  the slide's grid gives sharper labels than this snippet can.
- Settling is a decision about pixels whose colour is ambiguous. Two adjacent pixels that are both exactly a
  third listed colour (a straight border at exactly half coverage) keep that colour's id. A region of a class
  that is thinner than a mask pixel everywhere, so that none of its pixels has its exact colour, is read as its
  neighbours' classes or background.
- A class colour missing from the table is found only where it fills a 3x3 block of the mask; smaller patches of
  it are settled as mixtures.
- The mask's size is checked against the slide's, but which mask belongs to which slide is the binding's to
  state (here: the same UUID in both names).
- 8-bit RGB PNG masks only: an RGBA or palette PNG, a 16-bit PNG and an RGB PNG with a tRNS chunk raise.
- Only the slide's first page is read; the stored pyramid is not used. 16-bit, planar, strip or non-RGB first
  pages are refused. The page is returned as stored, including any blank margin the scanner padded it with (in
  the tested file the page is 81920 x 54271 and the description names an 81520 x 54256 scan; the rest is white).

## Example

`examples/figshare-28919840-hiesd-gastric-esd-wsi.yaml`: HiESD (figshare 28919840), 104 H&E whole-slide images
of gastric ESD specimens with one 1/64 colour mask each. The binding covers the pair the record gives URLs for,
slide `e492f29a-…`: raw `cyx` (3, 54271, 81920) at 245.8 nm, label `yx` (54271, 81920) from the 847 x 1280 mask
(54271 // 64 = 847, 81920 / 64 = 1280) with ids 0..10 in the record's class order.

Checked on the real files: the raw decodes to H&E colours at full sharpness; the mask's outlines drawn over a
1/16 overview of the slide follow the mucosa they annotate, with no flip or offset. The mask holds 904 colours;
its 8,384 unlisted pixels settle into the three classes it paints (ids 6, 9, 10) and background, where the
nearest table colour would have added 520 pixels of id 4 and 23 of id 1 along the borders. All 104 masks of the
zip were decoded with the same table: none raises, and no mask gains a class that it does not also hold as an
exact colour.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
