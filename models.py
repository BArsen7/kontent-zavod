from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, ForeignKey, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class PlatformAccount(Base):
    """Модель аккаунта социальной сети."""

    __tablename__ = "platform_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    access_token: Mapped[str] = mapped_column(String(500), nullable=False)
    config_json: Mapped[Optional[str]] = mapped_column(JSON, nullable=True)

    posts: Mapped[list["Post"]] = relationship("Post", back_populates="account")


class ContentPlan(Base):
    """Модель контент-плана на неделю."""

    __tablename__ = "content_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    week_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    posts: Mapped[list["Post"]] = relationship(
        "Post", back_populates="content_plan", cascade="all, delete-orphan"
    )


class Post(Base):
    """Модель поста для публикации."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("content_plans.id"), nullable=False, index=True
    )
    post_type: Mapped[str] = mapped_column(String(50), nullable=False)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    text_draft: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_final: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    image_source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), default="draft", nullable=False, index=True
    )
    publish_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    content_plan: Mapped["ContentPlan"] = relationship(
        "ContentPlan", back_populates="posts"
    )
    stats: Mapped[list["PostStats"]] = relationship(
        "PostStats", back_populates="post", cascade="all, delete-orphan"
    )


class PostStats(Base):
    """Модель статистики поста."""

    __tablename__ = "post_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("posts.id"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    likes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    comments: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    post: Mapped["Post"] = relationship("Post", back_populates="stats")
