# muvit-2d

The first tensorizer: the sample `weigertlab/muvit`'s `MuViTDataset` expects for 2D training.

- `img (L, C, P, P) float32`: each level's patch, scaled per level and channel to `[0, 1]` by the 1st and 99th
  percentiles of the patch (`normalize: percentile`, the default), or divided by the dtype maximum
  (`normalize: none`). Percentiles are per patch because the pool mixes uint8 and uint16 data. `C` is the stored
  channel count, or what the plan row's channel recipe makes of it (`channels` param, applied after scaling):
  `{op: channel, index: k}`, `mean` or `luminance` (Rec. 601 over the first three) give one channel; `replicate`,
  `rgb` (first three) or `additive` (channel i lights colour i mod 3, summed and clipped) give three.
- `bbox (L, 2, 2) float32`: `[[y0, x0], [y1, x1]]` in nanometres, first and last pixel of the patch at that level,
  from the pyramid's scale and translation. The levels share one world point, which is what MuViT's rotary
  embedding needs.
- `label/<key> (L, P, P) int32`: every label layer of the volume, nearest-sampled at each level by the pyramid.
- `pixel_size (L, 2) float32`: nanometres per pixel (y, x) per level.
- `meta`: pyramid path, record id, descriptor, levels, patch, plane, centre, normalisation, channel recipe.

Limits: 2D only (the plane is one z index); no resampling to a target voxel size; no `scope: volume|dataset`
statistics yet.
