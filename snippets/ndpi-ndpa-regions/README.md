# ndpi-ndpa-regions

Reads one Hamamatsu NDPI whole-slide image, and the regions drawn on it in NDP.view (its `.ndpa` file), on one
pixel grid.

## Raw: `urls: [<slide>.ndpi]`

The first page of the NDPI, which is its full-resolution level, as `yxc` uint8 (RGB). The other pages (the smaller
levels, the macro photograph and the map) are not presented.

An NDPI level is one JPEG stream with restart markers, and the file lists where each restart interval starts
(`McuStarts`). tifffile turns those into tiles (8 rows by 4096 columns on S32 of the tested record), so a read
fetches and decodes only the strips its region touches: `access` is `chunk`. A 256x256 crop is about 32 rows of
strips. Strips are stored row after row across the full width, so on a wide slide the strips of one crop are
spread over a few MB of the file.
On a server without range requests the file is staged whole and `access` is `file`.

`voxel_size_nm` is from the page's `XResolution` and `YResolution` (pixels per centimetre or inch), so it is
each slide's own value and y and x may differ in the last digits.

## Label: `urls: [<slide>.ndpi.ndpa, <slide>.ndpi]`, `params.classes`

An NDPA is XML: `<annotations>` with one `<ndpviewstate>` per region, each with a `<title>` and an
`<annotation type="freehand">` whose `<pointlist>` is the outline. The label is `yx` uint8 on the grid of the
NDPI's first page: a pixel holds the class id of the region whose outline encloses its centre, 0 where none does.

- **Class ids** come from `params.classes`, the list of region titles: id = position, from 1. A title that is not
  listed is an error, so no region is dropped silently. A listed title that a slide does not use is fine.
- **Placement.** NDPA points are nanometres from the centre of the slide (`<coordformat>nanometers`). The NDPI
  gives the centre of its image in the same frame (`XOffsetFromSlideCentre`, `YOffsetFromSlideCentre`, tags 65422
  and 65423) and its pixel size, so `x_px = (x_nm - XOffset) / x_pixel_nm + width / 2`, and the same in y. That
  is why the label needs both urls. Only the NDPI's header is read for it, no pixel.
- **Fill.** Nonzero winding rule: a loop the pen made twice is still inside. Regions are painted from the one
  that fills the most pixels to the one that fills the fewest (equal ones in file order), so a region drawn inside
  another is kept, whatever the two classes are: the stroma outlined inside a lesion, and equally the lesions
  outlined inside one loose stroma outline (S32 and S1 of the tested record). Where two regions only partly
  overlap, the shared part goes to the smaller one. The order of `classes` decides ids, not what is kept.
- An outline of fewer than 3 points (a click; S1 has one, of 1 point) encloses nothing and fills nothing.
- `access` is `plane`: the whole NDPA (4 MB for S32, 22.6 MB for S1) is read and parsed on the first read,
  whatever the region (about 15 s for S1's). Outlines are kept as their crossings with the pixel rows, and only
  the window asked for is filled. A reader that asks for the whole plane gets it: 1.1 GB for S1 (30976x36864).

## Limits

- One slide per binding entry: slides differ in shape, so there is no glob form.
- Only closed freehand outlines (`type="freehand"`, `<closed>1`) in nanometres are filled. Any other annotation
  (circle, pointer, ruler, pin, an open line) is an error naming it, not skipped. An NDPA with no region at all
  is an all-zero label.
- The error for an unreadable annotation comes at the first read of the label, not at open, and `harness smoke`
  reads only the labels of the first raw: read every label of a binding once (`harness describe --binding`)
  before trusting it.
- Only JPEG RGB NDPIs whose first page has restart markers and is the largest level. Fluorescence NDPIS sets and
  multi-focal-plane NDPIs were not tested.
- The label says what was outlined, not what was not: 0 is "not annotated". Freehand outlines are drawn loosely,
  so a region includes whatever glass or neighbouring tissue lies inside the line.
- An NDPA that does not belong to the NDPI is caught only when none of its regions touches the image.
