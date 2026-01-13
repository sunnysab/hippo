"""Typer-powered command line interface for the project."""

from __future__ import annotations

import json
import os
import random
import time
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
import typer
import click
from tqdm import tqdm

from .config import DB_PATH, DEFAULT_PAGE_SIZE, DOWNLOAD_ROOT, HOME_DIR, load_profile, get_profile_value
from .downloader import ArticleDownloader
from .http import MPClient, parse_appmsg_publish
from .logger import setup_logger, get_logger
from .models import AccountCredential, LoginSession
from .storage import StorageLike, PostgresStorage, open_storage
from .utils import ensure_directory

# Initialize logger on module import
logger = setup_logger()

app = typer.Typer(
    help="WeChat article exporter CLI",
    no_args_is_help=True,
    rich_markup_mode=None,
)


@app.callback()
def main_callback(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示详细日志到控制台"),
) -> None:
    """WeChat article exporter CLI"""
    if verbose:
        # Reinitialize logger with verbose console output
        import logging
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_formatter = logging.Formatter("%(levelname)s: %(message)s")
        console_handler.setFormatter(console_formatter)
        logging.getLogger("wechatcli").addHandler(console_handler)


accounts_app = typer.Typer(
    help="Manage stored WeChat accounts",
    no_args_is_help=True,
    rich_markup_mode=None,
)
articles_app = typer.Typer(
    help="Inspect and download articles",
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


def run() -> None:
    _patch_click_for_typer()
    command = typer.main.get_command(app)
    _fix_click_option_flags(command)
    command()
app.add_typer(accounts_app, name="account")
app.add_typer(articles_app, name="articles")



class OutputFormat(str, Enum):
    html = "html"
    markdown = "markdown"
    text = "text"

    def __str__(self) -> str:  # pragma: no cover - click displays value
        return self.value


class SyncInterrupted(Exception):
    pass


def _parse_since(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError as exc:
        raise typer.BadParameter("时间格式应为 YYYY-MM-DD") from exc


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


def _extract_publish_total(payload: dict) -> Optional[int]:
    raw_page = payload.get("publish_page")
    if isinstance(raw_page, str) and raw_page:
        try:
            parsed = json.loads(raw_page)
        except json.JSONDecodeError:
            parsed = {}
        total = parsed.get("total_count")
        if isinstance(total, int):
            return total
        if isinstance(total, str) and total.isdigit():
            return int(total)
    total = payload.get("total_count")
    if isinstance(total, int):
        return total
    if isinstance(total, str) and total.isdigit():
        return int(total)
    return None


def _extract_publish_page(payload: dict) -> dict:
    raw_page = payload.get("publish_page")
    if isinstance(raw_page, str) and raw_page:
        try:
            parsed = json.loads(raw_page)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _today_str() -> str:
    return date.today().isoformat()


def _pbar_write(progress: Optional[tqdm], message: str) -> None:
    if progress is not None:
        progress.write(message)
    else:
        typer.echo(message)


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    sep = "  "
    lines = [sep.join(h.ljust(widths[idx]) for idx, h in enumerate(headers))]
    lines.append(sep.join("-" * widths[idx] for idx in range(len(headers))))
    for row in rows:
        lines.append(sep.join(row[idx].ljust(widths[idx]) for idx in range(len(headers))))
    return "\n".join(lines)


def _require_nonempty(value: Optional[str], message: str) -> None:
    if value is None or not str(value).strip():
        typer.echo(message)
        raise typer.Exit(code=2)


def _enforce_exclusive_flags(force: bool, skip_minutes: Optional[int]) -> None:
    if force and skip_minutes is not None:
        raise typer.BadParameter("--force 与 --skip-time 不能同时使用")


def _should_skip_by_time(last_synced_at: Optional[datetime], skip_minutes: Optional[int]) -> bool:
    if skip_minutes is None or not last_synced_at:
        return False
    threshold = datetime.now(timezone.utc) - timedelta(minutes=skip_minutes)
    if last_synced_at.tzinfo is None:
        last_synced_at = last_synced_at.replace(tzinfo=timezone.utc)
    else:
        last_synced_at = last_synced_at.astimezone(timezone.utc)
    return last_synced_at >= threshold


def _format_last_synced(last_synced_at: Optional[datetime]) -> str:
    return last_synced_at.isoformat(timespec="seconds") if last_synced_at else "-"


def _resolve_account(storage: StorageLike, name: Optional[str]) -> AccountCredential:
    if name is None:
        raise LookupError("请输入公众号名称或 fakeid")
    target = name.strip()
    if not target:
        raise LookupError("请输入公众号名称或 fakeid")
    accounts = storage.list_accounts()
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


def _is_login_error(message: str) -> bool:
    lowered = message.lower()
    hints = ("login", "token", "session", "invalid", "expire", "expired", "timeout")
    return any(hint in lowered for hint in hints)


def _is_freq_control(message: str) -> bool:
    lowered = message.lower()
    hints = ("freq", "frequency", "control", "too fast", "too frequent")
    return any(hint in lowered for hint in hints)


def _handle_login_expired() -> bool:
    typer.echo("登录状态可能已失效，需要重新扫码登录。")
    return typer.confirm("现在重新登录并继续同步？", default=True)


def _sync_account_pages(
    *,
    storage: StorageLike,
    client: MPClient,
    account: AccountCredential,
    page_size: int,
    pages: Optional[int],
    sleep_seconds: float,
    resume_key: Optional[str] = None,
    full_synced_hint: bool = False,
    progress: Optional[tqdm] = None,
) -> tuple[int, int, bool]:
    session = _get_login_session(storage)
    offset = 0
    if resume_key:
        saved_offset = storage.get_meta(resume_key)
        if saved_offset and saved_offset.isdigit():
            offset = int(saved_offset)
            message = f"检测到断点进度，继续 {account.nickname} offset={offset}"
            _pbar_write(progress, message)
    if progress is not None and offset > 0:
        progress.n = offset
        progress.refresh()
    total_saved = 0
    page_count = 0
    total_count: Optional[int] = None
    completed = False
    request_count = 0
    while True:
        try:
            attempt = 0
            freq_attempt = 0
            while True:
                try:
                    payload = client.fetch_appmsg_publish(
                        session, fakeid=account.biz, begin=offset, count=page_size
                    )
                    break
                except RuntimeError as exc:
                    if _is_login_error(str(exc)):
                        if not _handle_login_expired():
                            typer.echo("已暂停同步，断点进度已保留")
                            raise typer.Exit(code=1)
                        try:
                            login()
                        except typer.Exit:
                            typer.echo("登录未完成，断点进度已保留")
                            raise
                        session = _get_login_session(storage)
                        continue
                    if _is_freq_control(str(exc)):
                        freq_attempt += 1
                        if freq_attempt == 1:
                            wait_seconds = 15
                        else:
                            wait_seconds = min(15 + 5 * (freq_attempt - 1), 60)
                        message = f"触发频率控制，等待 {wait_seconds} 秒后重试"
                        _pbar_write(progress, message)
                        time.sleep(wait_seconds)
                        continue
                    raise
                except (httpx.ReadTimeout, httpx.TimeoutException, httpx.TransportError) as exc:
                    attempt += 1
                    if attempt >= 3:
                        raise RuntimeError(f"网络请求超时或失败：{exc}") from exc
                    time.sleep(min(2 ** attempt, 5))
            request_count += 1
            if request_count % 60 == 0:
                message = "达到 60 次请求，等待 15 秒"
                _pbar_write(progress, message)
                time.sleep(15)
            publish_page = _extract_publish_page(payload)
            publish_list = publish_page.get("publish_list") or []
            publish_list_len = len(publish_list)
            total_count = _extract_publish_total(payload) or total_count
            if progress is not None and total_count and total_count > 0:
                completed_offset = offset if offset <= total_count else total_count
                if progress.total != total_count:
                    progress.total = total_count
                if progress.n != completed_offset:
                    progress.n = completed_offset
                progress.refresh()
            if publish_list_len == 0:
                completed = True
                break
            records = parse_appmsg_publish(account.biz, payload)
            if full_synced_hint:
                existing_ids = storage.get_existing_article_ids(
                    account.biz, [record.article_id for record in records]
                )
                if records and len(existing_ids) == len(records):
                    completed = True
                    break
            saved = storage.save_articles(records)
            storage.update_last_synced(account.biz)
            total_saved += saved
            page_count += 1
            current_completed = offset + publish_list_len
            if total_count is not None and current_completed > total_count:
                current_completed = total_count
            offset += page_size
            if resume_key:
                storage.set_meta(resume_key, str(offset))
            if progress is not None:
                delta = current_completed - progress.n
                if delta:
                    progress.update(delta)
            if pages is not None and page_count >= pages:
                completed = False
                break
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        except KeyboardInterrupt as exc:
            message = f"检测到中断，已保存断点：{account.nickname}"
            _pbar_write(progress, message)
            raise SyncInterrupted() from exc
    if resume_key and completed:
        storage.delete_meta(resume_key)
    if progress is not None and total_count and completed:
        progress.n = total_count
        progress.refresh()
    return total_saved, page_count, completed


def _get_login_session(storage: StorageLike) -> LoginSession:
    try:
        return storage.get_login_session()
    except LookupError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)


@app.callback()
def main_callback() -> None:
    """Ensure required directories exist before running sub-commands."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    ensure_directory(DOWNLOAD_ROOT)


# ---------------------------------------------------------------------------
# Account commands
@accounts_app.command("add")
def add_account(
    biz: str = typer.Option(..., prompt="fakeid", help="公众号 fakeid（searchbiz 返回）"),
    nickname: str = typer.Option(..., prompt="昵称", help="公众号昵称"),
    alias: Optional[str] = typer.Option(None, prompt=False, help="可选别名"),
    round_head_img: Optional[str] = typer.Option(None, help="头像 URL，可选"),
    set_default: bool = typer.Option(False, "--set-default/--no-set-default", help="是否设置为默认账号"),
) -> None:
    credential = AccountCredential(
        biz=biz.strip(),
        nickname=nickname.strip(),
        alias=(alias.strip() if alias else None),
        round_head_img=(round_head_img.strip() if round_head_img else None),
        uin="",
        key="",
        pass_ticket="",
        is_default=set_default,
    )
    with open_storage(DB_PATH) as storage:
        stored = storage.upsert_account(credential)
        if set_default:
            storage.set_default_account(stored.biz)
    typer.echo(f"账号 {stored.nickname} ({stored.biz}) 已保存")


@accounts_app.command("search")
def search_accounts(
    keyword: str = typer.Argument(..., help="搜索关键词"),
    page: int = typer.Option(1, min=1, help="分页页码，从 1 开始"),
    begin: Optional[int] = typer.Option(None, min=0, help="起始偏移，优先于分页"),
    interactive: bool = typer.Option(False, "--interactive/--no-interactive", help="交互式选择并添加账号"),
) -> None:
    _require_nonempty(keyword, "请提供搜索关键词。")
    with open_storage(DB_PATH) as storage:
        session = _get_login_session(storage)
        existing_biz = {account.biz for account in storage.list_accounts()}
    page_size = 10
    current_page = page
    while True:
        offset = begin if begin is not None else (current_page - 1) * page_size
        with MPClient() as client:
            payload = client.search_biz(session, keyword=keyword, begin=offset, count=page_size)
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
        table_text = _format_table(headers, rows)
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
            with open_storage(DB_PATH) as storage:
                saved = []
                for idx in indices:
                    item = records[idx]
                    credential = AccountCredential(
                        biz=item.get("fakeid", "").strip(),
                        nickname=(item.get("nickname") or "").strip() or "未知公众号",
                        alias=(item.get("alias") or "").strip() or None,
                        round_head_img=(item.get("round_head_img") or "").strip() or None,
                        uin="",
                        key="",
                        pass_ticket="",
                        is_default=False,
                    )
                    stored = storage.upsert_account(credential)
                    saved.append(f"{stored.nickname} ({stored.biz})")
            typer.echo(f"已保存 {len(saved)} 个账号")
        if begin is not None:
            begin += page_size
        current_page += 1


@accounts_app.command("list")
def list_accounts() -> None:
    with open_storage(DB_PATH) as storage:
        accounts = storage.list_accounts()
    if not accounts:
        typer.echo("尚未保存任何账号，使用 `account add` 添加")
        return
    headers = ["默认", "昵称", "fakeid", "最近同步"]
    rows: list[list[str]] = []
    for account in accounts:
        last_synced = account.last_synced_at.isoformat() if account.last_synced_at else "-"
        rows.append(["✅" if account.is_default else "", account.nickname, account.biz, last_synced])
    table_text = _format_table(headers, rows)
    if table_text:
        typer.echo(table_text)


@accounts_app.command("remove")
def remove_account(
    biz: str = typer.Argument(..., help="要移除的账号 fakeid")
) -> None:
    _require_nonempty(biz, "请提供要移除的账号 fakeid。")
    with open_storage(DB_PATH) as storage:
        removed = storage.remove_account(biz)
    if removed:
        typer.echo(f"账号 {biz} 已删除")
    else:
        typer.echo(f"未找到账号 {biz}")


@accounts_app.command("set-default")
def set_default_account(
    biz: str = typer.Argument(..., help="设置为默认账号的 fakeid")
) -> None:
    _require_nonempty(biz, "请提供要设置的账号 fakeid。")
    with open_storage(DB_PATH) as storage:
        storage.set_default_account(biz)
    typer.echo(f"{biz} 已设为默认账号")


# ---------------------------------------------------------------------------
# Article helpers
@accounts_app.command("sync")
def sync_account_articles(
    biz: Optional[str] = typer.Option(None, help="指定账号 fakeid，留空使用默认账号"),
    pages: int = typer.Option(1, min=1, help="抓取的分页数量，每页默认 10 篇"),
    page_size: int = typer.Option(DEFAULT_PAGE_SIZE, min=1, max=20, help="每页抓取数量"),
    force: bool = typer.Option(False, "--force/--no-force", help="忽略跳过条件，强制同步"),
    skip_time: Optional[int] = typer.Option(
        None, min=1, help="多少分钟内同步过则跳过"
    ),
) -> None:
    _enforce_exclusive_flags(force, skip_time)
    with open_storage(DB_PATH) as storage:
        account = storage.get_account(biz)
        if not force and _should_skip_by_time(account.last_synced_at, skip_time):
            typer.echo(
                f"该账号近期已同步，跳过（上次同步 {_format_last_synced(account.last_synced_at)}）"
            )
            return
        typer.echo(f"开始同步 {account.nickname} 的文章")
        total_saved = 0
        with MPClient() as client:
            progress = tqdm(
                total=None,
                desc=f"同步 {account.nickname}",
                unit="msg",
                dynamic_ncols=True,
                leave=True,
            )
            try:
                total_saved, _, completed = _sync_account_pages(
                    storage=storage,
                    client=client,
                    account=account,
                    page_size=page_size,
                    pages=pages,
                    sleep_seconds=0,
                    full_synced_hint=storage.get_meta(f"sync_complete:{account.biz}") is not None,
                    progress=progress,
                )
                status = "成功" if completed else "未完成"
                if completed and total_saved == 0:
                    status = "已是最新"
                progress.set_postfix_str(status, refresh=True)
            except SyncInterrupted:
                progress.set_postfix_str("未完成", refresh=True)
                typer.echo("同步中断，断点已保存")
                raise typer.Exit(code=130)
            except RuntimeError as exc:
                progress.set_postfix_str("失败", refresh=True)
                typer.echo(f"同步失败：{exc}")
                raise typer.Exit(code=1)
            finally:
                progress.close()
        typer.echo(f"同步完成，共写入 {total_saved} 条记录")


@accounts_app.command("sync-all")
def sync_all_accounts(
    page_size: int = typer.Option(DEFAULT_PAGE_SIZE, min=1, max=20, help="每页抓取数量"),
    sleep_seconds: float = typer.Option(
        0.05, min=0, help="翻页间隔秒数（可为小数）"
    ),
    reset: bool = typer.Option(False, "--reset/--no-reset", help="清除断点后从头同步"),
    force: bool = typer.Option(False, "--force/--no-force", help="忽略跳过条件，强制同步"),
    skip_time: Optional[int] = typer.Option(
        None, min=1, help="多少分钟内同步过则跳过"
    ),
) -> None:
    _enforce_exclusive_flags(force, skip_time)
    with open_storage(DB_PATH) as storage:
        accounts = storage.list_accounts()
        if not accounts:
            typer.echo("尚未保存任何账号，使用 `account add` 添加")
            return
        header = "开始同步全部账号（从最新文章往更早翻页）"
        if reset:
            header = "开始同步全部账号（重置断点，从最新文章往更早翻页）"
        if sleep_seconds > 0:
            header += f" 每页间隔 {sleep_seconds} 秒"
        typer.echo(header)
        with MPClient() as client:
            total_saved = 0
            summary: list[tuple[str, int]] = []
            for account in accounts:
                resume_key = f"sync_progress:{account.biz}"
                complete_key = f"sync_complete:{account.biz}"
                if reset:
                    storage.delete_meta(resume_key)
                    storage.delete_meta(complete_key)
                elif skip_time is None and not force and storage.get_meta(complete_key) == _today_str():
                    progress = tqdm(
                        total=0,
                        desc=f"同步 {account.nickname} ({account.biz})",
                        unit="msg",
                        dynamic_ncols=True,
                        leave=True,
                    )
                    progress.set_postfix_str("跳过(今日已完成)", refresh=True)
                    progress.close()
                    continue
                if not force and _should_skip_by_time(account.last_synced_at, skip_time):
                    last_synced = _format_last_synced(account.last_synced_at)
                    progress = tqdm(
                        total=0,
                        desc=f"同步 {account.nickname} ({account.biz})",
                        unit="msg",
                        dynamic_ncols=True,
                        leave=True,
                    )
                    progress.set_postfix_str(f"跳过(近期已同步 {last_synced})", refresh=True)
                    progress.close()
                    continue

                progress = tqdm(
                    total=None,
                    desc=f"同步 {account.nickname} ({account.biz})",
                    unit="msg",
                    dynamic_ncols=True,
                    leave=True,
                )
                try:
                    saved, _, completed = _sync_account_pages(
                        storage=storage,
                        client=client,
                        account=account,
                        page_size=page_size,
                        pages=None,
                        sleep_seconds=sleep_seconds,
                        resume_key=resume_key,
                        full_synced_hint=storage.get_meta(complete_key) is not None,
                        progress=progress,
                    )
                    status = "成功" if completed else "未完成"
                    if completed and saved == 0:
                        status = "已是最新"
                    progress.set_postfix_str(status, refresh=True)
                    if completed:
                        storage.set_meta(complete_key, _today_str())
                except SyncInterrupted:
                    progress.set_postfix_str("未完成", refresh=True)
                    typer.echo("同步中断，断点已保存")
                    raise typer.Exit(code=130)
                except RuntimeError as exc:
                    progress.set_postfix_str("失败", refresh=True)
                    typer.echo(f"同步失败：{exc}")
                    raise typer.Exit(code=1)
                finally:
                    progress.close()
                total_saved += saved
                summary.append((account.nickname or account.biz, saved))
            if summary:
                headers = ["账号", "新增/更新"]
                rows = [[name, str(saved)] for name, saved in summary]
                table_text = _format_table(headers, rows)
                if table_text:
                    typer.echo(table_text)
        typer.echo(f"全部账号同步完成，共写入 {total_saved} 条记录")


@articles_app.command("list")
def list_articles(
    biz: Optional[str] = typer.Option(None, help="指定账号 fakeid，留空使用默认账号"),
    limit: int = typer.Option(5, min=1, max=50, help="显示的文章数量"),
    since: Optional[str] = typer.Option(None, help="仅显示某时间后的文章，格式 YYYY-MM-DD"),
) -> None:
    since_timestamp = _parse_since(since)
    with open_storage(DB_PATH) as storage:
        account = storage.get_account(biz)
        articles = storage.list_articles(account.biz, limit=limit, since_timestamp=since_timestamp)
    if not articles:
        typer.echo("未找到文章，请先执行 `account sync`")
        return
    headers = ["日期", "标题", "作者", "链接"]
    rows: list[list[str]] = []
    for article in articles:
        publish_date = (
            datetime.utcfromtimestamp(article.publish_at).strftime("%Y-%m-%d")
            if article.publish_at
            else "-"
        )
        rows.append([publish_date, article.title, article.author or "-", article.link])
    table_text = _format_table(headers, rows)
    if table_text:
        typer.echo(table_text)


@articles_app.command("sync")
def sync_article_download(
    account: str = typer.Argument(..., help="公众号名称或 fakeid"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="使用配置文件中的 profile"),
    limit: Optional[int] = typer.Option(None, min=1, max=5000, help="下载文章数量，默认全部"),
    output_format: OutputFormat = typer.Option(
        OutputFormat.html, "--format", "-f", help="导出格式", show_default=True
    ),
    with_images: bool = typer.Option(True, "--with-images/--no-images", help="是否下载图片"),
    article_only: bool = typer.Option(
        False, "--article-only", help="仅下载文章，不下载图片（仍创建图片记录）"
    ),
    since: Optional[str] = typer.Option(None, help="仅下载某日期后的文章"),
    output: Optional[Path] = typer.Option(None, help="自定义输出目录"),
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
    # Load profile if specified
    if profile:
        try:
            profile_config = load_profile(profile)
            # Apply profile values if CLI args not explicitly set
            limit = limit or get_profile_value(profile_config, "limit")
            if "format" in profile_config and output_format == OutputFormat.html:
                output_format = OutputFormat(get_profile_value(profile_config, "format", "html"))
            with_images = get_profile_value(profile_config, "with_images", with_images)
            article_only = get_profile_value(profile_config, "article_only", article_only)
            since = since or get_profile_value(profile_config, "since")
            output = output or (Path(p) if (p := get_profile_value(profile_config, "output")) else None)
            worker_prefix = worker_prefix or get_profile_value(profile_config, "worker_prefix")
            worker_proxy = worker_proxy or get_profile_value(profile_config, "worker_proxy")
            workers = workers or get_profile_value(profile_config, "workers")
            image_workers = image_workers or get_profile_value(profile_config, "image_workers")
        except (FileNotFoundError, ValueError) as exc:
            typer.echo(f"加载 profile 失败：{exc}")
            raise typer.Exit(code=1)
    
    since_timestamp = _parse_since(since)
    with open_storage(DB_PATH) as storage:
        try:
            account_record = _resolve_account(storage, account)
        except LookupError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1)
        articles = storage.list_articles(
            account_record.biz, limit=limit, since_timestamp=since_timestamp
        )
        if not articles:
            typer.echo("没有可下载的文章，先执行 `account sync`")
            return
        target_dir = ensure_directory(output or DOWNLOAD_ROOT)
        typer.echo(f"开始下载 {len(articles)} 篇文章 -> {target_dir}")
        download_images = with_images and not article_only
        record_images_only = article_only
        fmt_value = (
            output_format.value
            if isinstance(output_format, OutputFormat)
            else str(output_format)
        )
        progress = tqdm(
            total=len(articles),
            desc=f"下载 {account_record.nickname or account_record.biz}",
            unit="篇",
            dynamic_ncols=True,
            leave=True,
        )
        try:
            with ArticleDownloader(
                output_dir=target_dir,
                storage=storage,
                article_worker=worker_prefix,
                article_worker_proxy=worker_proxy,
                article_max_connections=workers,
                image_workers=image_workers,
                enable_image_worker=not article_only,
            ) as downloader:
                results, skipped, failed = downloader.download_many(
                    articles,
                    fmt=fmt_value,
                    with_images=download_images,
                    record_images_only=record_images_only,
                    account_name=account_record.nickname or account_record.biz,
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
        typer.echo(f"下载完成，生成 {len(results)} 个目录，跳过 {skipped} 篇")


@articles_app.command("sync-all")
def sync_all_article_download(
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="使用配置文件中的 profile"),
    limit: Optional[int] = typer.Option(None, min=1, max=5000, help="每个账号下载文章数量，默认全部"),
    output_format: OutputFormat = typer.Option(
        OutputFormat.html, "--format", "-f", help="导出格式", show_default=True
    ),
    with_images: bool = typer.Option(True, "--with-images/--no-images", help="是否下载图片"),
    article_only: bool = typer.Option(
        False, "--article-only", help="仅下载文章，不下载图片（仍创建图片记录）"
    ),
    since: Optional[str] = typer.Option(None, help="仅下载某日期后的文章"),
    output: Optional[Path] = typer.Option(None, help="自定义输出目录"),
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
    # Load profile if specified
    if profile:
        try:
            profile_config = load_profile(profile)
            # Apply profile values if CLI args not explicitly set
            limit = limit or get_profile_value(profile_config, "limit")
            if "format" in profile_config and output_format == OutputFormat.html:
                output_format = OutputFormat(get_profile_value(profile_config, "format", "html"))
            with_images = get_profile_value(profile_config, "with_images", with_images)
            article_only = get_profile_value(profile_config, "article_only", article_only)
            since = since or get_profile_value(profile_config, "since")
            output = output or (Path(p) if (p := get_profile_value(profile_config, "output")) else None)
            worker_prefix = worker_prefix or get_profile_value(profile_config, "worker_prefix")
            worker_proxy = worker_proxy or get_profile_value(profile_config, "worker_proxy")
            workers = workers or get_profile_value(profile_config, "workers")
            image_workers = image_workers or get_profile_value(profile_config, "image_workers")
        except (FileNotFoundError, ValueError) as exc:
            typer.echo(f"加载 profile 失败：{exc}")
            raise typer.Exit(code=1)
    
    since_timestamp = _parse_since(since)
    total_downloads = 0
    with open_storage(DB_PATH) as storage:
        accounts = storage.list_accounts()
        if not accounts:
            typer.echo("尚未保存任何账号，使用 `account add` 添加")
            return
        target_dir = ensure_directory(output or DOWNLOAD_ROOT)
        download_images = with_images and not article_only
        record_images_only = article_only
        fmt_value = (
            output_format.value
            if isinstance(output_format, OutputFormat)
            else str(output_format)
        )
        with ArticleDownloader(
            output_dir=target_dir,
            storage=storage,
            article_worker=worker_prefix,
            article_worker_proxy=worker_proxy,
            article_max_connections=workers,
            image_workers=image_workers,
            enable_image_worker=not article_only,
        ) as downloader:
            total_skipped = 0
            total_failed = 0
            for account in accounts:
                articles = storage.list_articles(
                    account.biz, limit=limit, since_timestamp=since_timestamp
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
                    results, skipped, failed = downloader.download_many(
                        articles,
                        fmt=fmt_value,
                        with_images=download_images,
                        record_images_only=record_images_only,
                        account_name=account.nickname or account.biz,
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
            downloader.wait_for_images_with_progress(label="下载图片")
    
    if total_failed > 0:
        typer.echo(f"全部下载完成，成功 {total_downloads} 篇，跳过 {total_skipped} 篇，失败 {total_failed} 篇")
    else:
        typer.echo(f"全部下载完成，生成 {total_downloads} 个目录，跳过 {total_skipped} 篇")


@articles_app.command("download")
def download_article(
    url: str = typer.Argument(..., help="文章 URL"),
    output_format: OutputFormat = typer.Option(OutputFormat.html, "--format", "-f", help="导出格式"),
    with_images: bool = typer.Option(True, "--with-images/--no-images", help="是否下载图片"),
    output: Optional[Path] = typer.Option(None, help="自定义输出目录"),
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
    if not url:
        typer.echo("请提供文章 URL。示例：python -m wechatcli articles download \"https://mp.weixin.qq.com/...\"")
        raise typer.Exit(code=2)
    target_dir = ensure_directory(output or DOWNLOAD_ROOT)
    fmt_value = (
        output_format.value
        if isinstance(output_format, OutputFormat)
        else str(output_format)
    )
    with open_storage(DB_PATH) as storage, ArticleDownloader(
        output_dir=target_dir,
        storage=storage,
        article_worker=worker_prefix,
        article_worker_proxy=worker_proxy,
        article_max_connections=workers,
        image_workers=image_workers,
    ) as downloader:
        try:
            result = downloader.download_from_url(
                url,
                fmt=fmt_value,
                with_images=with_images,
                title=title,
            )
        except Exception as exc:
            typer.echo(f"下载失败：{exc}")
            raise typer.Exit(code=1)
    typer.echo(f"单篇文章已保存至 {result.output_path}")


@articles_app.command("backfill-images")
def backfill_article_images(
    pg_dsn: Optional[str] = typer.Option(
        None, help="PostgreSQL DSN (defaults to WECHATCLI_PG_DSN)"
    ),
    limit: Optional[int] = typer.Option(None, min=1, help="Max images to backfill per run"),
    workers: int = typer.Option(8, min=1, help="Concurrent image downloads"),
    retries: int = typer.Option(3, min=1, help="Download retries per image"),
    sleep_base: float = typer.Option(0.5, min=0.1, help="Base backoff sleep in seconds"),
    dry_run: bool = typer.Option(False, "--dry-run/--no-dry-run", help="List targets without writing"),
) -> None:
    resolved_dsn = pg_dsn or os.environ.get("WECHATCLI_PG_DSN")
    if not resolved_dsn:
        typer.echo("Missing PostgreSQL DSN. Set WECHATCLI_PG_DSN or pass --pg-dsn.")
        raise typer.Exit(code=2)

    def normalize_image_url(url: str) -> str:
        trimmed = url.strip().strip("\"'")
        if " " in trimmed:
            trimmed = trimmed.split(" ", 1)[0]
        if trimmed.endswith("\""):
            trimmed = trimmed.rstrip("\"")
        return trimmed

    def download_with_retry(url: str, *, referer: Optional[str]) -> tuple[bytes, Optional[str]]:
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return client.download_binary_with_type(
                    normalize_image_url(url), referer=referer
                )
            except Exception as exc:
                last_exc = exc
                time.sleep(min(sleep_base * (2**attempt), 5.0))
        raise RuntimeError(str(last_exc)) from last_exc

    def format_error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            url = exc.request.url
            return f"{exc} status={status} url={url}"
        if isinstance(exc, httpx.RequestError):
            return f"{exc} url={exc.request.url}"
        return str(exc)

    updated = 0
    skipped = 0
    failed = 0

    with PostgresStorage(resolved_dsn) as storage, MPClient() as client:
        query = """
            SELECT a.biz, a.article_id, a.link, i.orig_url
            FROM article_images i
            JOIN articles a ON a.id = i.article_pk
            WHERE i.data IS NULL AND i.orig_url IS NOT NULL
            ORDER BY a.id DESC, i.position ASC
        """
        params: list = []
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        with storage.conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        try:
            if dry_run:
                progress = tqdm(
                    total=len(rows),
                    desc="Backfill images",
                    unit="img",
                    dynamic_ncols=True,
                    leave=True,
                )
                try:
                    for _, _, _, orig_url in rows:
                        typer.echo(f"DRY-RUN {orig_url}")
                        skipped += 1
                        progress.update(1)
                finally:
                    progress.close()
            else:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                worker_count = max(1, workers)

                def worker(item: tuple) -> tuple[tuple, bytes, Optional[str]]:
                    biz, article_id, referer, orig_url = item
                    data, content_type = download_with_retry(
                        str(orig_url), referer=str(referer) if referer else None
                    )
                    return item, data, content_type

                progress = tqdm(
                    total=len(rows),
                    desc="Backfill images",
                    unit="img",
                    dynamic_ncols=True,
                    leave=True,
                )
                try:
                    with ThreadPoolExecutor(max_workers=worker_count) as executor:
                        future_map = {executor.submit(worker, item): item for item in rows}
                        for future in as_completed(future_map):
                            item = future_map[future]
                            biz, article_id, _, orig_url = item
                            try:
                                _, data, content_type = future.result()
                                storage.update_article_image_data(
                                    biz,
                                    article_id,
                                    str(orig_url),
                                    content_type,
                                    data,
                                )
                                updated += 1
                            except Exception as exc:
                                failed += 1
                                typer.echo(f"FAILED {orig_url}: {format_error(exc)}")
                            finally:
                                progress.update(1)
                finally:
                    progress.close()
        except KeyboardInterrupt:
            typer.echo("Interrupted. Exiting.")
            raise typer.Exit(code=130)

    typer.echo(f"Done. updated={updated} skipped={skipped} failed={failed}")


# ---------------------------------------------------------------------------
@app.command("export-accounts")
def export_accounts() -> None:
    """Dump stored accounts as JSON (sensitive)."""
    with open_storage(DB_PATH) as storage:
        accounts = storage.list_accounts()
    payload = [
        {
            "biz": account.biz,
            "nickname": account.nickname,
            "alias": account.alias,
            "round_head_img": account.round_head_img,
            "uin": account.uin,
            "key": account.key,
            "pass_ticket": account.pass_ticket,
            "is_default": account.is_default,
            "last_synced_at": account.last_synced_at.isoformat()
            if account.last_synced_at
            else None,
        }
        for account in accounts
    ]
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("login")
def login(
    timeout: int = typer.Option(300, min=30, help="扫码等待超时时间（秒）"),
    poll_interval: int = typer.Option(2, min=1, help="轮询间隔（秒）"),
    output: Optional[Path] = typer.Option(None, help="二维码输出目录"),
) -> None:
    target_dir = ensure_directory(output or (HOME_DIR / "login"))
    sid = f"{int(time.time() * 1000)}{random.randint(100, 999)}"
    with MPClient() as client, open_storage(DB_PATH) as storage:
        uuid_cookie = client.start_login_session(sid)
        qrcode_path = target_dir / "qrcode.png"
        qrcode_path.write_bytes(client.fetch_login_qrcode(uuid_cookie))
        typer.echo(f"请使用微信扫码登录，二维码已保存：{qrcode_path}")
        started = time.time()
        while True:
            if time.time() - started > timeout:
                raise typer.Exit(code=1)
            resp = client.check_login_status(uuid_cookie)
            if resp.get("base_resp", {}).get("ret") != 0:
                typer.echo("扫码状态获取失败，请重试")
                raise typer.Exit(code=1)
            status = resp.get("status")
            if status == 0:
                time.sleep(poll_interval)
                continue
            if status == 1:
                session = client.finalize_login(uuid_cookie)
                info = client.fetch_login_info(session)
                session.nickname = info.get("nickname") or None
                session.avatar = info.get("avatar") or None
                storage.save_login_session(session)
                typer.echo(f"登录成功：{session.nickname or '未知账号'}")
                return
            if status in (2, 3):
                qrcode_path.write_bytes(client.fetch_login_qrcode(uuid_cookie))
                typer.echo("二维码已刷新，请重新扫码")
                time.sleep(poll_interval)
                continue
            if status in (4, 6):
                typer.echo("扫码成功，等待确认...")
                time.sleep(poll_interval)
                continue
            if status == 5:
                typer.echo("该账号尚未绑定邮箱，无法登录")
                raise typer.Exit(code=1)
            time.sleep(poll_interval)


__all__ = ["app"]
