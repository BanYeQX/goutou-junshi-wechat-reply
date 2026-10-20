"""The vectorised frame comparison counts exactly what the old loop counted."""
import random
import unittest

from hosting_run import _ratio
from hosting_session import differing_bytes


def slow(old, new):
    return sum(1 for a, b in zip(old, new) if a != b)


class DifferingBytesTests(unittest.TestCase):
    def test_same_count_as_the_pure_python_loop(self):
        rng = random.Random(20261020)
        for size in (1, 7, 4096, 100_003):
            old = bytes(rng.randrange(256) for _ in range(size))
            new = bytearray(old)
            for i in rng.sample(range(size), max(1, size // 37)):
                new[i] = (new[i] + rng.randrange(1, 256)) % 256
            with self.subTest(size=size):
                self.assertEqual(differing_bytes(old, bytes(new)), slow(old, bytes(new)))

    def test_identical_and_all_different(self):
        self.assertEqual(differing_bytes(b"abc", b"abc"), 0)
        self.assertEqual(differing_bytes(b"\x00" * 9, b"\x01" * 9), 9)

    def test_ratio_keeps_its_edge_cases(self):
        self.assertEqual(_ratio(b"", b""), 1.0)            # nothing to compare: treat as changed
        self.assertEqual(_ratio(b"ab", b"abc"), 1.0)       # size mismatch: changed
        self.assertAlmostEqual(_ratio(b"abcd", b"abXd"), 0.25)


if __name__ == "__main__":
    unittest.main()


class RegionChangeTests(unittest.TestCase):
    def test_in_place_comparison_equals_cropping(self):
        from PIL import Image, ImageDraw
        from hosting_session import region_change
        from wechat_capture import PixelImage

        def pixel(image):
            rgba = image.convert("RGBA")
            return PixelImage(rgba.width, rgba.height, rgba.tobytes("raw", "BGRA"))
        one = Image.new("RGB", (300, 200), (30, 30, 31))
        two = one.copy()
        ImageDraw.Draw(two).rectangle((50, 60, 140, 120), fill=(200, 80, 80))
        ImageDraw.Draw(two).rectangle((0, 0, 20, 20), fill=(9, 9, 9))       # outside the box
        a, b = pixel(one), pixel(two)
        box = (40, 50, 290, 190)
        cropped = differing_bytes(a.crop(box).tobytes(), b.crop(box).tobytes()) / len(a.crop(box).tobytes())
        self.assertAlmostEqual(region_change(a, b, box), cropped)
        self.assertEqual(region_change(a, a, box), 0.0)
