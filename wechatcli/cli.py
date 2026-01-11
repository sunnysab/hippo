"""Typer-powered command line interface for the project."""

from __future__ import annotations

import json
import random
import time
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .config import DB_PATH, DEFAULT_PAGE_SIZE, DOWNLOAD_ROOT, HOME_DIR
from .downloader import ArticleDownloader
from .http import MPClient, parse_appmsg_publish
from .models import AccountCredential, LoginSession
from .storage import StorageLike, open_storage
from .utils import ensure_directory

app = typer.Typer(help="WeChat article exporter CLI", no_args_is_help=True, rich_markup_mode=None)
accounts_app = typer.Typer(
    help="Manage stored WeChat accounts",
    rich_markup_mode=None,
    no_args_is_help=True,
)
articles_app = typer.Typer(
    help="Sync, inspect, and download articles",
    rich_markup_mode=None,
    no_args_is_help=True,
)
app.add_typer(accounts_app, name="accounts")
app.add_typer(articles_app, name="articles")

console = Console()


class OutputFormat(str, Enum):
    html = "html"
    markdown = "markdown"
    text = "text"

    def __str__(self) -> str:  # pragma: no cover - click displays value
        return self.value


class CountColumn(ProgressColumn):
    def render(self, task) -> str:  # type: ignore[override]
        total = "?" if task.total is None else str(int(task.total))
        return f"{int(task.completed)}/{total}"


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


def _enforce_exclusive_flags(force: bool, skip_minutes: Optional[int]) -> None:
    if force and skip_minutes is not None:
        raise typer.BadParameter("--force 与 --skip-time 不能同时使用")


def _should_skip_by_time(last_synced_at: Optional[datetime], skip_minutes: Optional[int]) -> bool:
    if skip_minutes is None or not last_synced_at:
        return False
    threshold = datetime.utcnow() - timedelta(minutes=skip_minutes)
    return last_synced_at >= threshold


def _format_last_synced(last_synced_at: Optional[datetime]) -> str:
    return last_synced_at.isoformat(timespec="seconds") if last_synced_at else "-"


def _is_login_error(message: str) -> bool:
    lowered = message.lower()
    hints = ("login", "token", "session", "invalid", "expire", "expired", "timeout")
    return any(hint in lowered for hint in hints)


def _is_freq_control(message: str) -> bool:
    lowered = message.lower()
    hints = ("freq", "frequency", "control", "too fast", "too frequent")
    return any(hint in lowered for hint in hints)


def _handle_login_expired() -> bool:
    console.print("[red]登录状态可能已失效，需要重新扫码登录。[/red]")
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
    progress: Optional[Progress] = None,
    task_id: Optional[int] = None,
) -> tuple[int, int, bool]:
    session = _get_login_session(storage)
    offset = 0
    if resume_key:
        saved_offset = storage.get_meta(resume_key)
        if saved_offset and saved_offset.isdigit():
            offset = int(saved_offset)
            message = f"检测到断点进度，继续 {account.nickname} offset={offset}"
            if progress is not None:
                progress.console.log(f"[yellow]{message}[/yellow]")
            else:
                console.print(f"[yellow]{message}[/yellow]")
    if progress is not None and task_id is not None and offset > 0:
        progress.update(task_id, completed=offset)
    total_saved = 0
    page_count = 0
    total_count: Optional[int] = None
    completed = False
    request_count = 0
    while True:
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
                        console.print("[yellow]已暂停同步，断点进度已保留[/yellow]")
                        raise typer.Exit(code=1)
                    try:
                        login()
                    except typer.Exit:
                        console.print("[yellow]登录未完成，断点进度已保留[/yellow]")
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
                    if progress is not None:
                        progress.console.log(f"[yellow]{message}[/yellow]")
                    else:
                        console.print(f"[yellow]{message}[/yellow]")
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
            if progress is not None:
                progress.console.log(f"[yellow]{message}[/yellow]")
            else:
                console.print(f"[yellow]{message}[/yellow]")
            time.sleep(15)
        publish_page = _extract_publish_page(payload)
        publish_list = publish_page.get("publish_list") or []
        publish_list_len = len(publish_list)
        total_count = _extract_publish_total(payload) or total_count
        if progress is not None and task_id is not None and total_count and total_count > 0:
            completed_offset = offset if offset <= total_count else total_count
            progress.update(task_id, total=total_count, completed=completed_offset)
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
        if progress is not None and task_id is not None:
            progress.update(task_id, completed=current_completed)
        if pages is not None and page_count >= pages:
            completed = False
            break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    if resume_key and completed:
        storage.delete_meta(resume_key)
    if progress is not None and task_id is not None and total_count and completed:
        progress.update(task_id, completed=total_count)
    return total_saved, page_count, completed


def _get_login_session(storage: StorageLike) -> LoginSession:
    try:
        return storage.get_login_session()
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
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
    set_default: bool = typer.Option(False, help="是否设置为默认账号", flag_value=True),
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
    console.print(f"[green]账号 {stored.nickname} ({stored.biz}) 已保存[/green]")


@accounts_app.command("search")
def search_accounts(
    keyword: str = typer.Argument(..., help="搜索关键词"),
    page: int = typer.Option(1, min=1, help="分页页码，从 1 开始"),
    begin: Optional[int] = typer.Option(None, min=0, help="起始偏移，优先于分页"),
    interactive: bool = typer.Option(False, help="交互式选择并添加账号", flag_value=True),
) -> None:
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
            console.print("[yellow]未找到匹配的公众号[/yellow]")
            return
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("序号", justify="right")
        table.add_column("昵称")
        table.add_column("fakeid")
        table.add_column("别名")
        for idx, item in enumerate(records, start=1):
            fakeid = item.get("fakeid", "-")
            nickname = item.get("nickname", "-")
            if fakeid in existing_biz:
                nickname = f"{nickname}（已添加）"
            table.add_row(
                str(idx),
                nickname,
                fakeid,
                item.get("alias", "-"),
            )
        console.print(table)

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
                console.print(f"[red]{exc}[/red]")
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
            console.print(f"[green]已保存 {len(saved)} 个账号[/green]")
        if begin is not None:
            begin += page_size
        current_page += 1


@accounts_app.command("list")
def list_accounts() -> None:
    with open_storage(DB_PATH) as storage:
        accounts = storage.list_accounts()
    if not accounts:
        console.print("[yellow]尚未保存任何账号，使用 `accounts add` 添加[/yellow]")
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("默认", justify="center")
    table.add_column("昵称")
    table.add_column("fakeid")
    table.add_column("最近同步")
    for account in accounts:
        last_synced = account.last_synced_at.isoformat() if account.last_synced_at else "-"
        table.add_row("✅" if account.is_default else "", account.nickname, account.biz, last_synced)
    console.print(table)


@accounts_app.command("remove")
def remove_account(
    biz: str = typer.Argument(..., help="要移除的账号 fakeid")
) -> None:
    with open_storage(DB_PATH) as storage:
        removed = storage.remove_account(biz)
    if removed:
        console.print(f"[green]账号 {biz} 已删除[/green]")
    else:
        console.print(f"[yellow]未找到账号 {biz}[/yellow]")


@accounts_app.command("set-default")
def set_default_account(
    biz: str = typer.Argument(..., help="设置为默认账号的 fakeid")
) -> None:
    with open_storage(DB_PATH) as storage:
        storage.set_default_account(biz)
    console.print(f"[green]{biz} 已设为默认账号[/green]")


# ---------------------------------------------------------------------------
# Article helpers
@articles_app.command("sync")
def sync_articles(
    biz: Optional[str] = typer.Option(None, help="指定账号 fakeid，留空使用默认账号"),
    pages: int = typer.Option(1, min=1, help="抓取的分页数量，每页默认 10 篇"),
    page_size: int = typer.Option(DEFAULT_PAGE_SIZE, min=1, max=20, help="每页抓取数量"),
    force: bool = typer.Option(False, help="忽略跳过条件，强制同步", flag_value=True),
    skip_time: Optional[int] = typer.Option(
        None, min=1, help="多少分钟内同步过则跳过"
    ),
) -> None:
    _enforce_exclusive_flags(force, skip_time)
    with open_storage(DB_PATH) as storage:
        account = storage.get_account(biz)
        if not force and _should_skip_by_time(account.last_synced_at, skip_time):
            console.print(
                f"[yellow]该账号近期已同步，跳过（上次同步 {_format_last_synced(account.last_synced_at)}）[/yellow]"
            )
            return
        console.print(f"[cyan]开始同步 {account.nickname} 的文章[/cyan]")
        total_saved = 0
        columns = [
            SpinnerColumn(),
            TextColumn("{task.description}"),
            CountColumn(),
            BarColumn(),
            TimeElapsedColumn(),
            TextColumn("{task.fields[status]}"),
        ]
        with MPClient() as client, Progress(*columns, transient=True) as progress:
            task_id = progress.add_task(
                f"同步 {account.nickname}",
                total=None,
                completed=0,
                status="",
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
                    task_id=task_id,
                )
                status = "成功" if completed else "未完成"
                if completed and total_saved == 0:
                    status = "已是最新"
                progress.update(task_id, status=status)
            except RuntimeError as exc:
                progress.update(task_id, status="失败")
                console.print(f"[red]同步失败：{exc}[/red]")
                raise typer.Exit(code=1)
        console.print(f"[green]同步完成，共写入 {total_saved} 条记录[/green]")


@articles_app.command("sync-all")
def sync_all_articles(
    page_size: int = typer.Option(DEFAULT_PAGE_SIZE, min=1, max=20, help="每页抓取数量"),
    sleep_seconds: float = typer.Option(
        0.05, min=0, help="翻页间隔秒数（可为小数）"
    ),
    reset: bool = typer.Option(False, help="清除断点后从头同步", flag_value=True),
    force: bool = typer.Option(False, help="忽略跳过条件，强制同步", flag_value=True),
    skip_time: Optional[int] = typer.Option(
        None, min=1, help="多少分钟内同步过则跳过"
    ),
) -> None:
    _enforce_exclusive_flags(force, skip_time)
    with open_storage(DB_PATH) as storage:
        accounts = storage.list_accounts()
        if not accounts:
            console.print("[yellow]尚未保存任何账号，使用 `accounts add` 添加[/yellow]")
            return
        header = (
            "[cyan]开始同步全部账号（从最新文章往更早翻页）[/cyan]"
        )
        if reset:
            header = (
                "[cyan]开始同步全部账号（重置断点，从最新文章往更早翻页）[/cyan]"
            )
        if sleep_seconds > 0:
            header += f" [cyan]每页间隔 {sleep_seconds} 秒[/cyan]"
        console.print(header)
        with MPClient() as client:
            total_saved = 0
            summary: list[tuple[str, int]] = []
            columns = [
                SpinnerColumn(),
                TextColumn("{task.description}"),
                CountColumn(),
                BarColumn(),
                TimeElapsedColumn(),
                TextColumn("{task.fields[status]}"),
            ]
            with Progress(*columns, transient=False) as progress:
                for account in accounts:
                    resume_key = f"sync_progress:{account.biz}"
                    complete_key = f"sync_complete:{account.biz}"
                    if reset:
                        storage.delete_meta(resume_key)
                        storage.delete_meta(complete_key)
                    elif not force and storage.get_meta(complete_key) == _today_str():
                        task_id = progress.add_task(
                            f"同步 {account.nickname} ({account.biz})",
                            total=0,
                            completed=0,
                            status="跳过(今日已完成)",
                        )
                        progress.update(task_id, completed=0)
                        continue
                    if not force and _should_skip_by_time(account.last_synced_at, skip_time):
                        last_synced = _format_last_synced(account.last_synced_at)
                        task_id = progress.add_task(
                            f"同步 {account.nickname} ({account.biz})",
                            total=0,
                            completed=0,
                            status=f"跳过(近期已同步 {last_synced})",
                        )
                        progress.update(task_id, completed=0)
                        continue
                    task_id = progress.add_task(
                        f"同步 {account.nickname} ({account.biz})",
                        total=None,
                        completed=0,
                        status="",
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
                            task_id=task_id,
                        )
                        status = "成功" if completed else "未完成"
                        if completed and saved == 0:
                            status = "已是最新"
                        progress.update(task_id, status=status)
                        if completed:
                            storage.set_meta(complete_key, _today_str())
                    except RuntimeError as exc:
                        progress.update(task_id, status="失败")
                        console.print(f"[red]同步失败：{exc}[/red]")
                        raise typer.Exit(code=1)
                    total_saved += saved
                    summary.append((account.nickname or account.biz, saved))
            if summary:
                table = Table(show_header=True, header_style="bold green")
                table.add_column("账号")
                table.add_column("新增/更新", justify="right")
                for name, saved in summary:
                    table.add_row(name, str(saved))
                console.print(table)
        console.print(f"[green]全部账号同步完成，共写入 {total_saved} 条记录[/green]")


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
        console.print("[yellow]未找到文章，请先执行 `articles sync`[/yellow]")
        return
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("日期")
    table.add_column("标题")
    table.add_column("作者")
    table.add_column("链接")
    for article in articles:
        publish_date = (
            datetime.utcfromtimestamp(article.publish_at).strftime("%Y-%m-%d")
            if article.publish_at
            else "-"
        )
        table.add_row(publish_date, article.title, article.author or "-", article.link)
    console.print(table)


@articles_app.command("download")
def download_articles(
    biz: Optional[str] = typer.Option(None, help="指定账号 fakeid，留空使用默认账号"),
    limit: int = typer.Option(5, min=1, max=50, help="下载文章数量"),
    output_format: OutputFormat = typer.Option(
        OutputFormat.html, "--format", "-f", help="导出格式", show_default=True
    ),
    with_images: bool = typer.Option(True, help="是否下载图片", flag_value=True),
    since: Optional[str] = typer.Option(None, help="仅下载某日期后的文章"),
    output: Optional[Path] = typer.Option(None, help="自定义输出目录"),
) -> None:
    since_timestamp = _parse_since(since)
    with open_storage(DB_PATH) as storage:
        account = storage.get_account(biz)
        articles = storage.list_articles(account.biz, limit=limit, since_timestamp=since_timestamp)
    if not articles:
        console.print("[yellow]没有可下载的文章，先执行 `articles sync`[/yellow]")
        return
    target_dir = ensure_directory(output or DOWNLOAD_ROOT)
    console.print(f"[cyan]开始下载 {len(articles)} 篇文章 -> {target_dir}[/cyan]")
    fmt_value = (
        output_format.value
        if isinstance(output_format, OutputFormat)
        else str(output_format)
    )
    with ArticleDownloader(output_dir=target_dir) as downloader:
        results = downloader.download_many(
            articles,
            fmt=fmt_value,
            with_images=with_images,
            account_name=account.nickname or account.biz,
        )
    console.print(f"[green]下载完成，生成 {len(results)} 个目录[/green]")


@articles_app.command("download-single")
def download_single(
    url: str = typer.Argument(..., help="文章 URL"),
    output_format: OutputFormat = typer.Option(OutputFormat.html, "--format", "-f", help="导出格式"),
    with_images: bool = typer.Option(True, help="是否下载图片", flag_value=True),
    output: Optional[Path] = typer.Option(None, help="自定义输出目录"),
    title: Optional[str] = typer.Option(None, help="覆盖文章标题"),
) -> None:
    target_dir = ensure_directory(output or DOWNLOAD_ROOT)
    fmt_value = (
        output_format.value
        if isinstance(output_format, OutputFormat)
        else str(output_format)
    )
    with ArticleDownloader(output_dir=target_dir) as downloader:
        result = downloader.download_from_url(
            url,
            fmt=fmt_value,
            with_images=with_images,
            title=title,
        )
    console.print(f"[green]单篇文章已保存至 {result.output_path}[/green]")


# ---------------------------------------------------------------------------
@app.command("export-accounts")
def export_accounts() -> None:
    """Dump stored accounts as JSON (sensitive)."""
    with Storage(DB_PATH) as storage:
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
    console.print(json.dumps(payload, ensure_ascii=False, indent=2))


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
        console.print(f"[cyan]请使用微信扫码登录，二维码已保存：{qrcode_path}[/cyan]")
        started = time.time()
        while True:
            if time.time() - started > timeout:
                raise typer.Exit(code=1)
            resp = client.check_login_status(uuid_cookie)
            if resp.get("base_resp", {}).get("ret") != 0:
                console.print("[red]扫码状态获取失败，请重试[/red]")
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
                console.print(
                    f"[green]登录成功：{session.nickname or '未知账号'}[/green]"
                )
                return
            if status in (2, 3):
                qrcode_path.write_bytes(client.fetch_login_qrcode(uuid_cookie))
                console.print("[yellow]二维码已刷新，请重新扫码[/yellow]")
                time.sleep(poll_interval)
                continue
            if status in (4, 6):
                console.print("[cyan]扫码成功，等待确认...[/cyan]")
                time.sleep(poll_interval)
                continue
            if status == 5:
                console.print("[red]该账号尚未绑定邮箱，无法登录[/red]")
                raise typer.Exit(code=1)
            time.sleep(poll_interval)


__all__ = ["app"]
