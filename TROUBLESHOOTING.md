# 故障排查：TypeError: Secondary flag is not valid for non-boolean flag

## 错误信息

```
TypeError: Secondary flag is not valid for non-boolean flag.
```

## 原因

你运行的是 **已安装在 `.venv` 中的旧版本代码**，而不是最新提交的代码。

`.venv/bin/wechatcli` 这个可执行文件在 `pip install -e .` 时创建，它会缓存代码的路径和入口点。如果你修改了代码但没有重新安装，就会运行旧版本。

## 解决方案

### 方式 1：强制重新安装（推荐）

```bash
cd /home/sunnysab/Code/wechatcli
source .venv/bin/activate
pip install -e . --force-reinstall --no-deps
```

验证安装成功：
```bash
wechatcli account sync-all --help
```

### 方式 2：直接运行模块（无需安装）

```bash
cd /home/sunnysab/Code/wechatcli
source .venv/bin/activate
python -m wechatcli account sync-all --help
python -m wechatcli account sync-all --sleep-seconds 1 --skip-time 30
```

这会直接使用当前目录的代码，不依赖已安装的版本。

### 方式 3：重新创建虚拟环境

```bash
cd /home/sunnysab/Code/wechatcli
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 验证修复

运行以下命令应该都能正常工作：

```bash
# 帮助信息
wechatcli account sync-all --help

# 带值参数（空格）
wechatcli account sync-all --sleep-seconds 1 --skip-time 30

# 带值参数（等号）
wechatcli account sync-all --sleep-seconds=1 --skip-time=30

# 布尔标志
wechatcli account sync-all --reset
wechatcli account sync-all --force

# 组合使用
wechatcli account sync-all --page-size 10 --sleep-seconds 0.5 --force
```

## 如何确认使用的是哪个版本

### 检查已安装版本的位置

```bash
which wechatcli
# 应该输出: /home/sunnysab/Code/wechatcli/.venv/bin/wechatcli

python -c "import wechatcli; print(wechatcli.__file__)"
# 应该输出包含你的项目路径
```

### 检查代码是否最新

```bash
cd /home/sunnysab/Code/wechatcli
git log --oneline -1
# 应该显示最新的提交

grep "flag_value=True" wechatcli/cli.py
# 应该没有输出（说明已修复）
```

## 为什么会出现这个问题

### pip install -e 的工作原理

`pip install -e .`（可编辑安装）会：
1. 在 `.venv/lib/python3.x/site-packages/` 创建一个 `.egg-link` 文件
2. 在 `.venv/bin/` 创建可执行文件 `wechatcli`
3. 可执行文件包含固定的入口点代码

### 什么时候需要重新安装

- ✅ 修改了函数参数定义
- ✅ 修改了 `pyproject.toml` 中的依赖
- ✅ 修改了 `[project.scripts]` 入口点
- ✅ 添加/删除了模块文件
- ❌ 只修改函数内部代码（无需重新安装）

### 本次修复的内容

我们修改了函数参数定义（从 `flag_value=True` 改为 `--flag/--no-flag`），所以必须重新安装。

## 开发建议

### 开发时使用 python -m

开发调试时建议直接使用：
```bash
python -m wechatcli [command]
```

这样总是运行当前目录的最新代码，无需重新安装。

### 生产环境使用 pip install

部署到生产环境时：
```bash
pip install .  # 不带 -e，正常安装
```

## 快速检查脚本

保存以下脚本为 `check_version.sh`：

```bash
#!/bin/bash
echo "=== 检查 wechatcli 版本 ==="
echo ""
echo "可执行文件位置:"
which wechatcli
echo ""
echo "Python 模块位置:"
python -c "import wechatcli; print(wechatcli.__file__)"
echo ""
echo "最新 git 提交:"
git log --oneline -1
echo ""
echo "检查是否还有 flag_value=True:"
if grep -q "flag_value=True" wechatcli/cli.py; then
    echo "✗ 发现 flag_value=True (代码未更新)"
else
    echo "✓ 代码已修复"
fi
echo ""
echo "尝试运行命令:"
python -m wechatcli account sync-all --help 2>&1 | head -3
```

运行：
```bash
chmod +x check_version.sh
./check_version.sh
```
