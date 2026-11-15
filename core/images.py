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
