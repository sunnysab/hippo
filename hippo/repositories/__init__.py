"""Repository layer for Postgres storage.

Split by aggregate so each module stays small; the historical flat import path
(``hippo.repositories``) still works because everything is re-exported here.
"""

from __future__ import annotations

from .account import AccountRepository, GroupRepository
from .annotation import AnnotationRepository
from .article import (
    ARTICLE_CONTENT_PRESENT_SQL,
    ArticleDocumentRepository,
    ArticleRepository,
    DownloadAttemptRepository,
)
from .audit import AuditRepository
from .image import ArticleImageTarget, ImageRepository
from .llm import LlmProviderRepository, mask_key
from .meta import MetaRepository
from .queue import ArticleQueueRepository
from .session import UserSessionRepository
from .subscription import SubscriptionRepository
from .token import UserTokenRepository
from .user import UserRepository

__all__ = [
    'ARTICLE_CONTENT_PRESENT_SQL',
    'AccountRepository',
    'AnnotationRepository',
    'ArticleDocumentRepository',
    'ArticleImageTarget',
    'ArticleQueueRepository',
    'ArticleRepository',
    'AuditRepository',
    'DownloadAttemptRepository',
    'GroupRepository',
    'ImageRepository',
    'LlmProviderRepository',
    'MetaRepository',
    'SubscriptionRepository',
    'UserRepository',
    'UserSessionRepository',
    'UserTokenRepository',
    'mask_key',
]
