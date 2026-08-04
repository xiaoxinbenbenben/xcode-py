# xcode

本地 coding agent（包名 `xcode`）。标尺对齐 PaiCLI-Python，自研 ReAct，不上 OpenAI Agents SDK。

## 怎么跑

```bash
uv sync --extra dev
uv run xcode --version
uv run pytest -q
```

配置继续读 `.env`（`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` 等）。DeepSeek 等 OpenAI-compatible 接口改 base_url 即可。

## 约定

- 项目说明：根目录 `XCODE.md`（可共享）；本机补充用 `XCODE.local.md`（已 gitignore）
- 路线与完成定义见 `docs/todo.md`；交接见 `docs/handoff.md`
