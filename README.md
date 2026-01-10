# WeChat 文章导出 CLI (Python 版)

该目录包含一个基于 [Typer](https://typer.tiangolo.com) 的命令行工具，可在终端完成公众号管理、文章同步与下载。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate          # Windows 使用 .venv\Scripts\activate
pip install -r python/requirements-cli.txt
python -m python.wechatcli --help
```

如需安装为全局 CLI（开发模式）：

```bash
pip install -e .
wechatcli --help
```

> 工具支持扫码登录：执行 `login` 完成登录后，再用 `accounts search` 搜索并保存公众号（fakeid）。

## 常用命令

```bash
python -m python.wechatcli login                    # 扫码登录
python -m python.wechatcli accounts search 关键词    # 搜索公众号
python -m python.wechatcli accounts add             # 保存公众号 fakeid
python -m python.wechatcli accounts list            # 查看已保存的公众号
python -m python.wechatcli articles sync            # 拉取公众号文章列表
python -m python.wechatcli articles download --limit 5 --format html
python -m python.wechatcli articles download-single "https://mp.weixin.qq.com/..."
```

## 目录结构

```
python/
├── README.md                    # 本说明文件
├── requirements-cli.txt         # CLI 依赖列表
├── normalize_html.py            # HTML 清洗工具
└── wechatcli/                   # CLI 源码（Typer + SQLite + 下载器）
```

- `wechatcli/config.py`：基础配置与目录位置
- `wechatcli/storage.py`：SQLite 封装，负责账号与文章缓存
- `wechatcli/http.py`：对接公众号 profile 接口、拉取 HTML
- `wechatcli/downloader.py`：写入本地文件并下载图片资源
- `wechatcli/cli.py`：命令定义入口，可通过 `python -m python.wechatcli` 调用

## 数据存储

CLI 默认将账号与文章缓存存放在 `~/.local/share/wechatcli/cli.db`（或对应平台路径）。下载的文章会输出到 `~/.../wechatcli/downloads/`，可通过命令行参数 `--output` 自定义。
