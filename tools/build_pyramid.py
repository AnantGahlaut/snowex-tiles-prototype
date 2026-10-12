#!/usr/bin/env python3
"""Build a tiled, multi-resolution Zarr store of one site for the streaming viewer.

    python build_pyramid.py --site banner_summit --archive <site>.enriched.h5 --date 20210315
        --maps <dir of <site>_<date>_<N>m_loso_prediction.tif> --out <dir>

Writes <out>/<site>.zarr, a plain Zarr version 2 store (no Zarr library needed to
write it, any Zarr reader can open it):

    <site>.zarr/
      .zgroup  .zattrs  .zmetadata  zmetadata.json     site description + every array's metadata
      elevation/0 .. elevation/5                        level 0 = 3 m, each next level 2x coarser
      snow_depth_<date>/0 ..                            LiDAR snow depth
      rf_<N>m/0 ..                                      random-forest prediction made at N m blocks
      error_<N>m/0 ..                                   prediction minus LiDAR (same blocks)
      lidar_<N>m/0 ..                                   LiDAR snow depth averaged to the same N m blocks:
                                                        the values the model at that block size is scored against
      elevation_<N>m/0 ..                               elevation averaged to the same N m blocks, so the
                                                        ground under a model's cells has the model's detail

Each level is one 2-D array cut into 256 x 256 chunks. A chunk is one small file
named "<row>.<col>", zlib-compressed little-endian int16. Values are stored as
integers: value = stored * scale_factor + add_offset; the fill value marks
"no data". Chunks that are entirely "no data" are not written.
Coarser levels are the mean of the valid cells below them. Archives are opened read-only.
"""
from __future__ import annotations

import argparse, json, re, zlib
from pathlib import Path

import numpy as np

CHUNK = 256
FILL = -32768
MIN_LEVEL_PIXELS = 96          # stop coarsening once a level is this small


def coarsen(a: np.ndarray) -> np.ndarray:
    """Mean of each 2 x 2 block, ignoring NaN; odd edges are padded with NaN."""
    h, w = a.shape
    p = np.full((h + h % 2, w + w % 2), np.nan, dtype="float32")
    p[:h, :w] = a
    blocks = p.reshape(p.shape[0] // 2, 2, p.shape[1] // 2, 2)
    valid = np.isfinite(blocks)
    n = valid.sum(axis=(1, 3))
    total = np.where(valid, blocks, 0).sum(axis=(1, 3), dtype="float64")
    return np.where(n > 0, total / np.maximum(n, 1), np.nan).astype("float32")


def block_mean(a: np.ndarray, k: int) -> np.ndarray:
    """Mean of each k x k block of valid cells, cropping partial edge blocks (as the prediction maps do)."""
    h, w = (a.shape[0] // k) * k, (a.shape[1] // k) * k
    blocks = a[:h, :w].reshape(h // k, k, w // k, k)
    valid = np.isfinite(blocks)
    n = valid.sum(axis=(1, 3))
    total = np.where(valid, blocks, 0).sum(axis=(1, 3), dtype="float64")
    return np.where(n > 0, total / np.maximum(n, 1), np.nan).astype("float32")


def pyramid(a: np.ndarray) -> list[np.ndarray]:
    levels = [a.astype("float32")]
    while min(levels[-1].shape) > MIN_LEVEL_PIXELS:
        levels.append(coarsen(levels[-1]))
    return levels


def pack(a: np.ndarray, scale: float, offset: float) -> np.ndarray:
    q = np.round((a.astype("float64") - offset) / scale)
    q = np.clip(q, FILL + 2, 32767)                 # FILL = no data, FILL + 1 is reserved for the viewer
    return np.where(np.isfinite(a), q, FILL).astype("<i2")


def write_array(root: Path, name: str, a: np.ndarray, scale: float, offset: float, attrs: dict, meta: dict) -> list:
    """One level as a Zarr v2 array. Returns the names of the chunk files written."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    q = pack(a, scale, offset)
    zarray = {"zarr_format": 2, "shape": list(q.shape), "chunks": [CHUNK, CHUNK], "dtype": "<i2",
              "compressor": {"id": "zlib", "level": 6}, "fill_value": FILL, "order": "C", "filters": None,
              "dimension_separator": "."}
    zattrs = {"_ARRAY_DIMENSIONS": ["y", "x"], "scale_factor": scale, "add_offset": offset, **attrs}
    (d / ".zarray").write_text(json.dumps(zarray))
    (d / ".zattrs").write_text(json.dumps(zattrs))
    meta[f"{name}/.zarray"], meta[f"{name}/.zattrs"] = zarray, zattrs
    written = []
    for r in range(0, q.shape[0], CHUNK):
        for c in range(0, q.shape[1], CHUNK):
            block = np.full((CHUNK, CHUNK), FILL, dtype="<i2")
            part = q[r:r + CHUNK, c:c + CHUNK]
            block[:part.shape[0], :part.shape[1]] = part
            if (block != FILL).any():
                (d / f"{r // CHUNK}.{c // CHUNK}").write_bytes(zlib.compress(block.tobytes(), 6))
                written.append(f"{r // CHUNK}.{c // CHUNK}")
    return written


def write_layer(root: Path, key: str, a: np.ndarray, cell_m: float, scale: float, offset: float, info: dict, meta: dict) -> dict:
    levels = pyramid(a)
    finite = a[np.isfinite(a)]
    lo, hi = (float(np.percentile(finite, 2)), float(np.percentile(finite, 98))) if finite.size else (0.0, 1.0)
    datasets, files = [], 0
    for i, lv in enumerate(levels):
        names = write_array(root, f"{key}/{i}", lv, scale, offset, {"cell_m": cell_m * 2 ** i}, meta)
        files += len(names)
        # `chunks` lists the chunk files that exist, so a reader never asks for an empty one
        datasets.append({"path": str(i), "cell_m": cell_m * 2 ** i, "shape": list(lv.shape), "chunks": names})
    layer = {"key": key, "cell_m": cell_m, "levels": datasets, "p2": lo, "p98": hi,
             "min": float(finite.min()) if finite.size else None, "max": float(finite.max()) if finite.size else None,
             "valid_cells": int(finite.size), "chunk_files": files,
             "scale_factor": scale, "add_offset": offset, **info}
    group_attrs = {"multiscales": [{"version": "0.1", "name": key, "datasets": [{"path": d["path"]} for d in datasets],
                                    "type": "mean of valid cells"}], **{k: v for k, v in layer.items() if k != "levels"}}
    (root / key / ".zgroup").write_text(json.dumps({"zarr_format": 2}))
    (root / key / ".zattrs").write_text(json.dumps(group_attrs))
    meta[f"{key}/.zgroup"], meta[f"{key}/.zattrs"] = {"zarr_format": 2}, group_attrs
    print(f"{key:24} {cell_m:5.0f} m  levels {len(levels)}  chunks {files:4d}  p2..p98 {lo:.2f}..{hi:.2f}", flush=True)
    return layer


def main(argv=None) -> int:
    import h5py, rasterio
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True)
    ap.add_argument("--archive", type=Path, required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--maps", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    root = args.out / f"{args.site}.zarr"
    root.mkdir(parents=True, exist_ok=True)
    meta, layers = {}, []

    with h5py.File(args.archive, "r") as f:
        ident = f["identification"].attrs
        epsg = int(ident["common_crs_epsg"])
        transform = [float(v) for v in ident["common_grid_transform"]]
        shape = [int(v) for v in ident["common_grid_shape"]]
        elev = f["science/LIDAR/DEM/grids/elevation"][...].astype("float32")
        depth = f[f"science/LIDAR/SD/{args.date}/snow_depth"][...].astype("float32")
    date = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:]}"
    layers.append(write_layer(root, "elevation", elev, 3.0, 0.1, float(np.round(np.nanmean(elev))),
                              {"label": "Elevation", "units": "m", "cmap": "terrain", "kind": "terrain"}, meta))
    layers.append(write_layer(root, f"snow_depth_{args.date}", depth, 3.0, 0.001, 0.0,
                              {"label": f"LiDAR snow depth · {date}", "units": "m", "cmap": "blues", "kind": "lidar"}, meta))
    elev_offset = float(np.round(np.nanmean(elev)))
    del depth

    found = {}
    for tif in args.maps.glob(f"{args.site}_{args.date}_*m_loso_prediction.tif"):
        found[int(re.search(r"_(\d+)m_loso", tif.name).group(1))] = tif
    for n in sorted(found):
        with rasterio.open(found[n]) as src:
            rf, lidar = src.read(2).astype("float32"), src.read(3).astype("float32")
        layers.append(write_layer(root, f"rf_{n}m", rf, float(n), 0.001, 0.0,
                                  {"label": f"Random forest prediction · {n} m blocks", "units": "m", "cmap": "blues",
                                   "kind": "prediction", "block_m": n}, meta))
        layers.append(write_layer(root, f"error_{n}m", rf - lidar, float(n), 0.001, 0.0,
                                  {"label": f"Prediction minus LiDAR · {n} m blocks", "units": "m", "cmap": "diverging",
                                   "kind": "error", "block_m": n}, meta))
        if n > 3:                         # at 3 m the plain snow-depth layer is shown
            layers.append(write_layer(root, f"lidar_{n}m", lidar, float(n), 0.001, 0.0,
                                      {"label": f"LiDAR snow depth · {n} m blocks", "units": "m", "cmap": "blues",
                                       "kind": "lidar_block", "block_m": n}, meta))
        if n % 3 == 0 and n > 3:          # at 3 m the plain `elevation` layer already is this
            layers.append(write_layer(root, f"elevation_{n}m", block_mean(elev, n // 3), float(n), 0.1, elev_offset,
                                      {"label": f"Elevation · {n} m blocks", "units": "m", "cmap": "terrain",
                                       "kind": "terrain_block", "block_m": n}, meta))

    site = {"site": args.site, "epsg": epsg, "transform_3m": transform, "shape_3m": shape, "lidar_date": date,
            "chunk": CHUNK, "fill_value": FILL, "layers": layers,
            "note": "Predictions are leave-one-site-out: the models never saw this site."}
    (root / ".zgroup").write_text(json.dumps({"zarr_format": 2}))
    (root / ".zattrs").write_text(json.dumps(site))
    meta[".zgroup"], meta[".zattrs"] = {"zarr_format": 2}, site
    consolidated = json.dumps({"zarr_consolidated_format": 1, "metadata": meta})
    (root / ".zmetadata").write_text(consolidated)            # the standard name, for Zarr tools
    (root / "zmetadata.json").write_text(consolidated)        # same content, for hosts that hide dot-files
    total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    print(f"wrote {root}: {len(layers)} layers, {sum(l['chunk_files'] for l in layers)} chunks, {total / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
