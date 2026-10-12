import json, tempfile, unittest, zlib
from pathlib import Path

import numpy as np

import build_pyramid as B


class PyramidTests(unittest.TestCase):
    def test_coarsen_is_mean_of_valid_cells(self):
        a = np.array([[1, 3, np.nan], [5, 7, np.nan], [np.nan, 2, 4]], dtype="float32")
        c = B.coarsen(a)
        self.assertEqual(c.shape, (2, 2))
        self.assertAlmostEqual(float(c[0, 0]), 4.0)          # mean of 1, 3, 5, 7
        self.assertTrue(np.isnan(c[0, 1]))                   # all four missing
        self.assertAlmostEqual(float(c[1, 0]), 2.0)          # only one valid cell
        self.assertAlmostEqual(float(c[1, 1]), 4.0)

    def test_block_mean_crops_and_ignores_missing(self):
        a = np.arange(35, dtype="float32").reshape(5, 7)
        a[0, 0] = np.nan
        m = B.block_mean(a, 2)
        self.assertEqual(m.shape, (2, 3))                    # the odd last row and column are dropped
        self.assertAlmostEqual(float(m[0, 0]), (1 + 7 + 8) / 3, places=5)
        self.assertAlmostEqual(float(m[1, 2]), (18 + 19 + 25 + 26) / 4)

    def test_pack_round_trip_and_fill(self):
        a = np.array([[0.0, 1.2345, np.nan]], dtype="float32")
        q = B.pack(a, 0.001, 0.0)
        self.assertEqual(q.dtype.str, "<i2")
        self.assertEqual(int(q[0, 2]), B.FILL)
        np.testing.assert_allclose(q[0, :2] * 0.001, [0.0, 1.2345], atol=0.0005)
        self.assertGreater(int(B.pack(np.array([[-1e9]], dtype="float32"), 0.001, 0.0)[0, 0]), B.FILL + 1)

    def test_store_layout_and_empty_chunks(self):
        a = np.full((600, 300), np.nan, dtype="float32")
        a[:256, :256] = 2.0                                  # only the first chunk has data
        with tempfile.TemporaryDirectory() as t:
            root, meta = Path(t), {}
            layer = B.write_layer(root, "depth", a, 3.0, 0.001, 0.0, {"label": "x", "units": "m"}, meta)
            self.assertEqual(sorted(p.name for p in (root / "depth" / "0").iterdir()), [".zarray", ".zattrs", "0.0"])
            z = json.loads((root / "depth" / "0" / ".zarray").read_text())
            self.assertEqual((z["zarr_format"], z["shape"], z["chunks"], z["dtype"], z["fill_value"]),
                             (2, [600, 300], [256, 256], "<i2", B.FILL))
            raw = np.frombuffer(zlib.decompress((root / "depth" / "0" / "0.0").read_bytes()), dtype="<i2")
            self.assertEqual(raw.size, 256 * 256)
            self.assertTrue((raw == 2000).all())
            self.assertEqual([d["cell_m"] for d in layer["levels"]], [3.0, 6.0, 12.0])
            self.assertEqual(layer["levels"][1]["shape"], [300, 150])
            self.assertIn("depth/1/.zarray", meta)

    def test_readable_by_the_zarr_library(self):
        try:
            import zarr
        except ImportError:
            self.skipTest("zarr not installed")
        rng = np.random.default_rng(0)
        a = rng.random((700, 520)).astype("float32") * 3
        a[100:200, 50:400] = np.nan
        with tempfile.TemporaryDirectory() as t:
            root, meta = Path(t) / "s.zarr", {}
            root.mkdir()
            (root / ".zgroup").write_text(json.dumps({"zarr_format": 2}))
            B.write_layer(root, "depth", a, 3.0, 0.001, 0.0, {"label": "x", "units": "m"}, meta)
            arr = zarr.open_group(str(root), mode="r")["depth/0"]
            stored = np.asarray(arr[:])
            self.assertEqual(stored.shape, a.shape)
            back = np.where(stored == B.FILL, np.nan, stored * 0.001)
            np.testing.assert_allclose(back, a, atol=0.0006, equal_nan=True)


if __name__ == "__main__":
    unittest.main()
