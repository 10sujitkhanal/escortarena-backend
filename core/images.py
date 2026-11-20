"""Picture processing: validate, strip metadata (EXIF/GPS), resize and re-encode to WebP."""

from io import BytesIO

from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SIDE = 1600


class InvalidImage(ValueError):
    pass


def process_upload(upload) -> tuple[ContentFile, int, int]:
    if upload.size > settings.MAX_UPLOAD_BYTES:
        raise InvalidImage("Image is larger than 10 MB.")
    try:
        img = Image.open(upload)
        img.verify()
        upload.seek(0)
        img = Image.open(upload)
    except (UnidentifiedImageError, OSError) as exc:
        raise InvalidImage("File is not a valid image.") from exc

    # Apply EXIF rotation, then drop all metadata (location, camera serials).
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))

    out = BytesIO()
    img.save(out, format="WEBP", quality=82, method=4)
    return ContentFile(out.getvalue(), name="picture.webp"), img.width, img.height


def trim_logo(upload, max_width: int = 1200) -> ContentFile | None:
    """Cut empty borders off a logo (transparent, or the plain background colour of the corners) so it shows big
    in the header, and scale it to at most `max_width`. Returns a PNG, or None if the file is not an image."""
    from PIL import ImageChops

    try:
        upload.seek(0)
        img = Image.open(upload)
        img.load()
    except (UnidentifiedImageError, OSError):
        return None
    img = ImageOps.exif_transpose(img).convert("RGBA")
    alpha = img.getchannel("A")
    if alpha.getextrema()[0] < 250:  # has transparency: keep what is visible
        box = alpha.point(lambda a: 255 if a > 12 else 0).getbbox()
    else:  # solid background: everything that differs from the corner colour
        bg = Image.new("RGB", img.size, img.getpixel((0, 0))[:3])
        diff = ImageChops.difference(img.convert("RGB"), bg).convert("L").point(lambda v: 255 if v > 28 else 0)
        box = diff.getbbox()
    if box:
        pad = max(2, round(min(img.size) * 0.015))
        box = (max(0, box[0] - pad), max(0, box[1] - pad), min(img.width, box[2] + pad), min(img.height, box[3] + pad))
        img = img.crop(box)
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return ContentFile(out.getvalue(), name="logo.png")
