#!/usr/bin/env python3
"""冒烟检查：weixin-rs daemon 是否给出列表与正文。

用法::

    WEIXIN_SDK_PATH=../weixin-rs/sdk/python \
      python3 scripts/wx_source_check.py --key hqsbwx --biz MjM5MDk1NzQzMQ== --pages 1
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hippo.weixin_source import WeixinSource


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--key', default='hqsbwx', help='微信号 alias 或 gh_（daemon 认这个）')
    p.add_argument('--biz', default='MjM5MDk1NzQzMQ==', help='PG accounts.biz（Mz…==，仅用于打印）')
    p.add_argument('--pages', type=int, default=1)
    p.add_argument('--bodies', type=int, default=1, help='顺带抓几篇正文')
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    async with WeixinSource() as source:
        items = await source.list_articles(args.key, args.biz, pages=args.pages)
        print(f'列表：{len(items)} 篇')
        for item in items[:5]:
            print(f'  sn={item.sn} | {str(item.payload.get("title", ""))[:28]} | {item.long_link[:64]}')
        if not items or args.bodies <= 0:
            return 0 if items else 1

        bodies = await source.fetch_bodies([i.long_link for i in items[: args.bodies]])
        print(f'正文：{len(bodies)} 篇')
        for body in bodies:
            print(
                f'  {body.title[:28]} | slug={body.slug} | {len(body.html)} 字符 | '
                f'{len(body.images)} 图 | {body.short_link}'
            )
        return 0 if bodies else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
