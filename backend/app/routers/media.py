import logging
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from pydantic import BaseModel
from app.core.security import get_current_user
from app.models.user import User
from app.services.storage import upload_image, ALLOWED_TYPES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/media", tags=["media"])


class UploadResponse(BaseModel):
    url: str


async def _upload(file: UploadFile, folder: str, is_avatar: bool = False) -> str:
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Must be JPEG, PNG, WebP, or GIF")
    data = await file.read()
    logger.info(f"Uploading {len(data)} bytes to {folder}/")
    try:
        url = await upload_image(data, file.content_type, folder=folder, is_avatar=is_avatar)
        logger.info(f"Upload success: {url}")
        return url
    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@router.post("/upload/avatar", response_model=UploadResponse)
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    url = await _upload(file, "avatars", is_avatar=True)
    return UploadResponse(url=url)


@router.post("/upload/post", response_model=UploadResponse)
async def upload_post_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    url = await _upload(file, "posts")
    return UploadResponse(url=url)


@router.post("/upload/message", response_model=UploadResponse)
async def upload_message_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    url = await _upload(file, "messages")
    return UploadResponse(url=url)


@router.post("/upload/feed", response_model=UploadResponse)
async def upload_feed_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    url = await _upload(file, "feed")
    return UploadResponse(url=url)
