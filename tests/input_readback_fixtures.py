"""Synthetic RGB images only; never read a desktop, window, or real config."""
from PIL import Image, ImageChops, ImageDraw, ImageFont

FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
FONT_SIZE = 28
BACKGROUND = (30, 30, 30)
FOREGROUND = (220, 220, 220)


def font(size=FONT_SIZE):
    return ImageFont.truetype(FONT_PATH, size)


def baseline(size=FONT_SIZE):
    # The fixture font rasterizer's actual nonzero glyph-mask bbox, not editor
    # height, a percentage, or a measurement from a real WeChat image.
    box = font(size).getmask("收到").getbbox()
    return box[3] - box[1]


def caret_variants():
    """Known fixture ink boundary; not a real-window coordinate/calibration.

    Gap is empty columns after the exclusive text right edge. A touching
    caret starts exactly at that edge; gap 1 leaves one blank column.
    """
    with fixture() as image, Image.new("RGB", image.size, BACKGROUND) as background:
        with ImageChops.difference(image, background) as difference:
            right = difference.getbbox()[2]
    return (("no_caret", None), ("touching", right), ("gap_1px", right + 1), ("gap_8px", right + 8))


def fixture(text="收到", *, right=None, caret=None, border=False, size=FONT_SIZE,
            width=2087, height=74, left=22, top=20):
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    if text:
        draw.text((left, top), text, font=font(size), fill=FOREGROUND, anchor="lt")
    if right is not None:
        draw.text((width - 110, top), right, font=font(size), fill=FOREGROUND, anchor="lt")
    if caret is not None:
        # Ordinary vertical cursor is intentionally indistinguishable from a
        # bar/stroke in a single bitmap. Production must NOT erase it.
        draw.rectangle((caret, top, caret + 1, top + baseline(size) - 1), fill=FOREGROUND)
    if border:
        draw.rectangle((0, 0, width - 1, height - 1), outline=(90, 90, 90), width=1)
    return image
