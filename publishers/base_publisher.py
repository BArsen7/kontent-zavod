"""
Base publisher module.
Defines the abstract interface for all social media publishers.
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict

logger = logging.getLogger(__name__)


class BasePublisher(ABC):
    """
    Abstract base class for all social media publishers.
    All specific platform publishers (VK, Telegram, etc.) must inherit from this class.
    """

    @abstractmethod
    def publish(self, post: Dict[str, Any]) -> Dict[str, Any]:
        """
        Publish a post to the social media platform.

        Args:
            post: A dictionary containing post data.
                  Expected keys:
                  - "text": str (required) - The text content of the post.
                  - "image_path": str (optional) - Local path to the image file.

        Returns:
            A dictionary containing the result of the publication.
            Expected keys:
            - "success": bool
            - "post_id": str|int (if successful)
            - "error": str (if failed)
            - "platform": str
        """
        pass
