"""Convert a portrait photo to ASCII art for the profile SVG.

Crops to a face-centered square, enhances contrast and edges, then samples
brightness through a 10-level ramp into a 44x25 grid that fits the SVG's
left column.
"""
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
#Image:open and manipulate images
#ImageOps:Operations like auto-contrast and utility
#ImageEnhance:Enhance contrast 
#ImageFilter:Apply filters like sharpen

SRC = '1.png'
COLS = 60
ROWS = 30
CHAR_ASPECT = 2.0  # consolas chars are ~2x taller than wide
#height is twice of the width of the character in the font used for ASCII art. This is important for maintaining the correct aspect ratio when converting images to ASCII art.
# Face crop as fractions of source dimensions (tuned for this screenshot).
# Image looks roughly: shoulders bottom, hair top, face roughly centered horizontally.
FACE_BOX = (0.10, 0.00, 0.90, 0.79)  # (left, top, right, bottom) fractions

RAMP = '@%#*+=-:. '
#characters used to represent different levels of brightness in the image. The characters are ordered from darkest to lightest, with '@' representing the darkest areas and ' ' (space) representing the lightest areas.


def prep(img: Image.Image) -> Image.Image:
    w, h = img.size
    l, t, r, b = FACE_BOX
    img = img.crop((int(l * w), int(t * h), int(r * w), int(b * h)))
    img = img.convert('L')
    img = ImageOps.autocontrast(img, cutoff=3)
    img = ImageEnhance.Contrast(img).enhance(1.25)
    img = img.filter(ImageFilter.SHARPEN)
    # Match target aspect ratio (COLS / (ROWS * CHAR_ASPECT))
    target_ratio = COLS / (ROWS * CHAR_ASPECT)
    w, h = img.size
    src_ratio = w / h
    if src_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    return img.resize((COLS, ROWS), Image.LANCZOS)


def to_ascii(invert: bool) -> str:
    img = prep(Image.open(SRC))
    lines = []
    for y in range(ROWS):
        row = []
        for x in range(COLS):
            v = img.getpixel((x, y))
            scaled = v if invert else 255 - v
            idx = int((scaled / 255) * (len(RAMP) - 1))
            row.append(RAMP[idx])
        lines.append(''.join(row))
    return '\n'.join(lines)


if __name__ == '__main__':
    dark = to_ascii(invert=False)
    light = to_ascii(invert=True)
    with open('ascii_dark.txt', 'w', encoding='utf-8') as f:
        f.write(dark)
    with open('ascii_light.txt', 'w', encoding='utf-8') as f:
        f.write(light)
    print('=== DARK MODE ===')
    print(dark)
    print()
    print('=== LIGHT MODE ===')
    print(light)