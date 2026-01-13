# CLI 参数问题修复说明

## 问题描述

之前的命令行参数有严重问题：

```bash
# 错误：提示 "does not take a value"
wechatcli account sync-all --sleep-seconds=1

# 错误：提示 "unexpected extra arguments"
wechatcli account sync-all --sleep-seconds 1 --skip-time 30
```

## 原因分析

使用了 Typer 的 `flag_value=True` 参数，这会导致 Click（Typer 底层库）将参数误认为是布尔标志位，不接受值。

这是 Typer 的已知问题，文档建议使用 `--flag/--no-flag` 语法来定义布尔参数。

## 修复后的用法

### 布尔参数（开关）

**现在统一使用 `--flag/--no-flag` 语法：**

```bash
# 启用重置
wechatcli account sync-all --reset

# 禁用重置（显式）
wechatcli account sync-all --no-reset

# 强制同步
wechatcli account sync-all --force

# 下载时禁用图片
wechatcli articles sync <account> --no-images

# 设置为默认账号
wechatcli account add --biz xxx --nickname "测试" --set-default
```

### 带值的参数

**现在可以正常使用：**

```bash
# 方式 1: 空格分隔（推荐）
wechatcli account sync-all --sleep-seconds 1 --skip-time 30

# 方式 2: 等号
wechatcli account sync-all --sleep-seconds=1 --skip-time=30

# 组合使用
wechatcli account sync-all --page-size 10 --sleep-seconds 0.5 --force
```

## 完整示例

```bash
# 同步所有账号，每页 10 篇，间隔 1 秒，30 分钟内同步过的跳过
wechatcli account sync-all --page-size 10 --sleep-seconds 1 --skip-time 30

# 重置断点，从头同步
wechatcli account sync-all --reset

# 强制同步（忽略所有跳过条件）
wechatcli account sync-all --force

# 下载文章，不下载图片
wechatcli articles sync-all --no-images

# 下载文章，启用详细日志
wechatcli --verbose articles sync-all --profile production
```

## 所有修复的参数

| 命令 | 参数 | 旧语法问题 | 新语法 |
|------|------|------------|--------|
| `account add` | `--set-default` | ✗ 不接受值 | ✓ `--set-default/--no-set-default` |
| `account search` | `--interactive` | ✗ 不接受值 | ✓ `--interactive/--no-interactive` |
| `account sync` | `--force` | ✗ 不接受值 | ✓ `--force/--no-force` |
| `account sync-all` | `--reset` | ✗ 不接受值 | ✓ `--reset/--no-reset` |
| `account sync-all` | `--force` | ✗ 不接受值 | ✓ `--force/--no-force` |
| `articles sync` | `--with-images` | ✗ 不接受值 | ✓ `--with-images/--no-images` |
| `articles sync-all` | `--with-images` | ✗ 不接受值 | ✓ `--with-images/--no-images` |
| `articles download` | `--with-images` | ✗ 不接受值 | ✓ `--with-images/--no-images` |
| `articles backfill-images` | `--dry-run` | ✗ 不接受值 | ✓ `--dry-run/--no-dry-run` |

## 技术细节

**问题根源：**
```python
# 错误用法（会导致参数解析失败）
flag: bool = typer.Option(False, help="...", flag_value=True)

# 正确用法
flag: bool = typer.Option(False, "--flag/--no-flag", help="...")
```

**为什么会出错：**
- `flag_value=True` 告诉 Click 这是一个标志位（不需要值）
- 但 Typer 同时也设置了其他元数据（min/max/type）
- 导致 Click 混淆，无法正确解析参数

**修复方式：**
- 移除所有 `flag_value=True`
- 使用 Click/Typer 推荐的 `--flag/--no-flag` 语法
- 参数名统一使用连字符 `-` 而不是下划线 `_`

## 验证修复

```bash
# 应该可以正常工作
wechatcli account sync-all --help
wechatcli account sync-all --sleep-seconds 1 --skip-time 30
wechatcli articles sync-all --no-images --workers 10
```
