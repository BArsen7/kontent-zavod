"""
VKontakte (VK) Publisher module.
Handles publishing posts to VK groups via the VK API (version 5.199+).

Работает с сервисным ключом доступа сообщества (Community Service Token)
с правами: wall, photos, messages, groups.
group_id внутри системы всегда положительный; для методов API, требующих
owner_id (wall.post и т.п.), явно передаётся -abs(group_id).
"""
import logging
import os
from typing import Any, Dict, List, Optional

import vk_api
from vk_api.exceptions import ApiError
from vk_api.upload import VkUpload

from .base_publisher import BasePublisher
from vk_errors import (
    create_vk_session,
    describe_api_error,
    format_photo_attachment,
    owner_id_for_group,
    positive_group_id,
    with_retry,
)

logger = logging.getLogger(__name__)


class VKPublisher(BasePublisher):
    """
    Publisher for VKontakte (VK) social media platform.
    Handles photo upload and wall posting for VK groups.
    """

    def __init__(self, token: str, group_id: int):
        """
        Initialize the VK Publisher.

        Args:
            token: Сервисный ключ доступа сообщества (Community Service Token)
                с правами wall, photos, messages, groups.
            group_id: ID VK-группы (положительное число; нормализуется через abs()).
        """
        self.token = token.strip()
        self.group_id: int = positive_group_id(group_id)
        self._vk_session: Optional[vk_api.VkApi] = None
        self._upload: Optional[VkUpload] = None

        logger.info(f"Initializing VKPublisher for group_id={self.group_id}")

    @property
    def vk_session(self) -> vk_api.VkApi:
        """Lazy initialization of VK session (api_version=5.199)."""
        if self._vk_session is None:
            self._vk_session = create_vk_session(self.token)
            try:
                # Validate token by getting account info
                self._vk_session.auth()
                logger.debug("VK session authenticated successfully")
            except vk_api.AuthError as e:
                logger.error(
                    f"VK authentication failed. Проверьте, что передан сервисный "
                    f"ключ доступа сообщества (Настройки → Работа с API): {e}"
                )
                raise
        return self._vk_session

    @property
    def upload(self) -> VkUpload:
        """Lazy initialization of VkUpload helper."""
        if self._upload is None:
            self._upload = VkUpload(self.vk_session)
        return self._upload

    def upload_photo(self, image_path: str) -> str:
        """
        Upload a photo to VK servers and return the attachment string.

        Args:
            image_path: Local file path to the image to upload.

        Returns:
            Attachment string in format "photo{owner_id}_{photo_id}".

        Raises:
            FileNotFoundError: If the image file does not exist.
            Exception: If the upload fails for any reason.
        """
        if not os.path.exists(image_path):
            error_msg = f"Image file not found: {image_path}"
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)

        logger.info(f"Uploading photo to VK: {image_path}")

        try:
            # Upload photo for wall posting (photo_wall album of the group).
            # retry на случай rate-limit (код 6).
            uploaded_photo: List[Dict[str, Any]] = with_retry(
                self.upload.photo_wall,
                photo=image_path,
                group_id=self.group_id,
                scope=f"photo_wall(g={self.group_id})",
            )

            if not uploaded_photo or len(uploaded_photo) == 0:
                error_msg = "VK upload returned empty response"
                logger.error(error_msg)
                raise Exception(error_msg)

            photo_info = uploaded_photo[0]
            owner_id = photo_info.get('owner_id')
            photo_id = photo_info.get('id')

            if not owner_id or not photo_id:
                error_msg = f"Invalid photo data received from VK: {photo_info}"
                logger.error(error_msg)
                raise Exception(error_msg)

            # Формат вложения по спецификации VK API 5.199:
            # "photo-{abs(owner_id)}_{photo_id}" (например, "photo-123456_789012").
            attachment = format_photo_attachment(owner_id, photo_id)
            logger.info(f"Photo uploaded successfully. Attachment: {attachment}")
            return attachment

        except ApiError as e:
            error_msg = describe_api_error(e, scope=f"photo upload g={self.group_id}")
            logger.error(error_msg)
            raise Exception(error_msg)
        except Exception as e:
            error_msg = f"Failed to upload photo to VK: {e}"
            logger.error(error_msg)
            raise

    def publish(self, post: Dict[str, Any]) -> Dict[str, Any]:
        """
        Publish a post to the VK group wall.

        Args:
            post: Dictionary containing:
                - "text": str (required) - Post text content.
                - "image_path": str (optional) - Path to image file.

        Returns:
            Dictionary with publication result:
                - "success": bool
                - "post_id": int (if successful)
                - "platform": str ("vk")
                - "error": str (if failed)
        """
        text = post.get("text", "")
        image_path = post.get("image_path")

        if not text:
            error_msg = "Post text is required for VK publication"
            logger.error(error_msg)
            return {
                "success": False,
                "platform": "vk",
                "error": error_msg
            }

        logger.info(f"Publishing post to VK group {self.group_id}")
        logger.debug(f"Post text length: {len(text)} characters")

        try:
            vk_api_instance = self.vk_session.get_api()
            attachments: List[str] = []

            # Upload and attach photo if provided
            if image_path:
                try:
                    attachment_str = self.upload_photo(image_path)
                    attachments.append(attachment_str)
                    logger.debug(f"Photo attached: {attachment_str}")
                except Exception as e:
                    logger.warning(f"Photo upload failed, proceeding with text only: {e}")
                    # Continue with text-only post

            # Prepare wall.post parameters.
            # owner_id для групп всегда отрицательный: -abs(group_id).
            post_params: Dict[str, Any] = {
                "owner_id": owner_id_for_group(self.group_id),
                "message": text,
            }

            if attachments:
                post_params["attachments"] = ",".join(attachments)

            logger.info(
                f"Calling VK wall.post with params: owner_id={post_params['owner_id']}, "
                f"attachments={bool(attachments)}"
            )

            # Publish the post (retry при rate-limit, код 6)
            response = with_retry(
                vk_api_instance.wall.post,
                scope=f"wall.post(g={self.group_id})",
                **post_params,
            )

            post_id = response.get("post_id")

            if post_id:
                logger.info(f"Post published successfully to VK. Post ID: {post_id}")
                return {
                    "success": True,
                    "post_id": post_id,
                    "platform": "vk",
                    "url": f"https://vk.com/wall{owner_id_for_group(self.group_id)}_{post_id}"
                }
            else:
                error_msg = f"VK API returned no post_id in response: {response}"
                logger.error(error_msg)
                return {
                    "success": False,
                    "platform": "vk",
                    "error": error_msg
                }

        except vk_api.exceptions.AuthError as e:
            error_msg = (
                f"VK authentication error: {e}. Проверьте сервисный ключ доступа сообщества."
            )
            logger.error(error_msg)
            return {
                "success": False,
                "platform": "vk",
                "error": error_msg
            }
        except ApiError as e:
            # with_retry уже залогировал детали; здесь формируем ответ.
            error_code = getattr(e, 'code', 'unknown')
            error_msg = f"VK API error (code {error_code}): {e}"
            return {
                "success": False,
                "platform": "vk",
                "error": error_msg
            }
        except Exception as e:
            error_msg = f"Unexpected error publishing to VK: {e}"
            logger.error(error_msg)
            return {
                "success": False,
                "platform": "vk",
                "error": error_msg
            }
