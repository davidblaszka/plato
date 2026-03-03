"""
Cloudflare R2 storage service (S3-compatible).
Handles image upload, validation, and URL generation.
"""
import boto3
from fastapi import HTTPException
import uuid
import io
import asyncio
from functools import partial
from PIL import Image
from botocore.config import Config
from app.core.config import settings

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
AVATAR_SIZE = (256, 256)
THUMBNAIL_MAX = 1200

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _get_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _process_image(data: bytes, is_avatar: bool = False) -> bytes:
    """Resize and convert image to JPEG."""
    img = Image.open(io.BytesIO(data))

    # Normalize to RGB
    if img.mode in ("RGBA", "P", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if is_avatar:
        w, h = img.size
        m = min(w, h)
        img = img.crop(((w - m) // 2, (h - m) // 2, (w + m) // 2, (h + m) // 2))
        img = img.resize(AVATAR_SIZE, Image.LANCZOS)
    else:
        img.thumbnail((THUMBNAIL_MAX, THUMBNAIL_MAX), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()


def _do_upload(data: bytes, key: str):
    """Synchronous upload — runs in thread executor."""
    client = _get_client()
    client.put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=data,
        ContentType="image/jpeg",
        CacheControl="public, max-age=31536000",
    )


async def upload_image(
    data: bytes,
    content_type: str,
    folder: str,
    is_avatar: bool = False,
) -> str:
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported image type: {content_type}")
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")

    # Process in thread (CPU-bound)
    loop = asyncio.get_event_loop()
    processed = await loop.run_in_executor(
        None, partial(_process_image, data, is_avatar)
    )

    key = f"{folder}/{uuid.uuid4()}.jpg"

    # Upload in thread (I/O-bound but blocking)
    await loop.run_in_executor(None, partial(_do_upload, processed, key))

    return f"{settings.r2_public_url}/{key}"


async def delete_image(url: str):
    try:
        key = url.replace(f"{settings.r2_public_url}/", "")
        loop = asyncio.get_event_loop()
        client = _get_client()
        await loop.run_in_executor(
            None, partial(client.delete_object, Bucket=settings.r2_bucket, Key=key)
        )
    except Exception:
        pass
