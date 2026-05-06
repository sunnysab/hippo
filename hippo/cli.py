"""Typer-powered command line interface for the project."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import random
import time
import functools
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import typer
import click
from tqdm import tqdm

from .config import DEFAULT_PAGE_SIZE
from .container import build_downloader_container
from .downloader import _attach_image_block_metadata, _parse_markdown_blocks
from .env import load_env
from .image_hashes import ensure_image_hash_by_id
from .http import MPClient
from .file_storage import FileStorageError, S3FileStorage
from .image_store import ArticleImageService
from .wechat_api import SessionExpiredError, WeChatApiClient
from .login_service import save_login_session
from .logger import setup_logger
from .models import AccountCredential, AccountGroup, LoginSession
from .server import serve as run_server
from .rss import build_rss_xml, query_rss_items
from .storage import StorageInitError, PostgresStorage, open_storage
from .sync_worker import run_sync_worker
from .controllers.sync import (
    SyncMode,
    sync_account_articles as perform_account_sync,
    sync_all_accounts as perform_all_sync,
    sync_group_accounts as perform_group_sync,
)
from .utils import format_table, parse_iso_datetime_to_timestamp

# Initialize logger on module import
logger = setup_logger()

_ARTICLE_CONTENT_PRESENT_SQL = """
(
    (c.clean_html IS NOT NULL AND btrim(c.clean_html) <> '')
    OR (c.content_markdown IS NOT NULL AND btrim(c.content_markdown) <> '')
    OR (c.content_json IS NOT NULL AND c.content_json::text NOT IN ('[]', 'null'))
)
"""

_ARTICLE_ITEM_SHOW_TYPE_FROM_RAW_SQL = """
CASE
    WHEN jsonb_typeof(a.raw_json::jsonb -> 'appmsgex') = 'object'
     AND COALESCE(a.raw_json::jsonb -> 'appmsgex' ->> 'item_show_type', '') ~ '^-?[0-9]+$'
    THEN (a.raw_json::jsonb -> 'appmsgex' ->> 'item_show_type')::integer
    ELSE NULL
END
"""

app = typer.Typer(
    help="Hippo WeChat article exporter CLI",
    no_args_is_help=True,
    rich_markup_mode=None,
)


def coro(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper


@app.callback()
def main_callback(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示详细日志到控制台"),
) -> None:
    """Hippo WeChat article exporter CLI"""
    if verbose:
        # Reinitialize logger with verbose console output
        import logging
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_formatter = logging.Formatter("%(levelname)s: %(message)s")
        console_handler.setFormatter(console_formatter)
        logging.getLogger("hippo").addHandler(console_handler)


accounts_app = typer.Typer(
    help="Manage stored WeChat accounts",
    no_args_is_help=True,
    rich_markup_mode=None,
)
groups_app = typer.Typer(
    help="Manage account groups",
    no_args_is_help=True,
    rich_markup_mode=None,
)
articles_app = typer.Typer(
    help="Inspect and download articles",
    no_args_is_help=True,
    rich_markup_mode=None,
)
db_app = typer.Typer(
    help="Database maintenance",
    no_args_is_help=True,
    rich_markup_mode=None,
)


def _fix_click_option_flags(command: click.Command) -> None:
    for param in getattr(command, "params", []):
        if isinstance(param, click.Option):
            if param.is_flag and not isinstance(param.type, click.types.BoolParamType):
                param.is_flag = False
                param.flag_value = None
    for subcommand in getattr(command, "commands", {}).values():
        _fix_click_option_flags(subcommand)


def _patch_click_for_typer() -> None:
    try:
        from click.core import Parameter
    except Exception:
        return
    if getattr(Parameter.make_metavar, "__defaults__", None):
        return

    original = Parameter.make_metavar

    def _make_metavar(self, ctx=None):  # type: ignore[override]
        return original(self, ctx)

    Parameter.make_metavar = _make_metavar  # type: ignore[assignment]

    try:
        from typer.core import TyperOption
    except Exception:
        return

    if getattr(TyperOption.__init__, "_hippo_click_flag_patch", None):
        return

    original_option_init = TyperOption.__init__
    option_init_params = set(inspect.signature(original_option_init).parameters)

    def _option_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "flag_value" in kwargs and "flag_value" not in option_init_params:
            kwargs.pop("flag_value", None)
        if kwargs.get("is_flag") and kwargs.get("flag_value") is None and "flag_value" in option_init_params:
            kwargs["flag_value"] = click.core.UNSET
        return original_option_init(self, *args, **kwargs)

    _option_init._hippo_click_flag_patch = True  # type: ignore[attr-defined]
    TyperOption.__init__ = _option_init  # type: ignore[assignment]


def run() -> None:
    load_env()
    _patch_click_for_typer()
    command = typer.main.get_command(app)
    _fix_click_option_flags(command)
    try:
        command()
    except StorageInitError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from exc


def _parse_octal_mode(value: str) -> int:
    normalized = value.strip().lower()
    if normalized.startswith('0o'):
        normalized = normalized[2:]
    if not normalized:
        raise typer.BadParameter('Unix socket mode cannot be empty')
    try:
        mode = int(normalized, 8)
    except ValueError as exc:
        raise typer.BadParameter('Unix socket mode must be an octal value such as 660') from exc
    if mode < 0 or mode > 0o777:
        raise typer.BadParameter('Unix socket mode must be between 000 and 777')
    return mode


@db_app.command("init")
def init_db(
    pg_dsn: Optional[str] = typer.Option(
        None, help="PostgreSQL DSN (defaults to HIPPO_PG_DSN)"
    ),
    backfill_image_hashes: bool = typer.Option(
        False,
        '--backfill-image-hashes',
        help='Initialize schema and then backfill missing image hashes',
    ),
    image_hash_limit: Optional[int] = typer.Option(
        None,
        '--image-hash-limit',
        min=1,
        help='Optional limit used with --backfill-image-hashes',
    ),
) -> None:
    resolved_dsn = pg_dsn or os.environ.get("HIPPO_PG_DSN")
    if not resolved_dsn:
        typer.echo("Missing PostgreSQL DSN. Set HIPPO_PG_DSN or pass --pg-dsn.")
        raise typer.Exit(code=2)
    with PostgresStorage(resolved_dsn, auto_init=True):
        pass
    typer.echo("PostgreSQL schema initialized.")
    if backfill_image_hashes:
        _backfill_article_image_hashes(
            pg_dsn=resolved_dsn,
            limit=image_hash_limit,
            dry_run=False,
        )

@db_app.command("rebuild-counts")
def rebuild_counts(
    pg_dsn: Optional[str] = typer.Option(
        None, help='PostgreSQL DSN (defaults to HIPPO_PG_DSN)'
    ),
) -> None:
    resolved_dsn = pg_dsn or os.environ.get('HIPPO_PG_DSN')
    if not resolved_dsn:
        typer.echo('Missing PostgreSQL DSN. Set HIPPO_PG_DSN or pass --pg-dsn.')
        raise typer.Exit(code=2)
    with PostgresStorage(resolved_dsn, auto_init=False) as storage:
        with storage.transaction():
            with storage.conn.cursor() as cur:
                cur.execute('SELECT hippo_rebuild_article_counts()')
    typer.echo('Rebuilt cached article counts.')


@db_app.command('backfill-item-show-type')
def backfill_item_show_type(
    pg_dsn: Optional[str] = typer.Option(
        None, help='PostgreSQL DSN (defaults to HIPPO_PG_DSN)'
    ),
    limit: Optional[int] = typer.Option(
        None, min=1, help='Optional max article count to backfill per run'
    ),
    batch_size: int = typer.Option(
        1000, min=1, help='Article batch size used during backfill'
    ),
    dry_run: bool = typer.Option(
        False, help='Preview candidate count without writing changes'
    ),
) -> None:
    resolved_dsn = pg_dsn or os.environ.get('HIPPO_PG_DSN')
    if not resolved_dsn:
        typer.echo('Missing PostgreSQL DSN. Set HIPPO_PG_DSN or pass --pg-dsn.')
        raise typer.Exit(code=2)

    raw_candidate_ids_sql = f"""
    SELECT a.id
    FROM articles a
    WHERE a.item_show_type IS NULL
      AND {_ARTICLE_ITEM_SHOW_TYPE_FROM_RAW_SQL} IS NOT NULL
    ORDER BY a.id ASC
    """
    fallback_candidate_ids_sql = f"""
    SELECT a.id
    FROM articles a
    LEFT JOIN article_content c ON c.article_pk = a.id
    WHERE a.item_show_type IS NULL
      AND {_ARTICLE_ITEM_SHOW_TYPE_FROM_RAW_SQL} IS NULL
      AND {_ARTICLE_CONTENT_PRESENT_SQL}
    ORDER BY a.id ASC
    """
    raw_batch_update_sql = f"""
    WITH candidate AS (
        SELECT a.id, {_ARTICLE_ITEM_SHOW_TYPE_FROM_RAW_SQL} AS inferred_item_show_type
        FROM articles a
        WHERE a.item_show_type IS NULL
          AND a.id > %s
          AND {_ARTICLE_ITEM_SHOW_TYPE_FROM_RAW_SQL} IS NOT NULL
        ORDER BY a.id ASC
        LIMIT %s
    ),
    updated AS (
        UPDATE articles a
        SET item_show_type = candidate.inferred_item_show_type,
            updated_at = NOW()
        FROM candidate
        WHERE a.id = candidate.id
          AND a.item_show_type IS NULL
        RETURNING a.id
    )
    SELECT
        (SELECT COUNT(*) FROM updated) AS updated_count,
        COALESCE((SELECT MAX(id) FROM candidate), %s) AS last_seen_id
    """
    fallback_batch_update_sql = f"""
    WITH candidate AS (
        SELECT a.id, 0 AS inferred_item_show_type
        FROM articles a
        LEFT JOIN article_content c ON c.article_pk = a.id
        WHERE a.item_show_type IS NULL
          AND a.id > %s
          AND {_ARTICLE_ITEM_SHOW_TYPE_FROM_RAW_SQL} IS NULL
          AND {_ARTICLE_CONTENT_PRESENT_SQL}
        ORDER BY a.id ASC
        LIMIT %s
    ),
    updated AS (
        UPDATE articles a
        SET item_show_type = candidate.inferred_item_show_type,
            updated_at = NOW()
        FROM candidate
        WHERE a.id = candidate.id
          AND a.item_show_type IS NULL
        RETURNING a.id
    )
    SELECT
        (SELECT COUNT(*) FROM updated) AS updated_count,
        COALESCE((SELECT MAX(id) FROM candidate), %s) AS last_seen_id
    """

    with PostgresStorage(resolved_dsn, auto_init=False) as storage:
        if dry_run:
            with storage.conn.cursor() as cur:
                if limit is None:
                    cur.execute(f'SELECT COUNT(*) FROM ({raw_candidate_ids_sql}) AS candidate')
                    raw_total = int(cur.fetchone()[0] or 0)
                    cur.execute(f'SELECT COUNT(*) FROM ({fallback_candidate_ids_sql}) AS candidate')
                    fallback_total = int(cur.fetchone()[0] or 0)
                else:
                    cur.execute(
                        f'SELECT COUNT(*) FROM ({raw_candidate_ids_sql} LIMIT %s) AS candidate',
                        (limit,),
                    )
                    raw_total = int(cur.fetchone()[0] or 0)
                    remaining_after_raw = max(limit - raw_total, 0)
                    fallback_total = 0
                    if remaining_after_raw > 0:
                        cur.execute(
                            f'SELECT COUNT(*) FROM ({fallback_candidate_ids_sql} LIMIT %s) AS candidate',
                            (remaining_after_raw,),
                        )
                        fallback_total = int(cur.fetchone()[0] or 0)
            storage.rollback()
            total_candidates = raw_total + fallback_total
            if total_candidates == 0:
                typer.echo('No NULL item_show_type rows could be inferred.')
                return
            typer.echo(f'Found {total_candidates} candidate articles.')
            return

        updated = 0
        progress = tqdm(
            total=limit,
            desc='Backfill item_show_type',
            unit='article',
            dynamic_ncols=True,
            leave=True,
        )
        try:
            raw_last_id = 0
            raw_remaining = limit
            progress.set_description_str('Backfill item_show_type [raw]')
            while raw_remaining != 0:
                current_batch_size = batch_size if raw_remaining is None else min(batch_size, raw_remaining)
                with storage.transaction():
                    with storage.conn.cursor() as cur:
                        cur.execute(
                            raw_batch_update_sql,
                            (raw_last_id, current_batch_size, raw_last_id),
                        )
                        row = cur.fetchone()
                raw_updated = int(row[0] or 0)
                next_last_id = int(row[1] or raw_last_id)
                if next_last_id <= raw_last_id:
                    break
                raw_last_id = next_last_id
                updated += raw_updated
                progress.update(raw_updated)
                if raw_remaining is not None:
                    raw_remaining = max(raw_remaining - raw_updated, 0)

            fallback_last_id = 0
            fallback_remaining = raw_remaining
            progress.set_description_str('Backfill item_show_type [fallback]')
            while fallback_remaining != 0:
                current_batch_size = (
                    batch_size if fallback_remaining is None else min(batch_size, fallback_remaining)
                )
                with storage.transaction():
                    with storage.conn.cursor() as cur:
                        cur.execute(
                            fallback_batch_update_sql,
                            (fallback_last_id, current_batch_size, fallback_last_id),
                        )
                        row = cur.fetchone()
                fallback_updated = int(row[0] or 0)
                next_last_id = int(row[1] or fallback_last_id)
                if next_last_id <= fallback_last_id:
                    break
                fallback_last_id = next_last_id
                updated += fallback_updated
                progress.update(fallback_updated)
                if fallback_remaining is not None:
                    fallback_remaining = max(fallback_remaining - fallback_updated, 0)
        finally:
            progress.close()
    if updated == 0:
        typer.echo('No NULL item_show_type rows could be inferred.')
        return
    typer.echo(f'Backfilled item_show_type for {updated} articles.')


@db_app.command('backfill-content-json')
def backfill_content_json(
    pg_dsn: Optional[str] = typer.Option(
        None, help='PostgreSQL DSN (defaults to HIPPO_PG_DSN)'
    ),
    article_pk: Optional[int] = typer.Option(
        None, min=1, help='Only backfill a specific article_pk'
    ),
    limit: Optional[int] = typer.Option(
        None, min=1, help='Optional max row count to backfill per run'
    ),
    batch_size: int = typer.Option(
        1000, min=1, help='Row batch size used during backfill'
    ),
    dry_run: bool = typer.Option(
        False, help='Preview affected row count without writing changes'
    ),
) -> None:
    resolved_dsn = pg_dsn or os.environ.get('HIPPO_PG_DSN')
    if not resolved_dsn:
        typer.echo('Missing PostgreSQL DSN. Set HIPPO_PG_DSN or pass --pg-dsn.')
        raise typer.Exit(code=2)
    processed = 0
    updated = 0
    last_article_pk = 0
    with PostgresStorage(resolved_dsn, auto_init=False) as storage:
        while True:
            remaining = None if limit is None else max(limit - processed, 0)
            if remaining == 0:
                break
            current_batch_size = 1 if article_pk is not None else batch_size
            if remaining is not None:
                current_batch_size = min(current_batch_size, remaining)

            select_sql = """
            SELECT article_pk, content_markdown, content_json
            FROM article_content
            WHERE content_markdown IS NOT NULL
              AND btrim(content_markdown) <> ''
            """
            params: list[Any] = []
            if article_pk is not None:
                select_sql += ' AND article_pk = %s'
                params.append(article_pk)
            else:
                select_sql += ' AND article_pk > %s'
                params.append(last_article_pk)
            select_sql += ' ORDER BY article_pk ASC LIMIT %s'
            params.append(current_batch_size)

            with storage.conn.cursor() as cur:
                cur.execute(select_sql, params)
                rows = cur.fetchall()
            storage.rollback()

            if not rows:
                break

            article_pks = [int(row[0]) for row in rows]
            last_article_pk = article_pks[-1]

            with storage.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT article_pk, id, orig_url
                    FROM article_images
                    WHERE article_pk = ANY(%s)
                      AND orig_url IS NOT NULL
                    """,
                    (article_pks,),
                )
                image_rows = cur.fetchall()
            storage.rollback()

            image_id_maps: dict[int, dict[str, int]] = {}
            for current_article_pk, image_id, orig_url in image_rows:
                image_id_maps.setdefault(int(current_article_pk), {})[str(orig_url)] = int(image_id)

            updates: list[tuple[str, list[dict], int]] = []
            for current_article_pk, content_markdown, existing_content_json in rows:
                markdown = str(content_markdown or '').strip()
                if not markdown:
                    continue
                _title, _cover_local, blocks, normalized_markdown = _parse_markdown_blocks(markdown)
                rebuilt_blocks = _attach_image_block_metadata(
                    blocks,
                    resolve_url=lambda value: value.strip() if isinstance(value, str) else None,
                    image_id_by_url=image_id_maps.get(int(current_article_pk)),
                )
                current_content_json = existing_content_json
                if isinstance(current_content_json, str):
                    try:
                        current_content_json = json.loads(current_content_json)
                    except json.JSONDecodeError:
                        current_content_json = None
                if rebuilt_blocks != current_content_json or normalized_markdown != markdown:
                    updates.append((normalized_markdown, rebuilt_blocks, int(current_article_pk)))

            processed += len(rows)

            if dry_run:
                updated += len(updates)
                typer.echo(
                    f'Dry run progress: processed {processed} articles, would update {updated}.'
                )
            else:
                if updates:
                    with storage.transaction():
                        with storage.conn.cursor() as cur:
                            cur.executemany(
                                """
                                UPDATE article_content
                                SET content_markdown = %s,
                                    content_json = %s::jsonb,
                                    updated_at = NOW()
                                WHERE article_pk = %s
                                """,
                                [
                                    (
                                        normalized_markdown,
                                        json.dumps(rebuilt_blocks, ensure_ascii=False),
                                        current_article_pk,
                                    )
                                    for normalized_markdown, rebuilt_blocks, current_article_pk in updates
                                ],
                            )
                updated += len(updates)
                typer.echo(
                    f'Backfill progress: processed {processed} articles, updated {updated}.'
                )

            if article_pk is not None:
                break

    if processed == 0:
        typer.echo('No article_content rows matched.')
        return
    if updated == 0:
        typer.echo('No article_content rows needed backfill.')
        return
    if dry_run:
        typer.echo(f'Would backfill content_json for {updated} articles.')
        return
    typer.echo(f'Backfilled content_json for {updated} articles.')


app.add_typer(accounts_app, name="account")
accounts_app.add_typer(groups_app, name="group")
app.add_typer(articles_app, name="article")
app.add_typer(db_app, name="db")



def _parse_since(value: Optional[str]) -> Optional[int]:
    try:
        return parse_iso_datetime_to_timestamp(value)
    except ValueError as exc:
        raise typer.BadParameter("时间格式应为 YYYY-MM-DD") from exc


def _build_group_defaults(storage: PostgresStorage) -> dict[int, AccountGroup]:
    return {group.id: group for group in storage.groups.list_groups()}


def _resolve_recent_since(
    account: AccountCredential,
    group_defaults: dict[int, AccountGroup],
) -> Optional[int]:
    group = group_defaults.get(account.group_id) if account.group_id is not None else None
    group_mode = group.sync_mode if group else None
    group_recent_days = group.sync_recent_days if group else None
    mode = (account.sync_mode or group_mode or '').strip().lower()
    if mode != 'recent':
        return None
    recent_days = account.sync_recent_days
    if recent_days is None:
        recent_days = group_recent_days
    if recent_days is None:
        recent_days = 7
    recent_days = max(int(recent_days), 1)
    now = datetime.now(timezone.utc)
    return int(now.timestamp() - recent_days * 86400)


def _parse_selection_indices(selection: str, total: int) -> list[int]:
    if total <= 0:
        raise typer.BadParameter("没有可用的结果用于选择")
    raw = selection.replace(" ", "")
    if not raw:
        raise typer.BadParameter("请选择要保存的序号，例如 1,3-5")
    selected: set[int] = set()
    for part in raw.split(","):
        if not part:
            continue
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            if not start_str.isdigit() or not end_str.isdigit():
                raise typer.BadParameter(f"无效范围: {part}")
            start = int(start_str)
            end = int(end_str)
            if start <= 0 or end <= 0 or start > end:
                raise typer.BadParameter(f"无效范围: {part}")
            for value in range(start, end + 1):
                if value > total:
                    raise typer.BadParameter(f"序号超出范围: {value}")
                selected.add(value - 1)
        else:
            if not part.isdigit():
                raise typer.BadParameter(f"无效序号: {part}")
            value = int(part)
            if value <= 0:
                raise typer.BadParameter(f"无效序号: {part}")
            if value > total:
                raise typer.BadParameter(f"序号超出范围: {value}")
            selected.add(value - 1)
    if not selected:
        raise typer.BadParameter("未解析出有效序号")
    return sorted(selected)


def _require_nonempty(value: Optional[str], message: str) -> None:
    if value is None or not str(value).strip():
        typer.echo(message)
        raise typer.Exit(code=2)



def _build_image_store(storage: PostgresStorage, *, enabled: bool) -> ArticleImageService | None:
    if not enabled:
        return None
    try:
        return ArticleImageService(
            image_repo=storage.images,
            file_storage=S3FileStorage(),
            transaction=storage.transaction,
        )
    except FileStorageError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)


def _resolve_account(storage: PostgresStorage, name: Optional[str]) -> AccountCredential:
    if name is None:
        raise LookupError("请输入公众号名称或 fakeid")
    target = name.strip()
    if not target:
        raise LookupError("请输入公众号名称或 fakeid")
    accounts = storage.accounts.list_accounts()
    exact = [acc for acc in accounts if acc.biz == target]
    if exact:
        return exact[0]
    lower_target = target.lower()
    matches = [
        acc
        for acc in accounts
        if (acc.nickname or "").lower() == lower_target
        or (acc.alias or "").lower() == lower_target
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(acc.nickname or acc.biz for acc in matches)
        raise LookupError(f"匹配到多个账号：{names}")
    raise LookupError(f"未找到账号：{target}")


def _get_login_session(storage: PostgresStorage) -> LoginSession:
    try:
        return storage.sessions.get_login_session()
    except LookupError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Account commands
@accounts_app.command("add")
def add_account(
    biz: str = typer.Option(..., prompt="fakeid", help="公众号 fakeid（searchbiz 返回）"),
    nickname: str = typer.Option(..., prompt="昵称", help="公众号昵称"),
    alias: Optional[str] = typer.Option(None, prompt=False, help="可选别名"),
    round_head_img: Optional[str] = typer.Option(None, help="头像 URL，可选"),
) -> None:
    target_biz = biz.strip()
    credential = AccountCredential(
        biz=target_biz,
        nickname=nickname.strip(),
        alias=(alias.strip() if alias else None),
        round_head_img=(round_head_img.strip() if round_head_img else None),
    )
    with open_storage() as storage:
        with storage.transaction():
            stored = storage.accounts.upsert_account(credential)
    typer.echo(f"账号 {stored.nickname} ({stored.biz}) 已保存")


@accounts_app.command("search")
@coro
async def search_accounts(
    keyword: str = typer.Argument(..., help="搜索关键词"),
    page: int = typer.Option(1, min=1, help="分页页码，从 1 开始"),
    begin: Optional[int] = typer.Option(None, min=0, help="起始偏移，优先于分页"),
    interactive: bool = typer.Option(False, is_flag=True, help="交互式选择并添加账号"),
) -> None:
    await _search_accounts_async(
        keyword=keyword,
        page=page,
        begin=begin,
        interactive=interactive,
    )


async def _search_accounts_async(
    *,
    keyword: str,
    page: int,
    begin: Optional[int],
    interactive: bool,
) -> None:
    _require_nonempty(keyword, "请提供搜索关键词。")
    with open_storage() as storage:
        session = _get_login_session(storage)
        existing_biz = {account.biz for account in storage.accounts.list_accounts()}
    page_size = 10
    current_page = page
    while True:
        offset = begin if begin is not None else (current_page - 1) * page_size
        async with MPClient() as client:
            api_client = WeChatApiClient(client)
            try:
                payload = await api_client.search_biz(
                    session,
                    keyword=keyword,
                    begin=offset,
                    count=page_size,
                )
            except SessionExpiredError:
                typer.echo("Session expired. Please login again.")
                raise typer.Exit(code=2)
        records = payload.get("list") or []
        if not records:
            typer.echo("未找到匹配的公众号")
            return
        headers = ["序号", "昵称", "fakeid", "别名"]
        rows: list[list[str]] = []
        for idx, item in enumerate(records, start=1):
            fakeid = item.get("fakeid", "-")
            nickname = item.get("nickname", "-")
            if fakeid in existing_biz:
                nickname = f"{nickname}（已添加）"
            rows.append(
                [
                    str(idx),
                    nickname,
                    fakeid,
                    item.get("alias", "-"),
                ]
            )
        table_text = format_table(headers, rows)
        if table_text:
            typer.echo(table_text)

        if not interactive:
            return

        raw = typer.prompt(
            "选择要添加的序号(如 1,3-5，回车跳过，q 退出)",
            default="",
            show_default=False,
        ).strip()
        if raw.lower() == "q":
            return
        if raw:
            try:
                indices = _parse_selection_indices(raw, len(records))
            except typer.BadParameter as exc:
                typer.echo(str(exc))
                continue
            with open_storage() as storage:
                saved = []
                with storage.transaction():
                    for idx in indices:
                        item = records[idx]
                        fakeid_value = (item.get("fakeid") or "").strip()
                        credential = AccountCredential(
                            biz=fakeid_value,
                            nickname=(item.get("nickname") or "").strip() or "未知公众号",
                            alias=(item.get("alias") or "").strip() or None,
                            round_head_img=(item.get("round_head_img") or "").strip() or None,
                        )
                        stored = storage.accounts.upsert_account(credential)
                        saved.append(f"{stored.nickname} ({stored.biz})")
            typer.echo(f"已保存 {len(saved)} 个账号")
        if begin is not None:
            begin += page_size
        current_page += 1


@accounts_app.command("list")
def list_accounts(
    group: Optional[str] = typer.Option(None, help="Filter by group name"),
) -> None:
    with open_storage() as storage:
        accounts = storage.accounts.list_accounts(group=group)
    if not accounts:
        typer.echo("尚未保存任何账号，使用 `account add` 添加")
        return
    headers = ["昵称", "fakeid", "Group", "Disabled", "最近同步"]
    rows: list[list[str]] = []
    for account in accounts:
        last_synced = account.last_synced_at.isoformat() if account.last_synced_at else "-"
        rows.append(
            [
                account.nickname,
                account.biz,
                account.group_name or "-",
                "yes" if account.is_disabled else "",
                last_synced,
            ]
        )
    table_text = format_table(headers, rows)
    if table_text:
        typer.echo(table_text)


@groups_app.command("add")
def add_group(
    name: str = typer.Argument(..., help="Group name"),
) -> None:
    if not name.strip():
        typer.echo("Please provide a group name.")
        raise typer.Exit(code=2)
    with open_storage() as storage:
        with storage.transaction():
            group = storage.groups.upsert_group(name)
    typer.echo(f"Group {group.name} saved.")


@groups_app.command("list")
def list_groups() -> None:
    with open_storage() as storage:
        groups = storage.groups.list_groups()
    if not groups:
        typer.echo("No groups found.")
        return
    headers = ["Group", "Accounts"]
    rows = [[group.name, str(group.account_count)] for group in groups]
    table_text = format_table(headers, rows)
    if table_text:
        typer.echo(table_text)


@groups_app.command("sync")
@coro
async def sync_group(
    group: str = typer.Argument(..., help="Group name"),
    page_size: int = typer.Option(DEFAULT_PAGE_SIZE, min=1, max=20, help="每页抓取数量"),
    sleep_seconds: float = typer.Option(
        0.05, min=0, help="翻页间隔秒数（可为小数）"
    ),
    reset: bool = typer.Option(False, is_flag=True, help="清除断点后从头同步"),
    mode: SyncMode = typer.Option(
        SyncMode.full, "--mode", "-m", help="Sync mode: full, incremental, recent, range"
    ),
    recent_days: Optional[int] = typer.Option(
        None, "--recent-days", min=1, help="Sync the last N days (requires --mode recent)"
    ),
    since_date: Optional[str] = typer.Option(
        None, "--since", help="Start date (YYYY-MM-DD, for range mode)"
    ),
    until_date: Optional[str] = typer.Option(
        None, "--until", help="End date (YYYY-MM-DD, for range mode)"
    ),
    force: bool = typer.Option(False, is_flag=True, help="忽略跳过条件，强制同步"),
    skip_time: Optional[int] = typer.Option(
        None, min=1, help="多少分钟内同步过则跳过"
    ),
) -> None:
    _require_nonempty(group, "Please provide a group name.")
    await perform_group_sync(
        group=group,
        page_size=page_size,
        sleep_seconds=sleep_seconds,
        reset=reset,
        mode=mode,
        recent_days=recent_days,
        since_date=since_date,
        until_date=until_date,
        force=force,
        skip_time=skip_time,
        login_flow=_run_login_flow,
    )


@groups_app.command("set")
def set_account_group(
    account: str = typer.Argument(..., help="Account name, alias, or fakeid"),
    group: str = typer.Argument(..., help="Group name"),
) -> None:
    _require_nonempty(account, "Please provide an account name or fakeid.")
    _require_nonempty(group, "Please provide a group name.")
    with open_storage() as storage:
        try:
            target = _resolve_account(storage, account)
        except LookupError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1)
        with storage.transaction():
            storage.accounts.set_account_group(target.biz, group)
    typer.echo(f"Account {target.nickname} ({target.biz}) assigned to group {group}.")


@groups_app.command("clear")
def clear_account_group(
    account: str = typer.Argument(..., help="Account name, alias, or fakeid"),
) -> None:
    _require_nonempty(account, "Please provide an account name or fakeid.")
    with open_storage() as storage:
        try:
            target = _resolve_account(storage, account)
        except LookupError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1)
        with storage.transaction():
            storage.accounts.set_account_group(target.biz, None)
    typer.echo(f"Account {target.nickname} ({target.biz}) group cleared.")


@accounts_app.command("remove")
def remove_account(
    account: str = typer.Argument(..., help="Account name, alias, or fakeid")
) -> None:
    with open_storage() as storage:
        try:
            target = _resolve_account(storage, account)
        except LookupError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1)
        with storage.transaction():
            removed = storage.accounts.remove_account(target.biz)
    if removed:
        typer.echo(f"Account {target.nickname} ({target.biz}) removed.")
    else:
        typer.echo(f"Account {target.biz} not found.")


@accounts_app.command("disable")
def disable_account(
    account: str = typer.Argument(..., help="Account name, alias, or fakeid"),
) -> None:
    _require_nonempty(account, "Please provide an account name or fakeid.")
    with open_storage() as storage:
        try:
            target = _resolve_account(storage, account)
        except LookupError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1)
        with storage.transaction():
            storage.accounts.set_account_disabled(target.biz, True)
    typer.echo(f"Account {target.nickname} ({target.biz}) disabled.")


@accounts_app.command("enable")
def enable_account(
    account: str = typer.Argument(..., help="Account name, alias, or fakeid"),
) -> None:
    _require_nonempty(account, "Please provide an account name or fakeid.")
    with open_storage() as storage:
        try:
            target = _resolve_account(storage, account)
        except LookupError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1)
        with storage.transaction():
            storage.accounts.set_account_disabled(target.biz, False)
    typer.echo(f"Account {target.nickname} ({target.biz}) enabled.")


@accounts_app.command("sync-config")
def set_account_sync_config(
    account: str = typer.Argument(..., help="Account name, alias, or fakeid"),
    mode: Optional[SyncMode] = typer.Option(
        None, "--mode", help="Sync mode: full, incremental, recent, range"
    ),
    recent_days: Optional[int] = typer.Option(
        None, "--recent-days", min=1, help="Recent days for recent mode"
    ),
    clear_recent_days: bool = typer.Option(
        False, "--clear-recent-days", is_flag=True, help="Clear recent days override"
    ),
) -> None:
    _require_nonempty(account, "Please provide an account name or fakeid.")
    if clear_recent_days and recent_days is not None:
        raise typer.BadParameter("Cannot use --recent-days with --clear-recent-days.")
    with open_storage() as storage:
        try:
            target = _resolve_account(storage, account)
        except LookupError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1)
        updates: dict[str, Any] = {}
        if mode is not None:
            updates["sync_mode"] = mode.value
        if clear_recent_days:
            updates["sync_recent_days"] = None
        elif recent_days is not None:
            updates["sync_recent_days"] = recent_days
        if not updates:
            typer.echo("No sync settings provided.")
            return
        if (
            (mode == SyncMode.recent)
            and updates.get("sync_recent_days") is None
            and target.sync_recent_days is None
        ):
            raise typer.BadParameter("recent mode requires --recent-days.")
        updated = target.model_copy(update=updates)
        with storage.transaction():
            storage.accounts.upsert_account(updated)
    typer.echo(f"Account {target.nickname} ({target.biz}) sync settings updated.")


# ---------------------------------------------------------------------------
# Article helpers
@accounts_app.command("sync")
@coro
async def sync_account_articles(
    biz: Optional[str] = typer.Option(None, help="指定账号 fakeid，留空使用默认账号"),
    pages: int = typer.Option(1, min=1, help="抓取的分页数量，每页默认 10 篇"),
    page_size: int = typer.Option(DEFAULT_PAGE_SIZE, min=1, max=20, help="每页抓取数量"),
    mode: SyncMode = typer.Option(
        SyncMode.incremental,
        "--mode",
        "-m",
        help="Sync mode: full, incremental, recent, range",
    ),
    recent_days: Optional[int] = typer.Option(
        None, "--recent-days", min=1, help="Sync the last N days (requires --mode recent)"
    ),
    since_date: Optional[str] = typer.Option(
        None, "--since", help="Start date (YYYY-MM-DD, for range mode)"
    ),
    until_date: Optional[str] = typer.Option(
        None, "--until", help="End date (YYYY-MM-DD, for range mode)"
    ),
    force: bool = typer.Option(False, is_flag=True, help="忽略跳过条件，强制同步"),
    skip_time: Optional[int] = typer.Option(
        None, min=1, help="多少分钟内同步过则跳过"
    ),
) -> None:
    await perform_account_sync(
        biz=biz,
        pages=pages,
        page_size=page_size,
        mode=mode,
        recent_days=recent_days,
        since_date=since_date,
        until_date=until_date,
        force=force,
        skip_time=skip_time,
        login_flow=_run_login_flow,
    )


@accounts_app.command("sync-all")
@coro
async def sync_all_accounts(
    page_size: int = typer.Option(DEFAULT_PAGE_SIZE, min=1, max=20, help="每页抓取数量"),
    sleep_seconds: float = typer.Option(
        0.05, min=0, help="翻页间隔秒数（可为小数）"
    ),
    reset: bool = typer.Option(False, is_flag=True, help="清除断点后从头同步"),
    mode: SyncMode = typer.Option(
        SyncMode.full, "--mode", "-m", help="Sync mode: full, incremental, recent, range"
    ),
    recent_days: Optional[int] = typer.Option(
        None, "--recent-days", min=1, help="Sync the last N days (requires --mode recent)"
    ),
    since_date: Optional[str] = typer.Option(
        None, "--since", help="Start date (YYYY-MM-DD, for range mode)"
    ),
    until_date: Optional[str] = typer.Option(
        None, "--until", help="End date (YYYY-MM-DD, for range mode)"
    ),
    force: bool = typer.Option(False, is_flag=True, help="忽略跳过条件，强制同步"),
    skip_time: Optional[int] = typer.Option(
        None, min=1, help="多少分钟内同步过则跳过"
    ),
) -> None:
    await perform_all_sync(
        page_size=page_size,
        sleep_seconds=sleep_seconds,
        reset=reset,
        mode=mode,
        recent_days=recent_days,
        since_date=since_date,
        until_date=until_date,
        force=force,
        skip_time=skip_time,
        login_flow=_run_login_flow,
    )


@articles_app.command("list")
def list_articles(
    biz: Optional[str] = typer.Option(None, help="指定账号 fakeid，留空使用默认账号"),
    limit: int = typer.Option(5, min=1, max=50, help="显示的文章数量"),
    since: Optional[str] = typer.Option(None, help="仅显示某时间后的文章，格式 YYYY-MM-DD"),
) -> None:
    since_timestamp = _parse_since(since)
    with open_storage() as storage:
        account = storage.accounts.get_account(biz)
        articles = storage.articles.list_articles(account.biz, limit=limit, since_timestamp=since_timestamp)
    if not articles:
        typer.echo("未找到文章，请先执行 `account sync`")
        return
    headers = ["日期", "标题", "作者", "链接"]
    rows: list[list[str]] = []
    for article in articles:
        publish_date = (
            datetime.fromtimestamp(article.publish_at, tz=timezone.utc).strftime("%Y-%m-%d")
            if article.publish_at
            else "-"
        )
        rows.append([publish_date, article.title, article.author or "-", article.link])
    table_text = format_table(headers, rows)
    if table_text:
        typer.echo(table_text)


@articles_app.command("sync")
@coro
async def sync_article_download(
    account: str = typer.Argument(..., help="公众号名称或 fakeid"),
    limit: Optional[int] = typer.Option(None, min=1, max=5000, help="下载文章数量，默认全部"),
    with_images: bool = typer.Option(True, is_flag=True, help="是否下载图片"),
    article_only: bool = typer.Option(
        False, "--article-only", help="仅下载文章，不下载图片（仍创建图片记录）"
    ),
    since: Optional[str] = typer.Option(None, help="仅下载某日期后的文章"),
    worker_prefix: Optional[str] = typer.Option(None, help="文章 HTML worker 前缀或模板，留空使用环境变量"),
    worker_proxy: Optional[str] = typer.Option(None, help="访问 worker 时使用的代理（HTTP/SOCKS5），留空直连"),
    workers: Optional[int] = typer.Option(
        None,
        "--workers",
        "--worker-max-connections",
        min=1,
        help="文章下载并发数（原 --worker-max-connections）",
    ),
    image_workers: Optional[int] = typer.Option(
        None, min=1, help="图片下载并发数，留空使用默认"
    ),
) -> None:
    await _sync_article_download_async(
        account=account,
        limit=limit,
        with_images=with_images,
        article_only=article_only,
        since=since,
        worker_prefix=worker_prefix,
        worker_proxy=worker_proxy,
        workers=workers,
        image_workers=image_workers,
    )


async def _sync_article_download_async(
    *,
    account: str,
    limit: Optional[int],
    with_images: bool,
    article_only: bool,
    since: Optional[str],
    worker_prefix: Optional[str],
    worker_proxy: Optional[str],
    workers: Optional[int],
    image_workers: Optional[int],
) -> None:
    since_timestamp = _parse_since(since)
    with open_storage() as storage:
        try:
            account_record = _resolve_account(storage, account)
        except LookupError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1)
        if account_record.is_disabled:
            typer.echo(f"Account {account_record.nickname} ({account_record.biz}) is disabled. Skipping.")
            return
        if since_timestamp is None:
            group_defaults = _build_group_defaults(storage)
            since_timestamp = _resolve_recent_since(account_record, group_defaults)
        articles = storage.articles.list_articles(
            account_record.biz,
            limit=limit,
            since_timestamp=since_timestamp,
            exclude_downloaded=True,
        )
        if not articles:
            typer.echo("没有可下载的文章，先执行 `account sync`")
            return
        typer.echo("开始下载 {count} 篇文章 -> PostgreSQL".format(count=len(articles)))
        download_images = with_images and not article_only
        record_images_only = article_only
        progress = tqdm(
            total=len(articles),
            desc=f"下载 {account_record.nickname or account_record.biz}",
            unit="篇",
            dynamic_ncols=True,
            leave=True,
        )
        try:
            container = build_downloader_container(
                storage=storage,
                enable_images=download_images,
                article_worker=worker_prefix,
                article_worker_proxy=worker_proxy,
                article_max_connections=workers,
                image_workers=image_workers,
                enable_image_worker=not article_only,
            )
            async with container as app:
                downloader = app.downloader
                if not downloader:
                    raise RuntimeError("Downloader not initialized")
                results, skipped, failed = await downloader.download_many(
                    articles,
                    with_images=download_images,
                    record_images_only=record_images_only,
                    progress=progress,
                    skip_if_downloaded=True,
                )
        except Exception as exc:
            typer.echo(f"下载过程出错：{exc}")
            raise typer.Exit(code=1)
        finally:
            progress.close()

    if failed > 0:
        typer.echo(f"下载完成，成功 {len(results)} 篇，跳过 {skipped} 篇，失败 {failed} 篇")
    else:
        typer.echo(f"下载完成，已写入 {len(results)} 篇，跳过 {skipped} 篇")


@articles_app.command("sync-all")
@coro
async def sync_all_article_download(
    limit: Optional[int] = typer.Option(None, min=1, max=5000, help="每个账号下载文章数量，默认全部"),
    with_images: bool = typer.Option(True, is_flag=True, help="是否下载图片"),
    article_only: bool = typer.Option(
        False, "--article-only", help="仅下载文章，不下载图片（仍创建图片记录）"
    ),
    since: Optional[str] = typer.Option(None, help="仅下载某日期后的文章"),
    worker_prefix: Optional[str] = typer.Option(None, help="文章 HTML worker 前缀或模板，留空使用环境变量"),
    worker_proxy: Optional[str] = typer.Option(None, help="访问 worker 时使用的代理（HTTP/SOCKS5），留空直连"),
    workers: Optional[int] = typer.Option(
        None,
        "--workers",
        "--worker-max-connections",
        min=1,
        help="文章下载并发数（原 --worker-max-connections）",
    ),
    image_workers: Optional[int] = typer.Option(
        None, min=1, help="图片下载并发数，留空使用默认"
    ),
) -> None:
    await _sync_all_article_download_async(
        limit=limit,
        with_images=with_images,
        article_only=article_only,
        since=since,
        worker_prefix=worker_prefix,
        worker_proxy=worker_proxy,
        workers=workers,
        image_workers=image_workers,
    )


async def _sync_all_article_download_async(
    *,
    limit: Optional[int],
    with_images: bool,
    article_only: bool,
    since: Optional[str],
    worker_prefix: Optional[str],
    worker_proxy: Optional[str],
    workers: Optional[int],
    image_workers: Optional[int],
) -> None:
    since_timestamp = _parse_since(since)
    total_downloads = 0
    with open_storage() as storage:
        accounts = storage.accounts.list_accounts()
        if not accounts:
            typer.echo("尚未保存任何账号，使用 `account add` 添加")
            return
        group_defaults = _build_group_defaults(storage)
        download_images = with_images and not article_only
        record_images_only = article_only
        container = build_downloader_container(
            storage=storage,
            enable_images=download_images,
            article_worker=worker_prefix,
            article_worker_proxy=worker_proxy,
            article_max_connections=workers,
            image_workers=image_workers,
            enable_image_worker=not article_only,
        )
        async with container as app:
            downloader = app.downloader
            if not downloader:
                raise RuntimeError("Downloader not initialized")
            total_skipped = 0
            total_failed = 0
            for account in accounts:
                if account.is_disabled:
                    typer.echo(f"Account {account.nickname} ({account.biz}) is disabled. Skipping.")
                    continue
                account_since = since_timestamp
                if account_since is None:
                    account_since = _resolve_recent_since(account, group_defaults)
                articles = storage.articles.list_articles(
                    account.biz,
                    limit=limit,
                    since_timestamp=account_since,
                    exclude_downloaded=True,
                )
                if not articles:
                    continue
                progress = tqdm(
                    total=len(articles),
                    desc=f"下载 {account.nickname or account.biz}",
                    unit="篇",
                    dynamic_ncols=True,
                    leave=True,
                )
                try:
                    results, skipped, failed = await downloader.download_many(
                        articles,
                        with_images=download_images,
                        record_images_only=record_images_only,
                        progress=progress,
                        skip_if_downloaded=True,
                    )
                except Exception as exc:
                    typer.echo(f"下载过程出错：{exc}")
                    raise typer.Exit(code=1)
                finally:
                    progress.close()
                total_downloads += len(results)
                total_skipped += skipped
                total_failed += failed
            if download_images:
                await downloader.wait_for_images_with_progress(label="下载图片")

    if total_failed > 0:
        typer.echo(f"全部下载完成，成功 {total_downloads} 篇，跳过 {total_skipped} 篇，失败 {total_failed} 篇")
    else:
        typer.echo(f"全部下载完成，已写入 {total_downloads} 篇，跳过 {total_skipped} 篇")


@articles_app.command("download")
@coro
async def download_article(
    url: str = typer.Argument(..., help="文章 URL"),
    with_images: bool = typer.Option(True, is_flag=True, help="是否下载图片"),
    title: Optional[str] = typer.Option(None, help="覆盖文章标题"),
    worker_prefix: Optional[str] = typer.Option(None, help="文章 HTML worker 前缀或模板，留空使用环境变量"),
    worker_proxy: Optional[str] = typer.Option(None, help="访问 worker 时使用的代理（HTTP/SOCKS5），留空直连"),
    workers: Optional[int] = typer.Option(
        None,
        "--workers",
        "--worker-max-connections",
        min=1,
        help="文章下载并发数（原 --worker-max-connections）",
    ),
    image_workers: Optional[int] = typer.Option(
        None, min=1, help="图片下载并发数，留空使用默认"
    ),
) -> None:
    await _download_article_async(
        url=url,
        with_images=with_images,
        title=title,
        worker_prefix=worker_prefix,
        worker_proxy=worker_proxy,
        workers=workers,
        image_workers=image_workers,
    )


async def _download_article_async(
    *,
    url: str,
    with_images: bool,
    title: Optional[str],
    worker_prefix: Optional[str],
    worker_proxy: Optional[str],
    workers: Optional[int],
    image_workers: Optional[int],
) -> None:
    if not url:
        typer.echo("请提供文章 URL。示例：python -m hippo article download \"https://mp.weixin.qq.com/...\"")
        raise typer.Exit(code=2)
    with open_storage() as storage:
        try:
            container = build_downloader_container(
                storage=storage,
                enable_images=with_images,
                article_worker=worker_prefix,
                article_worker_proxy=worker_proxy,
                article_max_connections=workers,
                image_workers=image_workers,
            )
            async with container as app:
                downloader = app.downloader
                if not downloader:
                    raise RuntimeError("Downloader not initialized")
                await downloader.download_from_url(
                    url,
                    with_images=with_images,
                    title=title,
                )
        except Exception as exc:
            typer.echo(f"下载失败：{exc}")
            raise typer.Exit(code=1)
    typer.echo("Article saved to PostgreSQL.")


@articles_app.command("backfill-images")
@coro
async def backfill_article_images(
    pg_dsn: Optional[str] = typer.Option(
        None, help="PostgreSQL DSN (defaults to HIPPO_PG_DSN)"
    ),
    limit: Optional[int] = typer.Option(None, min=1, help="Max images to backfill per run"),
    workers: int = typer.Option(8, min=1, help="Concurrent image downloads"),
    retries: int = typer.Option(3, min=1, help="Download retries per image"),
    sleep_base: float = typer.Option(0.5, min=0.1, help="Base backoff sleep in seconds"),
    retry_failed: bool = typer.Option(False, is_flag=True, help="Include previously failed images"),
    dry_run: bool = typer.Option(False, is_flag=True, help="List targets without writing"),
) -> None:
    from .image_backfill import backfill_article_images

    try:
        result = await backfill_article_images(
            pg_dsn=pg_dsn,
            limit=limit,
            workers=workers,
            retries=retries,
            sleep_base=sleep_base,
            retry_failed=retry_failed,
            dry_run=dry_run,
            log=typer.echo,
        )
    except RuntimeError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2)
    typer.echo(f"Done. updated={result['updated']} skipped={result['skipped']} failed={result['failed']}")


@articles_app.command('backfill-image-hashes')
@coro
async def backfill_article_image_hashes(
    pg_dsn: Optional[str] = typer.Option(
        None, help='PostgreSQL DSN (defaults to HIPPO_PG_DSN)'
    ),
    limit: Optional[int] = typer.Option(
        None,
        min=1,
        help='Max stored images to hash per run',
    ),
    workers: int = typer.Option(8, min=1, help='Concurrent image hash workers'),
    batch_size: Optional[int] = typer.Option(
        None,
        min=1,
        help='Batch size per fetch cycle (defaults to workers * 4)',
    ),
    dry_run: bool = typer.Option(False, is_flag=True, help='List targets without writing'),
) -> None:
    await _backfill_article_image_hashes_async(
        pg_dsn=pg_dsn,
        limit=limit,
        workers=workers,
        batch_size=batch_size,
        dry_run=dry_run,
    )


def _backfill_article_image_hashes(
    *,
    pg_dsn: Optional[str],
    limit: Optional[int],
    workers: int = 8,
    batch_size: Optional[int] = None,
    dry_run: bool,
) -> None:
    asyncio.run(
        _backfill_article_image_hashes_async(
            pg_dsn=pg_dsn,
            limit=limit,
            workers=workers,
            batch_size=batch_size,
            dry_run=dry_run,
        )
    )


async def _backfill_article_image_hashes_async(
    *,
    pg_dsn: Optional[str],
    limit: Optional[int],
    workers: int,
    batch_size: Optional[int],
    dry_run: bool,
) -> None:
    resolved_dsn = pg_dsn or os.environ.get('HIPPO_PG_DSN')
    if not resolved_dsn:
        typer.echo('Missing PostgreSQL DSN. Set HIPPO_PG_DSN or pass --pg-dsn.')
        raise typer.Exit(code=2)

    updated = 0
    skipped = 0
    failed = 0
    interrupted = False
    worker_count = max(1, workers)
    fetch_size = batch_size if batch_size is not None else worker_count * 4
    fetch_size = max(fetch_size, worker_count)
    sem = asyncio.Semaphore(worker_count)
    count_query = """
        SELECT COUNT(*)
        FROM article_images i
        WHERE i.s3_key IS NOT NULL
          AND i.s3_key <> ''
          AND i.orig_url IS NOT NULL
          AND (i.hash_algo IS NULL OR i.hash_algo = '' OR i.content_hash IS NULL OR i.content_hash = '')
    """
    base_query = """
        SELECT i.id, i.orig_url
        FROM article_images i
        WHERE i.s3_key IS NOT NULL
          AND i.s3_key <> ''
          AND i.orig_url IS NOT NULL
          AND (i.hash_algo IS NULL OR i.hash_algo = '' OR i.content_hash IS NULL OR i.content_hash = '')
    """
    order_clause = 'ORDER BY i.id DESC'
    with PostgresStorage(resolved_dsn) as storage:
        with storage.conn.cursor() as cur:
            cur.execute(count_query)
            total_count = int(cur.fetchone()[0])
        if limit is not None:
            total_count = min(total_count, limit)
        progress = tqdm(
            total=total_count,
            desc='Backfill image hashes',
            unit='img',
            dynamic_ncols=True,
            leave=True,
        )
        try:
            try:
                last_id: Optional[int] = None
                remaining = total_count
                while remaining > 0:
                    with storage.conn.cursor() as cur:
                        current_limit = min(fetch_size, remaining)
                        if last_id is None:
                            query = f'{base_query} {order_clause} LIMIT %s'
                            params = (current_limit,)
                        else:
                            query = f'{base_query} AND i.id < %s {order_clause} LIMIT %s'
                            params = (last_id, current_limit)
                        cur.execute(query, params)
                        rows = cur.fetchall()
                    if not rows:
                        break
                    if dry_run:
                        for image_id, orig_url in rows:
                            typer.echo(f'DRY-RUN {image_id} {orig_url}')
                            skipped += 1
                            progress.update(1)
                    else:
                        async def run_hash_job(item: tuple[Any, Any]) -> tuple[int, str, Exception | None]:
                            image_id, orig_url = item
                            try:
                                async with sem:
                                    await asyncio.to_thread(
                                        ensure_image_hash_by_id,
                                        resolved_dsn,
                                        int(image_id),
                                        allow_origin_fetch=False,
                                    )
                                return int(image_id), str(orig_url), None
                            except Exception as exc:
                                return int(image_id), str(orig_url), exc

                        tasks = [
                            asyncio.create_task(run_hash_job((image_id, orig_url)))
                            for image_id, orig_url in rows
                        ]
                        try:
                            for task in asyncio.as_completed(tasks):
                                image_id, orig_url, error = await task
                                try:
                                    if error is not None:
                                        raise error
                                    updated += 1
                                except Exception as exc:
                                    failed += 1
                                    typer.echo(f'FAILED {image_id} {orig_url}: {exc}')
                                progress.update(1)
                        except KeyboardInterrupt:
                            interrupted = True
                            for task in tasks:
                                task.cancel()
                            typer.echo('Interrupted. Exiting.')
                            break
                    remaining -= len(rows)
                    last_id = int(rows[-1][0])
            except KeyboardInterrupt:
                interrupted = True
                typer.echo('Interrupted. Exiting.')
        finally:
            progress.close()

    typer.echo(f'Done. updated={updated} skipped={skipped} failed={failed}')
    if interrupted:
        raise typer.Exit(code=130)


# ---------------------------------------------------------------------------
@app.command("export-accounts")
def export_accounts() -> None:
    """Dump stored accounts as JSON (sensitive)."""
    with open_storage() as storage:
        accounts = storage.accounts.list_accounts()
    payload = [
        {
            "biz": account.biz,
            "nickname": account.nickname,
            "alias": account.alias,
            "round_head_img": account.round_head_img,
            "is_disabled": account.is_disabled,
            "last_synced_at": account.last_synced_at.isoformat()
            if account.last_synced_at
            else None,
        }
        for account in accounts
    ]
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("login")
@coro
async def login(
    timeout: int = typer.Option(300, min=30, help="扫码等待超时时间（秒）"),
    poll_interval: int = typer.Option(2, min=1, help="轮询间隔（秒）"),
) -> None:
    await _run_login_flow(timeout=timeout, poll_interval=poll_interval)


@app.command("serve")
def serve(
    host: Optional[str] = typer.Option('127.0.0.1', help='HTTP 监听地址'),
    port: Optional[int] = typer.Option(8000, min=1, max=65535, help='HTTP 监听端口'),
    no_tcp: bool = typer.Option(
        False,
        '--no-tcp',
        help='禁用 TCP 监听，仅使用 Unix socket',
    ),
    unix_socket: Optional[Path] = typer.Option(
        None,
        '--unix-socket',
        help='Unix socket 路径，例如 /run/hippo/hippo.sock',
    ),
    unix_socket_mode: str = typer.Option(
        '660',
        '--unix-socket-mode',
        help='Unix socket 文件权限，八进制，例如 660',
    ),
    static_dir: Path = typer.Option(Path('frontend/dist'), help='静态资源目录'),
    inprocess_sync: bool = typer.Option(
        False,
        '--inprocess-sync',
        help='在 Web 进程内启用自动同步（不推荐）',
    ),
) -> None:
    """Start HTTP server for API + UI."""
    if no_tcp and unix_socket is None:
        raise typer.BadParameter('--unix-socket is required when --no-tcp is set')
    resolved_host = None if no_tcp else host
    resolved_port = None if no_tcp else port
    try:
        run_server(
            host=resolved_host,
            port=resolved_port,
            static_dir=static_dir,
            unix_socket=unix_socket,
            unix_socket_mode=_parse_octal_mode(unix_socket_mode),
            enable_inprocess_sync=inprocess_sync,
        )
    except RuntimeError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc


@app.command("sync-worker")
@coro
async def sync_worker(
    poll_interval: float = typer.Option(5.0, min=0.2, help='队列轮询间隔（秒）'),
) -> None:
    """Run dedicated sync worker."""
    await run_sync_worker(poll_interval=poll_interval)


@app.command("rss")
def rss(
    group: Optional[list[str]] = typer.Option(
        None, "--group", help="分组名称，可多次传入"
    ),
    groups: Optional[str] = typer.Option(
        None, "--groups", help="多个分组名称，逗号分隔"
    ),
    limit: Optional[int] = typer.Option(50, min=1, help="最多生成的条目数"),
    days: Optional[int] = typer.Option(None, min=1, help="最近 N 天的文章"),
    since: Optional[str] = typer.Option(None, help="开始日期 (YYYY-MM-DD)"),
    until: Optional[str] = typer.Option(None, help="结束日期 (YYYY-MM-DD)"),
    title: Optional[str] = typer.Option(None, help="RSS 标题"),
    link: Optional[str] = typer.Option(None, help="RSS 站点链接"),
    description: Optional[str] = typer.Option(None, help="RSS 描述"),
) -> None:
    names: list[str] = []
    if group:
        names.extend(group)
    if groups:
        names.extend([item.strip() for item in groups.split(",") if item.strip()])
    try:
        items = query_rss_items(
            group_names=names,
            limit=limit,
            days=days,
            since=since,
            until=until,
            image_base_url=link or "http://localhost:8000/",
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from exc
    title_value = title or ("Hippo RSS" + (f" - {', '.join(names)}" if names else ""))
    link_value = link or "http://localhost:8000/"
    description_value = description or "Hippo RSS feed"
    xml = build_rss_xml(
        title=title_value,
        link=link_value,
        description=description_value,
        items=items,
    )
    typer.echo(xml)


def _render_qr_in_terminal(qr_bytes: bytes) -> bool:
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
        import qrcode
    except Exception:
        return False
    try:
        img = Image.open(BytesIO(qr_bytes))
    except Exception:
        return False
    decoded_objects = decode(img)
    if not decoded_objects:
        return False
    qr_data = decoded_objects[0].data.decode("utf-8", errors="ignore")
    qr = qrcode.QRCode()
    qr.add_data(qr_data)
    qr.make(fit=True)
    qr.print_ascii(tty=True)
    return True


def _emit_qr_data_url(qr_bytes: bytes) -> None:
    encoded = base64.b64encode(qr_bytes).decode("ascii")
    typer.echo("无法在终端渲染二维码，请将以下 data URL 复制到浏览器打开：")
    typer.echo(f"data:image/png;base64,{encoded}")


async def _run_login_flow(*, timeout: int, poll_interval: int) -> None:
    sid = f"{int(time.time() * 1000)}{random.randint(100, 999)}"
    typer.echo("正在获取二维码...")
    async with MPClient(timeout=15.0) as client:
        api_client = WeChatApiClient(client)
        with open_storage() as storage:
            try:
                uuid_cookie = await api_client.start_login_session(sid)
            except Exception as exc:
                typer.echo(f"获取登录会话失败：{exc}")
                raise typer.Exit(code=1)
            try:
                qrcode_bytes = await api_client.fetch_login_qrcode(uuid_cookie)
            except Exception as exc:
                typer.echo(f"获取二维码失败：{exc}")
                raise typer.Exit(code=1)
            if not _render_qr_in_terminal(qrcode_bytes):
                _emit_qr_data_url(qrcode_bytes)
            typer.echo("请使用微信扫码登录")
            started = time.time()
            while True:
                if time.time() - started > timeout:
                    raise typer.Exit(code=1)
                resp = await api_client.check_login_status(uuid_cookie)
                if resp.get("base_resp", {}).get("ret") != 0:
                    typer.echo("扫码状态获取失败，请重试")
                    raise typer.Exit(code=1)
                status = resp.get("status")
                if status == 0:
                    await asyncio.sleep(poll_interval)
                    continue
                if status == 1:
                    session = await api_client.finalize_login(uuid_cookie)
                    info = await api_client.fetch_login_info(session)
                    session.nickname = info.get("nickname") or None
                    session.avatar = info.get("avatar") or None
                    save_login_session(storage, session)
                    typer.echo(f"登录成功：{session.nickname or '未知账号'}")
                    return
                if status in (2, 3):
                    qrcode_bytes = await api_client.fetch_login_qrcode(uuid_cookie)
                    if not _render_qr_in_terminal(qrcode_bytes):
                        _emit_qr_data_url(qrcode_bytes)
                    typer.echo("二维码已刷新，请重新扫码")
                    await asyncio.sleep(poll_interval)
                    continue
                if status in (4, 6):
                    typer.echo("扫码成功，等待确认...")
                    await asyncio.sleep(poll_interval)
                    continue
                if status == 5:
                    typer.echo("该账号尚未绑定邮箱，无法登录")
                    raise typer.Exit(code=1)
                await asyncio.sleep(poll_interval)


__all__ = ["app"]
