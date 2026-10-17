"""Repository layer for Postgres storage.

Split by aggregate so each module stays small; the historical flat import path
(``hippo.repositories``) still works because everything is re-exported here.
"""

from __future__ import annotations

from .account import AccountRepository, GroupRepository
from .annotation import AnnotationRepository
from .apikey import ApiKeyRepository
from .article import (
    ARTICLE_CONTENT_PRESENT_SQL,
    ArticleDocumentRepository,
    ArticleRepository,
    DownloadAttemptRepository,
)
from .audit import AuditRepository
from .chat import ChatRepository
from .image import ArticleImageTarget, ImageRepository
from .knowledge import KnowledgeRepository
from .llm import LlmProviderRepository, mask_key
from .media import ArticleMediaRepository, MediaJobRepository, UserCredentialRepository
from .meta import MetaRepository
from .queue import ArticleQueueRepository
from .read import ArticleReadRepository
from .session import UserSessionRepository
from .star import ArticleStarRepository
from .subscription import SubscriptionRepository
from .token import UserTokenRepository
from .user import UserRepository

__all__ = [
    'ARTICLE_CONTENT_PRESENT_SQL',
    'AccountRepository',
    'AnnotationRepository',
    'ApiKeyRepository',
    'ArticleDocumentRepository',
    'ArticleImageTarget',
    'ArticleMediaRepository',
    'ArticleQueueRepository',
    'ArticleReadRepository',
    'ArticleRepository',
    'ArticleStarRepository',
    'AuditRepository',
    'ChatRepository',
    'DownloadAttemptRepository',
    'GroupRepository',
    'ImageRepository',
    'KnowledgeRepository',
    'LlmProviderRepository',
    'MediaJobRepository',
    'MetaRepository',
    'SubscriptionRepository',
    'UserCredentialRepository',
    'UserRepository',
    'UserSessionRepository',
    'UserTokenRepository',
    'mask_key',
]
