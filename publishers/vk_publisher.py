"""
VKontakte (VK) Publisher module.
Handles publishing posts to VK groups via the VK API.
"""
import logging
import os
from typing import Any, Dict

import vk_api
from vk_api.upload import VkUpload

from .base_publisher import BasePublisher

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
            token: VK API access token with appropriate permissions (wall, photos).
            group_id: ID of the VK group where posts will be published.
        """
        self.token = token
        self.group_id = group_id
        self._vk_session: vk_api.VkApi | None = None
        self._upload: VkUpload | None = None

        logger.info(f"Initializing VKPublisher for group_id={group_id}")

    @property
    def vk_session(self) -> vk_api.VkApi:
        """Lazy initialization of VK session."""
        if self._vk_session is None:
            self._vk_session = vk_api.VkApi(token=self.token)
            try:
                # Validate token by getting account info
                self._vk_session.auth()
                logger.debug("VK session authenticated successfully")
            except vk_api.AuthError as e:
                logger.error(f"VK authentication failed: {e}")
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
            # Upload photo to the group's album
            # For wall posts, we upload to the main album
            uploaded_photo = self.upload.photo_wall(
                photo=image_path,
                group_id=self.group_id
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

            # VK group owner IDs are negative in attachments
            attachment = f"photo-{abs(owner_id)}_{photo_id}"
            logger.info(f"Photo uploaded successfully. Attachment: {attachment}")
            return attachment

        except vk_api.exceptions.ApiError as e:
            error_msg = f"VK API error during photo upload: {e}"
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
            attachments = []

            # Upload and attach photo if provided
            if image_path:
                try:
                    attachment_str = self.upload_photo(image_path)
                    attachments.append(attachment_str)
                    logger.debug(f"Photo attached: {attachment_str}")
                except Exception as e:
                    logger.warning(f"Photo upload failed, proceeding with text only: {e}")
                    # Continue with text-only post

            # Prepare wall.post parameters
            post_params = {
                "owner_id": -self.group_id,  # Negative for groups
                "message": text,
            }

            if attachments:
                post_params["attachments"] = ",".join(attachments)

            logger.info(f"Calling VK wall.post with params: owner_id={-self.group_id}, attachments={bool(attachments)}")

            # Publish the post
            response = vk_api_instance.wall.post(**post_params)

            post_id = response.get("post_id")

            if post_id:
                logger.info(f"Post published successfully to VK. Post ID: {post_id}")
                return {
                    "success": True,
                    "post_id": post_id,
                    "platform": "vk",
                    "url": f"https://vk.com/wall-{self.group_id}_{post_id}"
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
            error_msg = f"VK authentication error: {e}"
            logger.error(error_msg)
            return {
                "success": False,
                "platform": "vk",
                "error": error_msg
            }
        except vk_api.exceptions.ApiError as e:
            error_code = e.code if hasattr(e, 'code') else 'unknown'
            error_msg = f"VK API error (code {error_code}): {e}"
            logger.error(error_msg)
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
