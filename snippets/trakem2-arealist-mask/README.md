# trakem2-arealist-mask

Reads the painted areas of [TrakEM2](https://imagej.net/plugins/trakem2/) projects as binary masks, one project
per z plane, for datasets that ship one 2D image and one project per field of view.

## What it reads

- **Urls**: TrakEM2 project files, `.xml.gz` (gzip) or `.xml`, as zip members, plain URLs or globs
  (`<zip>::<glob>`, `<dir>/::<glob>`). One project is one plane; several make `zyx`, one makes `yx`.
- **Image urls (optional)**: any url that is not a project is taken as the image of the project of the same name
  (`S10V1_RGB.tif` beside `S10V1.xml.gz` with `image_suffix: _RGB`). No byte of an image is read. They fix the
  plane order: plane k is the project of image k, in the order the urls expand, which is the order a raw snippet
  given the same image glob stacks them in. This matters because the two name-sorted lists differ
  (`S1V10.xml.gz` sorts after `S1V1.xml.gz`, but `S1V10_RGB.tif` sorts before `S1V1_RGB.tif`). Images and
  projects must pair one to one by name, or `open_array` raises. Without image urls the planes are the projects in
  url order.
- **A project** must have one `t2_layer_set` with one `t2_layer` holding one `t2_patch` (the image); copies of the
  patch at the same place count as one. The mask's grid is the patch's own pixels, `o_height` x `o_width`.
- **Objects**: every `t2_area_list` of the layer set. Its `t2_path`s (`M x y L x y ... z`) are filled together by
  the even-odd rule, which is how TrakEM2 writes an area: one path per outline and one per hole. Coordinates go
  through the area list's `transform` and then the inverse of the patch's, so a patch that was moved on the canvas
  (55 of the 174 projects of the tested dataset, by up to 897 px) still gets its areas on its own pixels. A pixel is
  inside when its centre is; TrakEM2's outlines run along pixel edges, so the fill is exact. Parts outside the
  image are cut off.
- **Output**: `uint8`, 1 where any object matching `titles` is, 0 elsewhere. 0 is "not painted", which is not
  the same as background. `access` is `plane`: a read fetches and rasterises that plane's project.
- `voxel_size_nm` is `None`: `t2_calibration` gives a pixel width, but its unit in the tested files is the literal
  `pixel`, and the value differs from project to project.

## Params

- `titles` (required): fnmatch patterns for the objects of this mask. Titles and patterns are compared
  case-insensitively, with runs of blanks as one blank. An object still titled `area_list` (TrakEM2's default)
  goes by the type name of its node in the project tree (`<efma>`, `<ef_total>`, ...) instead.
- `others`: patterns for all the other objects. Every object must match `titles` or `others`, and not both; one
  that matches neither raises with the file and the title. The check is made per project when it is read
  (`open_array` reads only the first), so list the titles of all projects before binding (`harness describe`
  cannot: unpack a project and look for `<t2_area_list title=`).
- `image_suffix`: see above.

One class is one binding label with its own `titles`; overlapping classes (mitochondria inside an endfoot) need
no paint order this way.

## Limits

- Area lists only. Balls, labels, polylines, profiles, connectors and trees are ignored.
- One layer, one image placement, affine patch transform only; a montage, a stack project or a patch with a lens
  or other coordinate transform raises. Nested layer sets raise.
- Path data other than `M`, `L`, `z` with plain decimal numbers raises (TrakEM2 writes nothing else).
- The image is never opened, so nothing checks that its size is the patch's `o_width` x `o_height`; the harness
  checks label against raw shapes. All projects of one array must have the same image size.
- Instances are not numbered: `EF_1` and `EF_2` both become 1.
- The DTD at the top of the file is skipped, not parsed; the XML is read from `<trakem2>` on.
