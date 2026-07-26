# xcode — pure-Python local coding agent

标准包 `xcode`，**单一入口**（CLI + TUI/REPL）。

## 安装

```bash
uv sync --extra dev
# 或
pip install -e ".[dev]"
```

准备 `.env`（可参考 `.env.example`）：

```env
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://your-compatible-endpoint/v1
OPENAI_MODEL=gpt-4o-mini
```

## 运行（单入口）

```bash
# 交互 TUI/REPL
uv run xcode
# 或
uv run python -m xcode

# 单次调用
uv run xcode -p "列出当前目录"

# 子命令
uv run xcode doctor
uv run xcode session list
uv run xcode session new --workspace /path/to/project
```
