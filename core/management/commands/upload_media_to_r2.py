"""Copy the local media/ folder (ad pictures, videos, profile pictures) to Cloudflare R2.

Run once after switching USE_R2=true, so files uploaded before the switch keep working:

    python manage.py upload_media_to_r2

Files already in R2 are skipped, so it is safe to run again. Private files (ID documents, chat files)
are never uploaded.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Upload the local media/ folder to Cloudflare R2 (skips files that are already there)."

    def handle(self, *args, **options):
        if not settings.USE_R2:
            raise CommandError("USE_R2 is not true in .env – nothing to upload to.")
        root = Path(settings.MEDIA_ROOT)
        files = [p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")]
        self.stdout.write(f"{len(files)} local files in {root}")

        def upload(path: Path) -> str:
            name = path.relative_to(root).as_posix()
            if default_storage.exists(name):
                return "skipped"
            with path.open("rb") as fh:
                saved = default_storage.save(name, File(fh, name=path.name))
            return "uploaded" if saved == name else f"renamed:{saved}"

        counts: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for result in pool.map(upload, files):
                key = result.split(":")[0]
                counts[key] = counts.get(key, 0) + 1
        self.stdout.write(self.style.SUCCESS(", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "nothing to do"))
