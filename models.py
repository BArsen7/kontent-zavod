from datetime import datetime
from typing import Optional
import json

from sqlalchemy import String, Integer, ForeignKey, DateTime, Text, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class User(Base):
    """Модель пользователя системы (администратора сообществ)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    first_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    photo: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    communities: Mapped[list["UserCommunity"]] = relationship(
        "UserCommunity", back_populates="user", cascade="all, delete-orphan"
    )


class UserCommunity(Base):
    """Модель связи пользователя с сообществами (права доступа)."""

    __tablename__ = "user_communities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    group_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    group_token: Mapped[str] = mapped_column(String(500), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_post: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="communities")


class PlatformAccount(Base):
    """Модель аккаунта социальной сети."""

    __tablename__ = "platform_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    access_token: Mapped[str] = mapped_column(String(500), nullable=False)
    config_json: Mapped[Optional[str]] = mapped_column(JSON, nullable=True)

    # Убираем relationship с Post, так как в модели Post нет foreign key на platform_accounts
    # posts: Mapped[list["Post"]] = relationship("Post", back_populates="account")


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
    content_plan_period_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("content_plan_periods.id"), nullable=True, index=True
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
    content_plan_period: Mapped[Optional["ContentPlanPeriod"]] = relationship(
        "ContentPlanPeriod", back_populates="posts"
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


class ContentPlanPeriod(Base):
    """Модель периода контент-плана (неделя, 2 недели, месяц и т.д.)."""

    __tablename__ = "content_plan_periods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    period_type: Mapped[str] = mapped_column(String(50), nullable=False, default="week")  # week, two_weeks, month
    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)  # draft, active, completed, archived
    community_info: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Информация о сообществе
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship("User", backref="content_plan_periods")
    posts: Mapped[list["Post"]] = relationship(
        "Post", back_populates="content_plan_period", cascade="all, delete-orphan"
    )
    chat_messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage", back_populates="content_plan_period", cascade="all, delete-orphan"
    )


class ChatMessage(Base):
    """Модель сообщения в чате с контент-менеджером."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_plan_period_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("content_plan_periods.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user, assistant, system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    content_plan_period: Mapped["ContentPlanPeriod"] = relationship("ContentPlanPeriod", back_populates="chat_messages")
