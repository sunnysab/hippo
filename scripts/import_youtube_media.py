#!/usr/bin/env python3
"""Import a channel's already-downloaded videos into hippo, through the object store.

A one-off for a library somebody downloaded with yt-dlp before hippo existed: the
files carry no video id, only a title, so the channel's listing is fetched and the
two are matched on a normalised title. Each match becomes an article whose *video*
lives in the object store (``media/video/<article_pk>.<ext>``) and whose transcript
job then reads that object instead of downloading the video again.

    uv run scripts/import_youtube_media.py --channel youtube:UC… --dir ~/youtube-media/某频道 --dry-run
    uv run scripts/import_youtube_media.py --channel youtube:UC… --dir ~/youtube-media/某频道

Nothing is written until ``--dry-run`` is left out. Run it on the machine that has
the files and the database (the script talks to both directly).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hippo.logger import get_logger
from hippo.media import archive as archive_module
from hippo.media.pipeline import _load_article
from hippo.media_settings import get_media_settings
from hippo.sources.registry import build_media_sources
from hippo.storage import open_storage

logger = get_logger(__name__)

#: `001 - 标题.webm` → the title. yt-dlp's numeric prefix is for ordering.
_INDEX_PREFIX = re.compile(r'^\d+\s*[-–—]\s*')
#: The publication date inside a title (「高盛」20260812：…). It survives a rename
#: even when the label in front changes, which is what makes it a useful fallback.
_TITLE_DATE = re.compile(r'(20\d{6})')

#: Kept for a normalised comparison: letters, digits and CJK. Everything else
#: (spaces, punctuation, the full-width variants YouTube sanitises) is dropped,
#: because the two sides come from different serialisations of the same title.
_KEEP = re.compile(r'[^0-9A-Za-z\u4e00-\u9fff]+')

CONTENT_TYPES = {
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mkv': 'video/x-matroska',
    '.m4v': 'video/x-m4v',
}

VIDEO_SUFFIXES = tuple(CONTENT_TYPES)

#: Codecs that are already as good as H.265, so re-encoding one would cost quality
#: and CPU for nothing. Measured over this library: 367 of 552 files are AV1 and 95
#: are VP9 — only the 90 H.264 files (6.4 GB) have anything to gain.
KEEP_VIDEO_CODECS = {'hevc', 'av1', 'vp9'}

#: H.265 settings for talking-head 1080p: CRF 26 keeps text readable, `hvc1` is what
#: Apple's players look for, and `+faststart` lets a browser start before the end.
#: `faster` rather than `medium`: a 66 MB file had not finished after five minutes at
#: `medium` while the machine was busy transcribing, and the difference between the
#: presets is not worth hours of CPU on this content.
X265_ARGS = ['-c:v', 'libx265', '-crf', '26', '-preset', 'faster', '-tag:v', 'hvc1', '-movflags', '+faststart']


#: How much of a normalised title identifies a video. The channels in question edit
#: their titles continuously (a 「高盛」 prefix appears months later), so an exact
#: match finds a third of the library; the date plus the first few words is unique.
PREFIX_CHARS = 12


def normalise(title: str) -> str:
    return _KEEP.sub('', title).lower()


def match_titles(items: list[Any], files: dict[str, Path]) -> tuple[list[tuple[Any, Path]], int]:
    """Pair videos with files: exact first, then by a unique prefix."""
    taken: set[Path] = set()
    matched: list[tuple[Any, Path]] = []
    unmatched: list[Any] = []
    for item in items:
        path = files.get(normalise(item.title))
        if path is not None and path not in taken:
            taken.add(path)
            matched.append((item, path))
        else:
            unmatched.append(item)

    # 前缀匹配：只有唯一命中才认，避免「同一栏目同一天」的两条互相顶替。
    by_prefix: dict[str, list[Path]] = {}
    for key, path in files.items():
        if path not in taken:
            by_prefix.setdefault(key[:PREFIX_CHARS], []).append(path)
    for item in unmatched:
        candidates = by_prefix.get(normalise(item.title)[:PREFIX_CHARS], [])
        if len(candidates) == 1:
            taken.add(candidates[0])
            matched.append((item, candidates[0]))

    # 日期兜底：标题里的 8 位日期在改名后通常还在，本地与列表都唯一时才认。
    by_date: dict[str, list[Path]] = {}
    for path in files.values():
        if path in taken:
            continue
        found = _TITLE_DATE.search(path.name)
        if found:
            by_date.setdefault(found.group(1), []).append(path)
    still_unmatched: list[Any] = []
    for item in unmatched:
        if any(entry[0] is item for entry in matched):
            continue
        found = _TITLE_DATE.search(item.title)
        candidates = by_date.get(found.group(1), []) if found else []
        if len(candidates) == 1 and candidates[0] not in taken:
            taken.add(candidates[0])
            matched.append((item, candidates[0]))
        else:
            still_unmatched.append(item)
    return matched, len(still_unmatched)


def local_files(directory: Path) -> dict[str, Path]:
    """Normalised title → file, for every video in ``directory``."""
    found: dict[str, Path] = {}
    for path in sorted(directory.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        title = _INDEX_PREFIX.sub('', path.stem)
        found.setdefault(normalise(title), path)
    return found


async def list_channel(source: Any, source_key: str, *, wanted: int, page_size: int = 100) -> list[Any]:
    """Walk the channel listing until ``wanted`` items or the end."""
    items: list[Any] = []
    cursor: str | None = None
    while len(items) < wanted:
        listing = await source.list_items(source_key=source_key, limit=page_size, cursor=cursor)
        items.extend(listing.items)
        print(f'  列表：{len(items)} 条（本页 {len(listing.items)}）', flush=True)
        if listing.is_end or not listing.next_cursor or not listing.items:
            break
        cursor = listing.next_cursor
    return items


def video_codec(path: Path) -> str:
    """The video stream's codec name, or `''` when ffprobe cannot say."""
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_name',
         '-of', 'default=nw=1:nk=1', str(path)],
        capture_output=True, text=True,
    )
    return result.stdout.strip().lower()


def reencode(path: Path, *, workdir: Path) -> Path:
    """H.264 (or anything unusual) → H.265 in MP4, with the audio kept as it is.

    Returns the file to upload: the transcode, or the original when there is nothing
    to gain. The transcode is written next to the original and removed by the caller.
    """
    codec = video_codec(path)
    if codec in KEEP_VIDEO_CODECS:
        return path
    target = workdir / f'{path.stem}.h265.mp4'
    result = subprocess.run(
        ['ffmpeg', '-v', 'error', '-y', '-i', str(path), *X265_ARGS, '-c:a', 'copy', str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        logger.warning('转码失败，按原文件上传（%s）：%s', path.name, result.stderr.strip()[-200:])
        target.unlink(missing_ok=True)
        return path
    before, after = path.stat().st_size, target.stat().st_size
    logger.info('H.265：%s %s → %s（省 %.0f%%）', path.name[:40], codec,
                f'{after // 1024 // 1024}MB', (1 - after / before) * 100)
    return target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--channel', required=True, help='账号 biz，如 youtube:UC…')
    parser.add_argument('--dir', required=True, help='本地视频目录')
    parser.add_argument('--limit', type=int, default=0, help='最多处理多少个文件（0 = 全部）')
    parser.add_argument(
        '--enqueue-limit',
        type=int,
        default=0,
        help='顺带入队多少个转写任务（0 = 不入队；导入后可按需要转写）',
    )
    parser.add_argument('--dry-run', action='store_true', help='只报告匹配结果，不写库不上传')
    parser.add_argument(
        '--transcode',
        choices=('auto', 'off'),
        default='auto',
        help='auto = 只把 H.264 之类压成 H.265/MP4（AV1/VP9/HEVC 不动）；off = 原样上传',
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    directory = Path(args.dir).expanduser()
    if not directory.is_dir():
        print(f'目录不存在：{directory}', file=sys.stderr)
        return 1

    files = local_files(directory)
    print(f'本地视频 {len(files)} 个（按标题归一化去重后）')

    async with open_storage() as storage:
        settings = await get_media_settings(storage)
        sources = await build_media_sources(storage=storage, settings=settings)
        try:
            adapter = sources.adapters.get('youtube')
            source = sources.sources.get('youtube')
            if adapter is None or source is None:
                print('youtube 来源不可用', file=sys.stderr)
                return 1
            source_key = args.channel.split(':', 1)[-1]
            print(f'拉取 {args.channel} 的列表（本地 {len(files)} 个文件，按需翻页）…')
            items = await list_channel(source, source_key, wanted=len(files) + 50)

            matched, unmatched = match_titles(items, files)
            if args.limit:
                matched = matched[: args.limit]
            print(f'列表 {len(items)} 条 → 匹配 {len(matched)} 个文件，{unmatched} 条没对上')
            for item, path in matched[:5]:
                print(f'  例：{item.article_id}  {path.name[:60]}')
            if args.dry_run:
                print('（dry-run）没有写库、没有上传')
                return 0
            if not matched:
                print('没有可导入的文件')
                return 0

            pks = await adapter._store(args.channel, [item for item, _ in matched])
            await storage.commit()
            uploaded = 0
            skipped = 0
            for index, (item, path) in enumerate(matched, start=1):
                article_pk = pks.get(item.article_id)
                if not article_pk:
                    continue
                # 已经在对象存储里的跳过：这个脚本可以反复跑，断了接着跑。
                existing = await storage.media_assets.get(article_pk, kind='video')
                if existing and existing.get('state') == 'stored':
                    skipped += 1
                    continue
                source_path = await asyncio.to_thread(reencode, path, workdir=directory) \
                    if args.transcode == 'auto' else path
                content_type = CONTENT_TYPES.get(source_path.suffix.lower(), 'video/mp4')
                try:
                    key, size = await asyncio.to_thread(
                        archive_module.store_media,
                        article_pk=article_pk,
                        path=source_path,
                        content_type=content_type,
                        kind='video',
                    )
                except Exception as exc:  # 一个文件失败不该让整批停下
                    logger.warning('上传失败 %s：%s', path.name, exc)
                    continue
                await storage.media_assets.save(
                    article_pk=article_pk,
                    kind='video',
                    orig_url=item.link,
                    content_type=content_type,
                    size_bytes=size,
                    duration_seconds=item.duration_seconds,
                    s3_key=key,
                )
                # 先写一个视频块：读者立刻能播，不必等转写排到它（转写完成后会用
                # 「视频 + 留档音频 + 正文」覆盖这一块）。
                article = await _load_article(storage, article_pk)
                await storage.articles.save_article_content(
                    article,
                    url_token=None,
                    title=article.title,
                    item_show_type=article.item_show_type,
                    content_markdown=item.digest or '',
                    content_blocks=[
                        {
                            'type': 'video',
                            'url': f'/api/media/{article_pk}/video',
                            'cover_url': item.cover,
                            'video_id': item.article_id,
                        }
                    ],
                    cover_url=None,
                    images=[],
                )
                await storage.commit()
                if source_path != path:
                    source_path.unlink(missing_ok=True)
                uploaded += 1
                if index % 10 == 0 or index == len(matched):
                    print(f'  已上传 {uploaded} / 跳过 {skipped} / 共 {len(matched)}'
                          f'（最近：{path.name[:44]}）', flush=True)

            # 视频已经在对象存储里了，排队的任务会用那个对象当源，不再下载。
            queued = 0
            if args.enqueue_limit:
                slice_of = matched[: args.enqueue_limit]
                queued = await adapter._enqueue([item for item, _ in slice_of], pks)
                await storage.commit()
            print(f'完成：文章 {len(pks)} 篇，视频入 S3 {uploaded} 个（跳过已存在 {skipped} 个），'
          f'转写任务入队 {queued} 个')
        finally:
            await sources.aclose()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
