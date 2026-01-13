# 搜狗代理 URL 问题修复说明

## 问题描述
下载图片时出现 403 错误，URL 类似：
```
http://img01.store.sogou.com/net/a/04/link?appid=100520031&w=710&url=http://mmbiz.qpic.cn/...
```

## 修复内容（已提交）

1. **扩大搜狗代理检测范围**
   - 之前：只匹配 `img01.store.sogou.com`、`img02.store.sogou.com` 等
   - 现在：匹配所有 `sogou.com` 域名

2. **自动提取实际图片 URL**
   - 从 `url=` 参数中提取微信图片的真实地址
   - 直接下载 `mmbiz.qpic.cn` 而不是搜狗代理

3. **图片下载超时设置为 15 秒**

## 如何应用修复

### 方式 1: 重新安装（推荐）
```bash
cd /home/sunnysab/Code/wechatcli
pip install -e . --force-reinstall --no-deps
```

### 方式 2: 直接运行模块
```bash
cd /home/sunnysab/Code/wechatcli
python -m wechatcli articles sync-all --profile production
```

### 方式 3: 重启 Python 进程
如果你在交互式 Python 环境或 Jupyter 中：
```python
import importlib
import wechatcli.downloader
importlib.reload(wechatcli.downloader)
```

## 验证修复生效

运行诊断脚本：
```bash
python check_sogou_fix.py
```

或启用详细日志查看：
```bash
wechatcli --verbose articles sync <account>
```

应该能看到类似日志：
```
DEBUG: Unwrapped Sogou proxy URL: http://img01.store.sogou.com/... -> http://mmbiz.qpic.cn/...
```

## 测试代码

```python
from urllib.parse import urlparse, parse_qs

url = "http://img01.store.sogou.com/net/a/04/link?appid=100520031&w=710&url=http://mmbiz.qpic.cn/mmbiz/test.jpg"
lowered = url.lower()

if "sogou.com" in lowered and "url=" in lowered:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "url" in params:
        actual_url = params["url"][0]
        print(f"✓ Extracted: {actual_url}")
    else:
        print("✗ No url parameter")
else:
    print("✗ Not a Sogou proxy URL")
```

预期输出：
```
✓ Extracted: http://mmbiz.qpic.cn/mmbiz/test.jpg
```

## 相关提交

- `493e32f` - fix: improve Sogou proxy handling and set image timeout to 15s
- `9c13686` - fix: skip file:// URLs when downloading images
