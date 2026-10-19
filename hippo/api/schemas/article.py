"""Article, highlight, transcript and archived-media schemas.

An article is anything hippo subscribed to: a WeChat post, a video, a podcast
episode. Its body lives in ``article_content`` as a list of content blocks whose
shape belongs to the source, so a block is documented at the level every source
shares and keeps whatever else the source put in it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .common import RowPayload

#: Highlight colours the API accepts; anything else is a 400.
AnnotationColor = Literal['default', 'yellow', 'green', 'blue', 'pink']


class ReadUpdateRequest(BaseModel):
    """``PUT /api/article/{article_id}/read``."""

    read: bool = Field(default=True, description='True marks read, false marks unread.', strict=True)


class StarUpdateRequest(BaseModel):
    """``PUT /api/article/{article_id}/star``."""

    starred: bool = Field(default=True, description='True stars, false unstars.', strict=True)


class AnnotationCreateRequest(BaseModel):
    """``POST /api/article/{article_id}/annotation``.

    ``prefix`` and ``suffix`` are the text around the quote, used to re-anchor it
    when the article is rendered again.
    """

    quote: str = Field(description='Selected text, at most 2000 characters.')
    prefix: str | None = Field(default=None, description='Up to 32 characters before the quote.')
    suffix: str | None = Field(default=None, description='Up to 32 characters after the quote.')
    note: str | None = Field(default=None, description='Optional reader note.')
    color: AnnotationColor = 'default'


class ReadStateOut(BaseModel):
    article_id: int
    read_at: str | None = Field(description='When it was marked read; null when unread.')


class StarStateOut(BaseModel):
    article_id: int
    starred_at: str | None = Field(description='When it was starred; null when not starred.')


class AnnotationOut(BaseModel):
    id: int
    article_id: int
    quote: str
    prefix: str
    suffix: str
    note: str
    color: str
    created_at: str | None = None


class AnnotationListOut(BaseModel):
    items: list[AnnotationOut]


class ArticleOut(BaseModel):
    """One article row, as the list and the detail return it.

    A public shape of the article query, closed on purpose: publishing a column
    is a decision, not the default when the row happens to carry one.
    """

    id: int
    biz: str
    article_id: str
    title: str
    item_show_type: int | None = Field(default=None, description="The source's own layout type.")
    author: str | None = None
    digest: str | None = None
    cover: int | str | None = Field(default=None, description='Cover image id, or the legacy URL.')
    link: str
    source_url: str | None = None
    publish_at: int | None = Field(default=None, description='Unix timestamp.')
    duration_seconds: int | None = None
    created_at: str | None = None
    read_at: str | None = Field(default=None, description='Read state of the caller.')
    starred_at: str | None = Field(default=None, description='Star state of the caller.')
    source: str | None = Field(default=None, description='Where the article came from.')
    content_kind: str = Field(description='article / video / audio / gallery / text / short.')
    account_nickname: str | None = None
    account_alias: str | None = None
    account_avatar: str | None = None
    account_avatar_url: str = Field(description='Same-origin avatar URL.')
    group_id: int | None = None
    group_name: str | None = None
    image_id: int | None = Field(default=None, description='Cover image row, when one is attached.')
    rank: float | None = Field(default=None, description='Relevance score, set when the query was a search.')


class FeedArticleOut(ArticleOut):
    """An article in the mixed feed.

    The same query, without the two fields the article list computes on the way
    out; a feed reader gets the stored columns only.
    """

    content_kind: str | None = None
    account_avatar_url: str = ''


class ItemShowTypeFacetOut(BaseModel):
    """One bar of the layout-type filter."""

    item_show_type: int
    count: int


class ArticleListOut(BaseModel):
    articles: list[ArticleOut]
    page: int
    page_size: int
    total: int
    item_show_type_facets: list[ItemShowTypeFacetOut] = Field(
        description='Counts per layout type; unknown types are counted in ``total`` only.'
    )


class FeedOut(BaseModel):
    """JSON form of ``GET /api/feed/mixed``; ``format=rss`` answers XML instead."""

    articles: list[FeedArticleOut]


class ContentBlockOut(RowPayload):
    """One block of an article body.

    ``type`` decides which of the other fields are set: a paragraph carries
    ``text`` (and ``t`` when it has a timestamp), an image carries ``image_id``,
    a video or audio block carries ``url`` (and ``cover_url``/``embed_url``/
    ``provider``/``video_id``).

    Unlike the row models above, this one keeps undeclared keys: a block is
    written by the source that produced it (WeChat markup, a Bilibili embed, a
    transcript), so the reader's copy stays faithful to what was stored. A body
    block never holds a credential.
    """

    type: str = Field(description='paragraph / heading / image / code / link / video / audio.')
    text: str | None = None
    level: int | None = Field(default=None, description='Heading level, for a heading block.')
    image_id: int | None = None
    alt: str | None = None
    href: str | None = Field(default=None, description='Target URL of a link block.')
    t: float | None = Field(default=None, description='Seconds into the recording this block starts at.')
    url: str | None = Field(default=None, description='Playable URL for a video or audio block.')
    duration: float | None = None
    cover_url: str | None = Field(default=None, description='Poster or thumbnail, as the source published it.')
    embed_url: str | None = None
    video_id: str | None = None
    provider: str | None = None


class ArticleImageOut(BaseModel):
    id: int
    position: int
    kind: str | None = None
    content_type: str | None = None
    hash_algo: str | None = None
    content_hash: str | None = None


class FetchDiagnosticOut(BaseModel):
    """Why a body is missing, when the downloader recorded a reason."""

    last_error: str | None = None
    error_type: str | None = None
    retryable: bool | None = None
    attempts: int | None = None
    last_attempt_at: str | None = None


class ArticleDetailOut(BaseModel):
    article: ArticleOut
    content: list[ContentBlockOut] | None = Field(
        default=None, description='Body blocks, or null when no body was stored.'
    )
    content_status: Literal['ok', 'missing', 'empty', 'invalid', 'processing']
    content_updated_at: str | None = None
    content_fetch_diagnostic: FetchDiagnosticOut | None = Field(
        default=None, description='Downloader diagnostic, set when the body is not ``ok``.'
    )
    images: list[ArticleImageOut]


class ArticleImageListOut(BaseModel):
    images: list[ArticleImageOut]


class BlockedImageOut(BaseModel):
    """``POST /api/image/{image_id}/block``: the hash was added to the block list."""

    image_id: int
    hash_algo: str
    content_hash: str
    blocked: bool


class TranscribeQueuedOut(BaseModel):
    article_pk: int
    state: Literal['pending'] = Field(description='The job was queued again.')
    force_asr: bool


class RefetchStartedOut(BaseModel):
    task_id: str
    status: Literal['started']


class RefetchStatusOut(BaseModel):
    """Progress of a refetch task; only its own starter may poll it."""

    task_id: str
    status: Literal['running', 'done', 'error']
    phase: str | None = None
    started_at: float | None = Field(default=None, description='Monotonic seconds, for measuring the run.')
    finished_at: float | None = None
    error: str | None = None
