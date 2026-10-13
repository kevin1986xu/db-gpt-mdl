"""Exercise real HTTP identity, Wren planning, and database execution together."""

import json
from pathlib import Path

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dbgpt_app.openapi.api_v1.mdlint import endpoints
from dbgpt_app.openapi.api_v1.mdlint.auth import (
    MDLIdentityMiddleware,
    initialize_credentials,
)
from dbgpt_app.openapi.api_v1.mdlint.connector import wrap_connector


class RawConnector:
    db_type = "duckdb"
    dialect = "duckdb"

    def __init__(self, path):
        self.path = path

    def query_ex(self, sql, params=None, fetch="all", timeout=None):
        with duckdb.connect(str(self.path), read_only=True) as db:
            cursor = db.execute(sql, params or {})
            return [c[0] for c in cursor.description], cursor.fetchall()

    def get_table_names(self):
        return ["raw_customers", "raw_orders", "secret_payroll"]

    def get_columns(self, table):
        with duckdb.connect(str(self.path), read_only=True) as db:
            rows = db.execute(
                "SELECT column_name,data_type FROM information_schema.columns "
                "WHERE table_name=? ORDER BY ordinal_position",
                [table],
            ).fetchall()
            return [{"name": n, "type": t} for n, t in rows]


@pytest.fixture
def clients(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    mdl = tmp_path / "mdl.json"
    mdl.write_text((root / "configs/mdl.example.json").read_text())
    database = tmp_path / "demo.duckdb"
    with duckdb.connect(str(database)) as db:
        db.execute(
            "CREATE TABLE raw_customers(id INT, name VARCHAR, "
            "region VARCHAR, email VARCHAR)"
        )
        db.execute(
            "INSERT INTO raw_customers VALUES (1,'星河科技','华东','a@test'),"
            "(2,'青山零售','华南','b@test'),(3,'云帆贸易','华东','c@test')"
        )
        db.execute(
            "CREATE TABLE raw_orders(id INT, customer_id INT, amount DOUBLE, "
            "cost DOUBLE, status VARCHAR, region VARCHAR)"
        )
        db.execute(
            "INSERT INTO raw_orders VALUES (1,1,1200,700,'paid','华东'),"
            "(2,1,1800,1000,'paid','华东'),(3,2,900,500,'paid','华南'),"
            "(4,3,1300,800,'paid','华东'),(5,3,500,300,'cancelled','华东')"
        )
        db.execute("CREATE TABLE secret_payroll(employee VARCHAR, salary DOUBLE)")
        db.execute("INSERT INTO secret_payroll VALUES ('x',20000)")
    monkeypatch.setenv("DBGPT_MDL_ENABLED", "1")
    monkeypatch.setenv("DBGPT_MDL_PATH", str(mdl))
    monkeypatch.setenv("DBGPT_MDL_DATABASE", "mdl_demo")
    monkeypatch.setenv("DBGPT_MDL_CREDENTIALS", str(tmp_path / "credentials.json"))
    raw = RawConnector(database)

    class Manager:
        def _get_raw_connector(self, name):
            return raw

        def get_connector(self, name):
            return wrap_connector(raw, name)

    monkeypatch.setattr(endpoints, "manager", lambda: Manager())
    app = FastAPI()
    app.add_middleware(MDLIdentityMiddleware)
    app.include_router(endpoints.router, prefix="/api")
    tokens = json.loads(initialize_credentials().read_text())
    with TestClient(app) as admin, TestClient(app) as analyst:
        for client, name in [(admin, "admin"), (analyst, "analyst")]:
            assert (
                client.post(
                    "/api/v1/mdl/login", json={"token": tokens[name]}
                ).status_code
                == 200
            )
        yield admin, analyst, mdl


def query(client, sql):
    return client.post("/api/v1/mdl/query", json={"sql": sql})


def test_actual_wren_plans_join_and_row_policy(clients):
    admin, analyst, _ = clients
    sql = "SELECT SUM(amount) AS revenue FROM orders WHERE status='paid'"
    assert query(admin, sql).json()["data"]["rows"] == [[5200.0]]
    assert query(analyst, sql).json()["data"]["rows"] == [[4300.0]]
    joined = query(
        analyst,
        "SELECT c.name, SUM(o.amount) AS revenue FROM orders o "
        "JOIN customers c ON o.customer_id=c.id WHERE o.status='paid' "
        "GROUP BY c.name ORDER BY revenue DESC",
    )
    assert joined.status_code == 200, joined.text
    assert joined.json()["data"]["rows"] == [["星河科技", 3000.0], ["云帆贸易", 1300.0]]
    relation = query(
        analyst,
        "SELECT customer_name, SUM(amount) AS revenue FROM orders "
        "WHERE status='paid' GROUP BY customer_name ORDER BY revenue DESC",
    )
    assert relation.status_code == 200, relation.text
    assert relation.json()["data"]["rows"] == joined.json()["data"]["rows"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM secret_payroll",
        "SELECT * FROM raw_orders",
        "SELECT email FROM customers",
        "SELECT cost FROM orders",
        "SELECT id FROM orders WHERE cost > 0",
        "SELECT SUM(amount) FROM orders HAVING SUM(cost)>0",
        "SELECT id FROM orders UNION ALL SELECT salary FROM secret_payroll",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM information_schema.tables",
        "DELETE FROM orders",
        "SELECT 1; SELECT * FROM secret_payroll",
    ],
)
def test_rejects_bypasses(clients, sql):
    _, analyst, _ = clients
    response = query(analyst, sql)
    assert response.status_code == 400, response.text


def test_identity_cannot_be_forged_and_configuration_is_admin_only(clients):
    admin, analyst, _ = clients
    analyst.headers["user-id"] = "admin"
    response = query(analyst, "SELECT SUM(amount) FROM orders WHERE status='paid'")
    assert response.json()["data"]["rows"] == [[4300.0]]
    assert analyst.get("/api/v1/mdl").status_code == 403
    assert analyst.put("/api/v1/mdl/policies", json={"policies": []}).status_code == 403
    assert analyst.post("/api/v1/chat/db/add", json={}).status_code == 403
    assert admin.get("/api/v1/mdl").status_code == 200
    analyst.post("/api/v1/mdl/logout")
    assert query(analyst, "SELECT amount FROM orders").status_code == 401


def test_policy_updates_apply_to_subsequent_requests(clients):
    admin, analyst, _ = clients
    current = admin.get("/api/v1/mdl/policies").json()["data"]
    for grant in current["policies"][0]["models"].values():
        grant["filters"][0]["value"] = "华南"
    response = admin.put("/api/v1/mdl/policies", json=current)
    assert response.status_code == 200, response.text
    assert query(analyst, "SELECT SUM(amount) FROM orders WHERE status='paid'").json()[
        "data"
    ]["rows"] == [[900.0]]


def test_revision_and_invalid_models_do_not_overwrite_saved_state(clients):
    admin, _, mdl = clients
    before = mdl.read_text()
    document = admin.get("/api/v1/mdl").json()["data"]
    response = admin.put(
        "/api/v1/mdl",
        json={"manifest": document["manifest"], "revision": document["revision"] + 1},
    )
    assert response.status_code == 409, response.text
    assert mdl.read_text() == before
    document["manifest"]["models"][0]["tableReference"]["table"] = "missing"
    response = admin.put(
        "/api/v1/mdl",
        json={"manifest": document["manifest"], "revision": document["revision"]},
    )
    assert response.status_code == 400
    assert mdl.read_text() == before


def test_manifest_replacement_prunes_stale_policy_models(clients):
    admin, _, _ = clients
    current = admin.get("/api/v1/mdl").json()["data"]
    response = admin.put(
        "/api/v1/mdl",
        json={
            "revision": current["revision"],
            "database": "mdl_demo",
            "manifest": {
                "catalog": "wren",
                "schema": "main",
                "models": [
                    {
                        "name": "raw_orders_model",
                        "table": "raw_orders",
                        "columns": [
                            {"name": "id", "type": "integer"},
                            {"name": "amount", "type": "double"},
                        ],
                    }
                ],
                "relationships": [],
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["policies"][0]["models"] == {}


def test_disabled_policy_and_legacy_agent_routes_fail_closed(clients):
    admin, analyst, _ = clients
    current = admin.get("/api/v1/mdl/policies").json()["data"]
    current["policies"][0]["enabled"] = False
    response = admin.put("/api/v1/mdl/policies", json=current)
    assert response.status_code == 200, response.text
    assert analyst.get("/api/v1/mdl/schema").json()["data"]["models"] == []
    assert query(analyst, "SELECT amount FROM orders").status_code == 400
    for path in ("/api/v1/chat/completions", "/api/v1/chat/prepare"):
        assert analyst.post(path, json={"chat_mode": "chat_agent"}).status_code == 403


def test_schema_hides_physical_names_and_denied_columns(clients):
    _, analyst, _ = clients
    response = analyst.get("/api/v1/mdl/schema")
    assert response.status_code == 200
    data = response.json()["data"]
    assert "tables" not in data
    text = json.dumps(data)
    for hidden in ("raw_orders", "secret_payroll", "email", "cost", "refSql"):
        assert hidden not in text
