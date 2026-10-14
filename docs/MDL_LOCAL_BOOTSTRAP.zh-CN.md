# DB-GPT MDL 本地启动与复现

本文记录 `db-gpt-mdl` 从全新克隆到可访问服务的完整流程。内容按 `dev` 分支提交 `ab4f80c` 验证，适用于 macOS/Linux 的源码运行方式。

## 1. 环境要求

- Git，可访问 GitHub。
- Python 3.12。MDL 依赖按 Python 3.12 验证。
- Node.js 20 和 npm。Node.js 26 可以完成 webpack 编译，但当前依赖组合在静态页面数据收集阶段仍可能触发 `d3-shape` 的 ESM 断言。
- 一个可写的本地工作目录。运行时会生成 SQLite、DuckDB、日志和前端静态文件。
- WrenAI 源码的固定提交。`requirements-mdl.txt` 通过相对路径引用 `../WrenAI/core/wren`。

模型 API Key 不是启动 MDL 工作台的前置条件。没有 Key 时，服务会以 MDL-only 模式启动，支持模型编辑、权限校验和本地 DuckDB smoke 测试；需要自然语言问数时，再配置 `OPENAI_API_KEY` 或运行时模型凭据文件。

默认端口如下：

| 服务 | 默认端口 | 用途 |
| --- | ---: | --- |
| DB-GPT Web | `18765` | Web UI 和 API |
| 模型控制器 | `18766` | 配置模型 API Key 后使用 |

如果端口已被其他实例占用，修改 `configs/dbgpt-mdl.toml` 中的两个端口即可。启动脚本会从该配置读取 Web 和控制器地址，`status` 与 `start --wait` 会跟随修改后的端口。

## 2. 克隆工程

把 WrenAI 和 DB-GPT MDL 放在同一个父目录，使 `../WrenAI/core/wren` 能被解析：

```bash
mkdir -p ~/source/agent/问数智能体
cd ~/source/agent/问数智能体

git clone https://github.com/Canner/WrenAI.git WrenAI
git -C WrenAI checkout fedf182b4bb8294623d9cc0a9db4fbae1edf63fb

git clone -b dev https://github.com/kevin1986xu/db-gpt-mdl.git db-gpt-mdl-local
cd db-gpt-mdl-local
```

已有相同提交的 WrenAI 时可以跳过 clone，只需确认下面的文件存在：

```text
../WrenAI/core/wren/pyproject.toml
```

检查 DB-GPT MDL 来源和提交：

```bash
git remote -v
git status --short --branch
git log -1 --oneline
```

## 3. 创建 Python 环境

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-mdl.txt
```

不要使用系统 Python 直接启动，以免把本地包安装到全局环境。

## 4. 初始化本地运行时

```bash
.venv/bin/python scripts/mdl_local.py setup
```

首次执行会创建：

- `configs/mdl.json`：当前生效的 MDL 文档。
- `.runtime/mdl/demo.duckdb`：包含 `raw_orders`、`raw_customers` 和受保护的 `secret_payroll`。
- `.runtime/mdl/dbgpt.db`：DB-GPT 元数据库和 `mdl_demo` 数据源记录。
- `.runtime/mdl/credentials.json`：本地 `admin`、`analyst` 令牌，权限为 `0600`。
- `.runtime/mdl/alembic.ini` 与 `.runtime/mdl/alembic/`：源码可编辑安装所需的数据库迁移模板。

运行时目录在 Git 中被忽略，不要提交令牌、模型 Key、DuckDB 数据库或日志。

全新元数据库首次启动需要执行 Alembic 初始化，`configs/dbgpt-mdl.toml` 中的 `disable_alembic_upgrade` 应保持为 `false`。迁移完成后可以按部署策略重新关闭自动迁移。

## 5. 构建前端

使用 Node 20：

```bash
cd web
npm ci --legacy-peer-deps --ignore-scripts
npm run compile
cd ..
```

`npm run compile` 会生成 `web/out`，启动脚本检测到该目录后会让 DB-GPT 使用集成 MDL 工作台。

本次验证中 webpack 编译成功，但 Next.js 在收集 `PromptSelect`、`NativeApp` 等页面时出现：

```text
Error [ERR_INTERNAL_ASSERTION]: Unexpected module status 0
Cannot require() ES Module ... d3-shape/src/index.js
```

这是当前 `@antv/g2`、`d3-shape`、Next.js 和 Node 的依赖组合问题，和 DB-GPT Python 服务无关。遇到该错误时：

1. 确认 Node 版本为 20，并重新执行 `npm ci` 和 `npm run compile`。
2. 保留完整构建日志，避免把 npm 自动改写的锁文件直接提交。
3. 如果已有同一 Git 提交生成的 `web/out`，可以临时复用该静态产物启动后端；静态产物必须与当前提交一致。

## 6. 端口与模型配置

默认配置位于 `configs/dbgpt-mdl.toml`：

```toml
[service.web]
host = "127.0.0.1"
port = 18765

[service.model.controller]
host = "127.0.0.1"
port = 18766
```

如果 `18765` 已经被旧实例占用，把两个端口改成一组空闲端口，例如：

```toml
[service.web]
port = 18767

[service.model.controller]
port = 18768
```

修改配置后必须重启服务。模型 Key 可以通过环境变量提供：

```bash
export OPENAI_API_KEY='sk-...'
```

也可以写入 `.runtime/mdl/model_credentials.json`：

```json
{"openai_api_key": "sk-..."}
```

该文件应保持 `0600` 权限，不能提交到 Git。

## 7. 启动与验证

```bash
.venv/bin/python scripts/mdl_local.py start --wait 60
.venv/bin/python scripts/mdl_local.py status
```

浏览器打开：

```text
http://127.0.0.1:18765/construct/mdl/
```

使用并行端口示例时访问 `http://127.0.0.1:18767/construct/mdl/`。自动登录默认打开本地 `admin` 工作台；要改为手工登录：

```bash
DBGPT_MDL_AUTO_LOGIN=0 .venv/bin/python scripts/mdl_local.py start --wait 60
```

建议至少执行以下检查：

```bash
# MDL 权限和 SQL 计划验证
.venv/bin/python scripts/mdl_local.py smoke

# MDL 会话接口应返回 HTTP 200
curl -i http://127.0.0.1:18765/api/v1/mdl/session

# 查看最近日志
.venv/bin/python scripts/mdl_local.py tail -n 120
```

本次启动验证结果：页面 HTTP 200，会话 HTTP 200，`admin` 查询结果为 `5200`，`analyst` 查询结果为 `4300`，未出现在 MDL 中的 `secret_payroll` 会被拒绝。

## 8. 停止服务

```bash
.venv/bin/python scripts/mdl_local.py stop
```

如果启动进程异常退出，先查看：

```bash
.venv/bin/python scripts/mdl_local.py tail -n 160
cat .runtime/mdl/dbgpt-mdl.pid
```

不要直接删除 `.runtime/mdl/dbgpt.db` 或 `configs/mdl.json`；其中可能包含已经保存的本地 MDL 文档和数据源配置。

## 9. 源码测试

```bash
.venv/bin/python -m pip install pytest ruff
PYTHONPATH=packages/dbgpt-app/src:packages/dbgpt-core/src:packages/dbgpt-ext/src:packages/dbgpt-serve/src:packages/dbgpt-client/src:packages/dbgpt-sandbox/src \\
  .venv/bin/python -m pytest tests/mdl packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/mdlint/tests -q
```

本地启动验证与源码测试是两条独立路径：前者证明服务能启动并完成 MDL 权限计划，后者覆盖 Python 单元测试。

## 10. 提交前检查

```bash
git status --short
git diff --check
git diff --stat
```

不要提交以下内容：`.venv/`、`web/node_modules/`、`web/out/`、`.runtime/`、模型 Key、令牌、SQLite/DuckDB 数据库和日志。
