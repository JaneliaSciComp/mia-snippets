# muvit-2d

The first tensorizer: the sample `weigertlab/muvit`'s `MuViTDataset` expects for 2D training.

- `img (L, C, P, P) float32`: each level's patch, every channel, scaled per level and channel to `[0, 1]` by the
  1st and 99th percentiles of the patch (`normalize: percentile`, the default), or divided by the dtype maximum
  (`normalize: none`). Percentiles are per patch because the pool mixes uint8 and uint16 data.
- `bbox (L, 2, 2) float32`: `[[y0, x0], [y1, x1]]` in nanometres, first and last pixel of the patch at that level,
  from the pyramid's scale and translation. The levels share one world point, which is what MuViT's rotary
  embedding needs.
- `label/<key> (L, P, P) int32`: every label layer of the volume, nearest-sampled at each level by the pyramid.
- `pixel_size (L, 2) float32`: nanometres per pixel (y, x) per level.
- `meta`: pyramid path, record id, descriptor, levels, patch, plane, centre, normalisation.

Limits: 2D only (the plane is one z index); no resampling to a target voxel size; no `scope: volume|dataset`
statistics yet.
