# qupath-qpdata-classes

Reads the classified objects of one QuPath image entry (`<project>/data/<n>/data.qpdata`) and fills them as class
ids onto the pixel grid of one slide: `yx`, `uint8`, the slide's full size, 0 where nothing listed lies.

## What it reads

- **urls**: exactly one `data.qpdata`, a zip member (`<zip>::<member>`) or a plain URL. No globs. The file is
  fetched whole (`access` is `file`); the largest seen is 9.8 MB.
- **The Java object stream is never deserialized.** A `.qpdata` is written by `ObjectOutputStream`. The snippet
  walks the stream's grammar (class descriptions, field values, block data, back references; protocol 2) into
  plain records and takes three things from them: the image description (a JSON string, data file version 3 or
  later), each object's class name (`PathClass.name`, joined as `Parent: Child` for a derived class) and each
  ROI's coordinates. No class is loaded and no code from the file runs.
- **Objects**: every annotation and detection in the hierarchy, at any depth, whose class is listed in `classes`.
  ROIs read: `PolygonROI` (float x and y arrays), `RectangleROI`, and `GeometryROI` (WKB Polygon or MultiPolygon,
  holes included). Any other ROI on a listed class (ellipse, line, points, the old `AreaROI`) is an error.
- **Slide grid**: the image description is either one slide (`builderType: uri`) or a QuPath montage
  (`builderType: sparse`): rectangles cropped from several slides, placed side by side. For a montage each region
  gives its place in the montage and the rectangle it was cropped from on the slide; the ROIs inside a region are
  moved by the difference, so they land on the slide's own grid. The slide's size and pixel size come from the
  same description (`voxel_size_nm` from `pixelCalibration` when its unit is µm, nm or mm, else `None`); no slide
  file is opened.
- **Filling**: a pixel belongs to an object when its centre is inside, by the even-odd rule over all the object's
  rings. Only the part of the slide the entry shows is filled; the rest is 0.

## Params

- `image` (required): the slide's file name as the entry names it, the last part of its `uri`, percent-decoded.
  The error for a name the entry does not show lists the names it does.
- `classes` (required): class names in id order (first is 1). Objects of other classes, or of none, are left
  out. Where two listed classes overlap, the one listed later wins. A name no object has is an error that lists
  the file's class names.

## Known limits

- Only pixel-for-pixel montages: a region shown at a downsample other than 1, or through a builder other than
  `cropped` over `uri` (rotated, pyramidized, ...), is not matched or is an error.
- One plane: an ROI with z or t other than 0 is an error.
- Whether an object is an annotation or a detection, and its name, measurements, probability or lock state, are
  ignored; choose by class name.
- A class whose `writeObject` does not follow the stream grammar (none in QuPath 0.3 files seen) would stop the
  walk with an error rather than be guessed at.
- 0 means "no listed object here", which is not the same as background.
- Tested on QuPath 0.3.0 files (data file version 3) of one project. The filled areas agree with the areas QuPath
  stored beside the ROIs to within 1 percent; alignment to the slide pixels was not checked by eye.
