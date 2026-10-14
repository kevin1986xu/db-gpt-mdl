#!/usr/bin/env python3
"""Local runtime helper for the DB-GPT/Wren MDL integration."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterable

import tomllib

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "mdl"
DEMO_DB = RUNTIME / "demo.duckdb"
METADATA_DB = RUNTIME / "dbgpt.db"
PID_FILE = RUNTIME / "dbgpt-mdl.pid"
CONTROLLER_PID_FILE = RUNTIME / "dbgpt-mdl-controller.pid"
LOG_FILE = RUNTIME / "dbgpt-mdl.log"
CONTROLLER_LOG_FILE = RUNTIME / "dbgpt-mdl-controller.log"
CREDENTIALS = RUNTIME / "credentials.json"
MODEL_CREDENTIALS = RUNTIME / "model_credentials.json"
CONFIG = ROOT / "configs" / "dbgpt-mdl.toml"
MDL = ROOT / "configs" / "mdl.json"
MDL_EXAMPLE = ROOT / "configs" / "mdl.example.json"
PILOT_META_DATA = ROOT / "pilot" / "meta_data"
PYTHON = ROOT / ".venv" / "bin" / "python"

SOURCE_PATHS = [
    ROOT / "packages" / "dbgpt-core" / "src",
    ROOT / "packages" / "dbgpt-app" / "src",
    ROOT / "packages" / "dbgpt-ext" / "src",
    ROOT / "packages" / "dbgpt-serve" / "src",
    ROOT / "packages" / "dbgpt-client" / "src",
    ROOT / "packages" / "dbgpt-sandbox" / "src",
]


def source_path() -> str:
    return os.pathsep.join(str(path) for path in SOURCE_PATHS)


def service_url(name: str, default_port: int) -> str:
    with CONFIG.open("rb") as stream:
        config = tomllib.load(stream)
    service: object = config.get("service", {})
    for part in name.split("."):
        if not isinstance(service, dict):
            service = {}
            break
        service = service.get(part, {})
    if not isinstance(service, dict):
        service = {}
    host = str(service.get("host", "127.0.0.1"))
    port = int(service.get("port", default_port))
    return f"http://{host}:{port}"


def runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_path() + (os.pathsep + existing if existing else "")
    env["DBGPT_MDL_ENABLED"] = "1"
    # The local demo owns the generated credentials, so the workbench can open
    # as admin without exposing the random token in the browser bundle.
    env["DBGPT_MDL_AUTO_LOGIN"] = env.get("DBGPT_MDL_AUTO_LOGIN", "1")
    model_api_key = env.get("OPENAI_API_KEY", "")
    if not model_api_key and MODEL_CREDENTIALS.exists():
        try:
            model_api_key = json.loads(
                MODEL_CREDENTIALS.read_text(encoding="utf-8")
            ).get("openai_api_key", "")
        except (OSError, json.JSONDecodeError, AttributeError):
            model_api_key = ""
    with CONFIG.open("rb") as stream:
        config = tomllib.load(stream)
    models = config.get("models", {})
    has_models = any(models.get(kind) for kind in ("llms", "embeddings", "rerankers"))
    env["DBGPT_MDL_ONLY"] = "0" if has_models and model_api_key else "1"
    if model_api_key:
        env["OPENAI_API_KEY"] = model_api_key
    exported_web = ROOT / "web" / "out"
    if (exported_web / "construct" / "mdl" / "index.html").is_file():
        env["DBGPT_WEB_STATIC_DIR"] = str(exported_web)
    mdl_database = "mdl_demo"
    try:
        document = json.loads(MDL.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document.get("database"):
            mdl_database = str(document["database"])
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    env["DBGPT_MDL_DATABASE"] = mdl_database
    env["DBGPT_MDL_PATH"] = str(MDL)
    env["DBGPT_MDL_CREDENTIALS"] = str(CREDENTIALS)
    env["DBGPT_CORS_ALLOWED_ORIGINS"] = env.get("DBGPT_CORS_ALLOWED_ORIGINS", "*")
    return env


def initialize_database(path: Path) -> None:
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with duckdb.connect(str(path)) as db:
        db.execute(
            "CREATE TABLE raw_customers(id INTEGER PRIMARY KEY, name VARCHAR, "
            "region VARCHAR, email VARCHAR)"
        )
        db.execute(
            "CREATE TABLE raw_orders(id INTEGER PRIMARY KEY, customer_id INTEGER, "
            "amount DOUBLE, cost DOUBLE, status VARCHAR, region VARCHAR)"
        )
        db.execute("CREATE TABLE secret_payroll(employee VARCHAR, salary DOUBLE)")
        db.executemany(
            "INSERT INTO raw_customers VALUES (?, ?, ?, ?)",
            [
                (1, "星河科技", "华东", "xinghe@example.test"),
                (2, "青山零售", "华南", "qingshan@example.test"),
                (3, "云帆贸易", "华东", "yunfan@example.test"),
            ],
        )
        db.executemany(
            "INSERT INTO raw_orders VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 1, 1200, 700, "paid", "华东"),
                (2, 1, 1800, 1000, "paid", "华东"),
                (3, 2, 900, 500, "paid", "华南"),
                (4, 3, 1300, 800, "paid", "华东"),
                (5, 3, 500, 300, "cancelled", "华东"),
            ],
        )
        db.execute("INSERT INTO secret_payroll VALUES ('演示员工', 20000)")


def initialize_credentials() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    os.chmod(RUNTIME, 0o700)
    if CREDENTIALS.exists():
        return
    import secrets

    credentials = {
        "admin": secrets.token_urlsafe(32),
        "analyst": secrets.token_urlsafe(32),
    }
    fd = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(credentials, stream, indent=2)
        stream.write("\n")


def initialize_mdl_manifest() -> None:
    MDL.parent.mkdir(parents=True, exist_ok=True)
    if MDL.exists():
        return
    shutil.copyfile(MDL_EXAMPLE, MDL)


def initialize_pilot_metadata() -> None:
    """Provision Alembic templates for editable source checkouts."""
    if not PILOT_META_DATA.is_dir():
        return
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for source in PILOT_META_DATA.rglob("*"):
        if not source.is_file():
            continue
        target = RUNTIME / source.relative_to(PILOT_META_DATA)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def initialize_metadata() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(METADATA_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS connect_config (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              db_type VARCHAR(255) NOT NULL,
              db_name VARCHAR(255) NOT NULL UNIQUE,
              db_path VARCHAR(255),
              db_host VARCHAR(255),
              db_port VARCHAR(255),
              db_user VARCHAR(255),
              db_pwd VARCHAR(255),
              comment TEXT,
              sys_code VARCHAR(128),
              user_id VARCHAR(128),
              user_name VARCHAR(128),
              gmt_created DATETIME DEFAULT CURRENT_TIMESTAMP,
              gmt_modified DATETIME DEFAULT CURRENT_TIMESTAMP,
              ext_config TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO connect_config (
              db_type, db_name, db_path, db_host, db_port, db_user, db_pwd,
              comment, sys_code, user_id, user_name, ext_config
            ) VALUES (
              'duckdb', 'mdl_demo', ?, '', '0', '', '',
              'Local Wren MDL demo datasource', '', '', '', '{}'
            )
            ON CONFLICT(db_name) DO NOTHING
            """,
            (str(DEMO_DB),),
        )
        conn.commit()


def setup(_: argparse.Namespace) -> None:
    initialize_mdl_manifest()
    initialize_database(DEMO_DB)
    initialize_credentials()
    initialize_pilot_metadata()
    initialize_metadata()
    print(f"Demo database: {DEMO_DB}")
    print(f"DB-GPT metadata: {METADATA_DB}")
    print(f"MDL credentials: {CREDENTIALS}")
    if MODEL_CREDENTIALS.exists():
        print("OpenAI-compatible model credentials: configured")
    print(f"MDL manifest: {MDL}")


def runtime_process(pid: int):
    import psutil

    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        command = " ".join(process.cmdline())
    except psutil.Error:
        return None
    if (
        "dbgpt_app.dbgpt_server" not in command
        and "dbgpt.model.cluster.controller.controller" not in command
    ):
        return None
    return process


def is_running(pid: int) -> bool:
    return runtime_process(pid) is not None


def read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def read_controller_pid() -> int | None:
    if not CONTROLLER_PID_FILE.exists():
        return None
    try:
        return int(CONTROLLER_PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def status(_: argparse.Namespace) -> None:
    pid = read_pid()
    if pid and is_running(pid):
        print(f"running pid={pid} url={service_url('web', 18765)}")
        return
    print("stopped")


def start(args: argparse.Namespace) -> None:
    setup(args)
    pid = read_pid()
    if pid and is_running(pid):
        print(f"already running pid={pid} url={service_url('web', 18765)}")
        return
    env = runtime_env()
    with CONFIG.open("rb") as stream:
        config = tomllib.load(stream)
    if env["DBGPT_MDL_ONLY"] != "1" and config["service"]["web"].get("light"):
        controller_command = [
            str(PYTHON),
            "-m",
            "dbgpt.model.cluster.controller.controller",
            "-c",
            str(CONFIG),
        ]
        CONTROLLER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        controller_log = CONTROLLER_LOG_FILE.open("ab")
        controller = subprocess.Popen(
            controller_command,
            cwd=str(ROOT),
            env=env,
            stdout=controller_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        CONTROLLER_PID_FILE.write_text(str(controller.pid), encoding="utf-8")
        wait_for_controller(args.wait or 30, controller.pid)

    command = [
        str(PYTHON),
        "-c",
        (
            "from dbgpt_app.dbgpt_server import run_webserver; "
            f"run_webserver({str(CONFIG)!r})"
        ),
    ]
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("ab")
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    if args.wait:
        try:
            wait_until_ready(timeout=args.wait, pid=process.pid)
        except Exception:
            if process.poll() is not None:
                PID_FILE.unlink(missing_ok=True)
            raise
    print(f"started pid={process.pid} url={service_url('web', 18765)} log={LOG_FILE}")


def stop(_: argparse.Namespace) -> None:
    pid = read_pid()
    if pid:
        process = runtime_process(pid)
        if process:
            os.kill(pid, signal.SIGTERM)
            for _ in range(40):
                if not is_running(pid):
                    break
                time.sleep(0.25)
        PID_FILE.unlink(missing_ok=True)
    controller_pid = read_controller_pid()
    if controller_pid:
        controller = runtime_process(controller_pid)
        if controller:
            os.kill(controller_pid, signal.SIGTERM)
            for _ in range(40):
                if not is_running(controller_pid):
                    break
                time.sleep(0.25)
        CONTROLLER_PID_FILE.unlink(missing_ok=True)
    print("stopped")


def wait_for_controller(timeout: int, pid: int) -> None:
    import urllib.request

    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if not is_running(pid):
            break
        try:
            with urllib.request.urlopen(
                f"{service_url('model.controller', 18766)}/api/health", timeout=2
            ) as response:
                if response.status < 500:
                    return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(1)
    log_tail = ""
    if CONTROLLER_LOG_FILE.exists():
        log_tail = "\n".join(
            CONTROLLER_LOG_FILE.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[-40:]
        )
    raise RuntimeError(
        f"DB-GPT model controller did not become ready: {last_error}\n{log_tail}"
    )


def wait_until_ready(timeout: int, pid: int) -> None:
    import urllib.request

    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if not is_running(pid):
            log_tail = ""
            if LOG_FILE.exists():
                log_tail = "\n".join(
                    LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[
                        -40:
                    ]
                )
            raise RuntimeError(
                f"DB-GPT MDL server exited before readiness check (pid={pid}).\n"
                f"Log tail:\n{log_tail}"
            )
        try:
            with urllib.request.urlopen(
                f"{service_url('web', 18765)}/api/v1/mdl/session", timeout=2
            ) as response:
                if response.status < 500:
                    time.sleep(0.5)
                    if is_running(pid):
                        return
                    break
        except Exception as exc:  # pragma: no cover - runtime helper
            last_error = str(exc)
        time.sleep(1)
    if not is_running(pid):
        log_tail = ""
        if LOG_FILE.exists():
            log_tail = "\n".join(
                LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[
                    -40:
                ]
            )
        raise RuntimeError(
            f"DB-GPT MDL server exited during readiness check (pid={pid}).\n"
            f"Log tail:\n{log_tail}"
        )
    raise RuntimeError(f"DB-GPT MDL server did not become ready: {last_error}")


def smoke(_: argparse.Namespace) -> None:
    env = runtime_env()
    script = """
import os
from dbgpt_app.openapi.api_v1.mdlint import MdlQueryService
import duckdb

conn = duckdb.connect(os.environ["DBGPT_MDL_SMOKE_DB"], read_only=True)
service = MdlQueryService()

def execute(sql, user_id, role):
    plan = service.plan_authorized_sql(
        sql,
        data_source="duckdb",
        user_id=user_id,
        role=role,
    )
    cursor = conn.execute(plan.planned_sql)
    return plan, [item[0] for item in cursor.description], cursor.fetchall()

admin_plan, admin_columns, admin_rows = execute(
    "SELECT SUM(amount) AS revenue FROM orders WHERE status = 'paid'",
    "admin",
    "admin",
)
analyst_plan, analyst_columns, analyst_rows = execute(
    "SELECT SUM(amount) AS revenue FROM orders WHERE status = 'paid'",
    "analyst",
    "analyst",
)
assert admin_columns == ["revenue"], admin_columns
assert analyst_columns == ["revenue"], analyst_columns
assert admin_rows == [(5200.0,)], admin_rows
assert analyst_rows == [(4300.0,)], analyst_rows

try:
    execute("SELECT SUM(salary) FROM secret_payroll", "analyst", "analyst")
except Exception as exc:
    assert "model" in str(exc).lower() or "table" in str(exc).lower(), exc
else:
    raise AssertionError("secret_payroll should be blocked by the MDL manifest")

print(
    "smoke ok: "
    f"admin={admin_rows[0][0]:g}, analyst={analyst_rows[0][0]:g}, "
    f"admin_sql={admin_plan.planned_sql}, analyst_sql={analyst_plan.planned_sql}"
)
"""
    # DuckDB permits a single writing process. Verify an isolated demo copy
    # so smoke remains usable while the DB-GPT server owns the source file.
    with tempfile.TemporaryDirectory(prefix="dbgpt-mdl-smoke-") as directory:
        smoke_db = Path(directory) / "demo.duckdb"
        shutil.copyfile(DEMO_DB, smoke_db)
        env["DBGPT_MDL_SMOKE_DB"] = str(smoke_db)
        subprocess.run([str(PYTHON), "-c", script], cwd=str(ROOT), env=env, check=True)


def tail(args: argparse.Namespace) -> None:
    lines = args.lines
    if not LOG_FILE.exists():
        print(f"log not found: {LOG_FILE}")
        return
    print(
        "\n".join(
            LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True)
    for name, func in (
        ("setup", setup),
        ("status", status),
        ("stop", stop),
        ("smoke", smoke),
    ):
        cmd = sub.add_parser(name)
        cmd.set_defaults(func=func)
    start_cmd = sub.add_parser("start")
    start_cmd.add_argument(
        "--wait", type=int, default=0, help="seconds to wait for HTTP readiness"
    )
    start_cmd.set_defaults(func=start)
    tail_cmd = sub.add_parser("tail")
    tail_cmd.add_argument("-n", "--lines", type=int, default=80)
    tail_cmd.set_defaults(func=tail)
    return parser


def main(argv: Iterable[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
