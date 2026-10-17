"""NS-80-1 무드보드 이전과 보드별 HTTP 권한·복제·삭제를 격리 DB에서 검사한다."""
import os
import secrets
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", secrets.token_urlsafe(24))
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import db

all_migrations = db.MIGRATIONS
db.MIGRATIONS = [m for m in all_migrations if m[0] <= 27]
db.init()
with db.connect() as conn:
    conn.execute("INSERT INTO projects(id,name,description,defaults_json,created_at,updated_at) "
                 "VALUES(900,'이전 프로젝트','','{}','x','x')")
    for node_id in (901, 902):
        conn.execute("INSERT INTO board_nodes(id,project_id,kind,text,created_at,updated_at) "
                     "VALUES(?,900,'text','이전 카드','x','x')", (node_id,))
    conn.execute("INSERT INTO board_edges(id,project_id,from_node_id,to_node_id,created_at) "
                 "VALUES(903,900,901,902,'x')")
db.MIGRATIONS = all_migrations
db._initialized = False
db.init()

import app as A
from fastapi.testclient import TestClient


class ProjectBoardHTTPTests(unittest.TestCase):
    def client(self, name):
        password = secrets.token_urlsafe(18)
        user = A.auth.register(name, f"{name}@example.test", password)
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (user["id"],))
        client = TestClient(A.app)
        self.addCleanup(client.close)
        response = client.post("/api/auth/login", json={"username": name, "password": password},
                               headers={"X-Requested-With": "nightshift"})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def request(self, client, method, path, **kwargs):
        response = getattr(client, method)(path, headers={"X-Requested-With": "nightshift"}, **kwargs)
        self.assertLess(response.status_code, 500, response.text)
        return response

    def test_migration_and_foreign_keys(self):
        with db.connect() as conn:
            board = conn.execute("SELECT id FROM boards WHERE project_id=900").fetchone()[0]
            self.assertEqual(conn.execute("SELECT board_id FROM board_nodes WHERE id=901").fetchone()[0], board)
            self.assertEqual(conn.execute("SELECT board_id FROM board_edges WHERE id=903").fetchone()[0], board)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            other = conn.execute("INSERT INTO boards(project_id,name,position,created_at) "
                                 "VALUES(900,'다른 보드',1,'x')").lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO board_edges(project_id,board_id,from_node_id,to_node_id,created_at) "
                             "VALUES(900,?,901,902,'x')", (other,))

    def test_board_http_lifecycle_and_scope(self):
        owner = self.client("board-owner")
        other = self.client("board-other")
        self.assertEqual(TestClient(A.app).get("/api/projects/900/boards").status_code, 401)
        project = self.request(owner, "post", "/api/projects", json={"name": "Board project"}).json()
        base = f"/api/projects/{project['id']}"
        first = self.request(owner, "get", base + "/boards").json()["boards"][0]
        self.assertEqual(self.request(other, "get", base + "/boards").status_code, 404)
        second = self.request(owner, "post", base + "/boards", json={"name": "Second"}).json()
        other_project = self.request(owner, "post", "/api/projects", json={"name": "Elsewhere"}).json()
        self.assertEqual(self.request(owner, "get", f"/api/projects/{other_project['id']}/board?board_id={second['id']}").status_code, 404)
        self.assertEqual(self.request(other, "post", base + f"/boards/{second['id']}/duplicate",
                                      json={"name": "Stolen"}).status_code, 404)
        self.assertEqual(self.request(owner, "post", base + "/boards", json={"name": "second"}).status_code, 409)
        self.assertEqual(self.request(owner, "post", base + "/boards", json={"name": "  "}).status_code, 400)
        a = self.request(owner, "post", base + f"/board/nodes?board_id={first['id']}",
                         json={"kind": "text", "text": "A"}).json()
        b = self.request(owner, "post", base + f"/board/nodes?board_id={second['id']}",
                         json={"kind": "text", "text": "B"}).json()
        c = self.request(owner, "post", base + f"/board/nodes?board_id={second['id']}",
                         json={"kind": "text", "text": "C"}).json()
        frame = self.request(owner, "post", base + f"/board/nodes?board_id={second['id']}",
                             json={"kind": "frame", "text": "Group"}).json()
        self.assertEqual(self.request(owner, "patch", base + f"/board/nodes/{frame['id']}?board_id={second['id']}",
                                      json={"collapsed": True, "members": [b["id"], c["id"]]}).status_code, 200)
        self.assertEqual(self.request(owner, "patch", base + f"/board/nodes/{frame['id']}?board_id={second['id']}",
                                      json={"collapsed": True, "members": [a["id"]]}).status_code, 404)
        self.assertEqual(self.request(owner, "post", base + f"/board/edges?board_id={second['id']}",
                                      json={"from_node_id": b["id"], "to_node_id": c["id"]}).status_code, 200)
        self.assertEqual(len(self.request(owner, "get", base + "/board").json()["nodes"]), 1)
        self.assertEqual(self.request(owner, "post", base + f"/board/edges?board_id={first['id']}",
                                      json={"from_node_id": a["id"], "to_node_id": b["id"]}).status_code, 404)
        self.assertEqual(self.request(owner, "patch", base + f"/board/nodes/{b['id']}?board_id={first['id']}",
                                      json={"text": "wrong"}).status_code, 404)
        self.assertEqual(self.request(owner, "post", base + f"/board/restore?board_id={first['id']}",
                                      json={"nodes": [], "edges": [{"from_node_id": a["id"],
                                                                         "to_node_id": b["id"]}]}).status_code, 404)
        self.assertEqual(self.request(other, "get", base + f"/board?board_id={second['id']}").status_code, 404)
        duplicate = self.request(owner, "post", base + f"/boards/{second['id']}/duplicate",
                                 json={"name": "Copy"}).json()
        copy = self.request(owner, "get", base + f"/board?board_id={duplicate['id']}").json()
        copy_nodes = copy["nodes"]
        self.assertEqual([n["text"] for n in copy_nodes], ["B", "C", "Group"])
        self.assertNotEqual(copy_nodes[0]["id"], b["id"])
        self.assertEqual(set(copy_nodes[2]["data"]["members"]), {copy_nodes[0]["id"], copy_nodes[1]["id"]})
        self.assertEqual({copy["edges"][0]["from_node_id"], copy["edges"][0]["to_node_id"]},
                         {copy_nodes[0]["id"], copy_nodes[1]["id"]})
        self.assertEqual(self.request(owner, "delete", base + f"/boards/{second['id']}").status_code, 400)
        self.assertEqual(self.request(owner, "delete", base + f"/boards/{second['id']}?confirm=true").status_code, 200)
        self.assertEqual(self.request(owner, "get", base + f"/board?board_id={second['id']}").status_code, 404)
        self.assertEqual(self.request(owner, "get", base + f"/board?board_id={duplicate['id']}").status_code, 200)
        self.assertEqual(self.request(owner, "delete", base + f"/boards/{first['id']}?confirm=true").status_code, 200)
        self.assertEqual(self.request(owner, "delete", base + f"/boards/{duplicate['id']}?confirm=true").status_code, 200)
        self.assertEqual(self.request(owner, "get", base + "/boards").json(), {"boards": []})
        self.assertEqual(self.request(owner, "get", base + "/board").status_code, 404)


if __name__ == "__main__":
    unittest.main()
