# SnowEx streaming tiles prototype

A test of loading SnowEx terrain, LiDAR snow depth and model predictions in the browser as
tiles fetched on demand, for one site (Banner Summit, 2021-03-15).

- `index.html` is the viewer. It holds no data.
- `banner_summit.zarr/` is a Zarr (version 2) store: each layer is a pyramid of levels, each
  level is cut into 256 x 256 chunks, and each chunk is one small compressed file.

Open the page, zoom in, and the viewer fetches only the chunks in view at the detail the zoom
needs. Predictions are leave-one-site-out: the models never saw this site.

Data: [SnowEx Field Atlas v1.0.05](https://github.com/cryogars/cryogars-atlas/tree/v1.0.05).
Model results: https://anantgahlaut.github.io/cryogars-atlas-results/
