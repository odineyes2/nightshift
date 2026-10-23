"""NS-80-1 무드보드 이전과 보드별 HTTP 권한·복제·삭제를 격리 DB에서 검사한다."""
import os
import secrets
import sqlite3
import sys
import tempfile
import unittest
import io
from unittest.mock import AsyncMock, patch
from pathlib import Path
from PIL import Image

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

    def test_draft_gen_text_slot_and_execution(self):
        owner = self.client("draft-gen-owner")
        project = self.request(owner, "post", "/api/projects", json={"name": "Draft gen"}).json()
        base = f"/api/projects/{project['id']}/board"
        draft = {"template_id": "seed_batch", "workflow": {},
                 "options": {"main_prompt": "카드 기본 글"}, "exposed": ["main_prompt"]}
        self.assertEqual(self.request(owner, "post", base + "/nodes",
                                      json={"kind": "gen", "draft": {**draft, "workflow": None}}).status_code, 400)
        response = self.request(owner, "post", base + "/nodes", json={"kind": "gen", "draft": draft})
        self.assertEqual(response.status_code, 200, response.text)
        gen = response.json()
        self.assertIsNone(gen["data"]["preset_id"])
        self.assertEqual(gen["data"]["preset_name"], "시드 반복")
        slots = {slot["name"]: slot for slot in gen["data"]["slots"]}
        self.assertEqual(slots["main_prompt"]["kind"], "text")
        self.assertFalse(slots["main_prompt"]["required"])
        self.assertNotIn("kind", slots["pose_image"])
        memo = self.request(owner, "post", base + "/nodes",
                            json={"kind": "text", "text": "연결한 글"}).json()
        image = self.request(owner, "post", base + "/nodes", json={"kind": "image"}).json()
        edge = lambda source, target, slot: self.request(
            owner, "post", base + "/edges",
            json={"from_node_id": source["id"], "to_node_id": target["id"], "to_slot": slot})
        self.assertEqual(edge(image, gen, "main_prompt").status_code, 400)
        self.assertEqual(edge(memo, gen, "pose_image").status_code, 400)
        self.assertEqual(self.request(owner, "post", base + "/restore", json={"nodes": [], "edges": [
            {"from_node_id": image["id"], "to_node_id": gen["id"], "to_slot": "main_prompt"}]}).status_code, 400)
        self.assertEqual(self.request(owner, "post", base + f"/nodes/{gen['id']}/prepare").json()["options"]["main_prompt"],
                         "카드 기본 글")
        self.assertEqual(edge(memo, gen, "main_prompt").status_code, 200)
        self.assertEqual(self.request(owner, "post", base + f"/nodes/{gen['id']}/prepare").json()["options"]["main_prompt"],
                         "연결한 글")
        with patch.object(A, "create_job", new_callable=AsyncMock) as create_job:
            create_job.return_value = {"id": "draft-test-job", "status": "pending"}
            run = self.request(owner, "post", base + f"/nodes/{gen['id']}/run")
            self.assertEqual(run.status_code, 200, run.text)
            self.assertEqual(create_job.await_args.args[5]["main_prompt"], "연결한 글")

        image_gen = self.request(owner, "post", base + "/nodes", json={"kind": "gen", "draft": {
            "template_id": "input_image_batch", "workflow": {}, "slots": ["input_image"]}}).json()
        self.assertEqual(edge(memo, image_gen, "input_image").status_code, 400)
        self.assertEqual(edge(image, image_gen, "input_image").status_code, 200)

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

    def test_empty_media_cards_and_fill_scope(self):
        owner = self.client("empty-media-owner")
        other = self.client("empty-media-other")
        project = self.request(owner, "post", "/api/projects", json={"name": "Empty media"}).json()
        base = f"/api/projects/{project['id']}/board"
        self.assertEqual(self.request(TestClient(A.app), "post", base + "/nodes", json={"kind": "image"}).status_code, 401)
        cards = [self.request(owner, "post", base + "/nodes", json={"kind": kind}).json()
                 for kind in ("image", "video")]
        self.assertEqual([card["asset_path"] for card in cards], [None, None])
        self.assertEqual(self.request(owner, "get", base).json()["nodes"][0]["asset_path"], None)
        self.assertEqual(self.request(other, "patch", base + f"/nodes/{cards[0]['id']}",
                                      json={"asset_path": None}).status_code, 404)
        png = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(png, format="PNG")
        upload = self.request(owner, "post", "/api/output-assets/upload",
                              files={"files": ("owned.png", png.getvalue(), "image/png")},
                              data={"project_id": str(project["id"])}).json()
        path = upload["paths"][0]
        other_project = self.request(other, "post", "/api/projects", json={"name": "Other media"}).json()
        foreign = self.request(other, "post", f"/api/projects/{other_project['id']}/board/nodes",
                               json={"kind": "image"}).json()
        self.assertEqual(self.request(other, "patch", f"/api/projects/{other_project['id']}/board/nodes/{foreign['id']}",
                                      json={"asset_path": path}).status_code, 404)
        filled = self.request(owner, "patch", base + f"/nodes/{cards[0]['id']}",
                              json={"asset_path": path}).json()
        self.assertEqual(filled["asset_path"], path)
        self.assertIsNone(self.request(owner, "patch", base + f"/nodes/{cards[0]['id']}",
                                       json={"asset_path": None}).json()["asset_path"])
        restored = self.request(owner, "post", base + "/restore", json={"nodes": [cards[0]], "edges": []})
        self.assertEqual(restored.status_code, 200, restored.text)

    def test_empty_cards_and_job_fill_scope(self):
        owner = self.client("empty-job-owner")
        other = self.client("empty-job-other")
        project = self.request(owner, "post", "/api/projects", json={"name": "Empty cards"}).json()
        foreign_project = self.request(other, "post", "/api/projects", json={"name": "Foreign cards"}).json()
        base = f"/api/projects/{project['id']}/board"
        first = self.request(owner, "get", f"/api/projects/{project['id']}/boards").json()["boards"][0]["id"]
        second = self.request(owner, "post", f"/api/projects/{project['id']}/boards",
                              json={"name": "Second empty board"}).json()["id"]
        cards = {}
        for kind in ("text", "image", "video", "job"):
            response = self.request(owner, "post", f"{base}/nodes?board_id={first}",
                                    json={"kind": kind, "text": "", "asset_path": None,
                                          "job_id": None, "x": 31, "y": 47})
            self.assertEqual(response.status_code, 200, response.text)
            cards[kind] = response.json()
            self.assertEqual((cards[kind]["x"], cards[kind]["y"], cards[kind]["text"]), (31, 47, ""))
        self.assertIsNone(cards["job"]["job_id"])
        self.assertIsNone(cards["image"]["asset_path"])
        self.assertIsNone(cards["video"]["asset_path"])
        listed = self.request(owner, "get", f"{base}?board_id={first}").json()["nodes"]
        self.assertEqual({card["id"] for card in listed}, {card["id"] for card in cards.values()})
        status = self.request(owner, "get", f"{base}/jobs?board_id={first}").json()["cards"]
        self.assertEqual(status[str(cards["job"]["id"])], {"job_id": None, "missing": False})
        with db.connect() as conn:
            owner_id = conn.execute("SELECT owner_id FROM projects WHERE id=?", (project["id"],)).fetchone()[0]
            other_id = conn.execute("SELECT owner_id FROM projects WHERE id=?", (foreign_project["id"],)).fetchone()[0]
        job_ids = ("empty-own-job", "empty-other-job", "empty-other-project-job")
        A.jobs[job_ids[0]] = {"id": job_ids[0], "owner_id": owner_id, "project_id": project["id"]}
        A.jobs[job_ids[1]] = {"id": job_ids[1], "owner_id": other_id, "project_id": foreign_project["id"]}
        A.jobs[job_ids[2]] = {"id": job_ids[2], "owner_id": owner_id, "project_id": foreign_project["id"]}
        try:
            with db.connect() as conn:
                for job_id in job_ids:
                    job = A.jobs[job_id]
                    conn.execute("INSERT INTO jobs(id,project_id,owner_id,data_json) VALUES(?,?,?,?)",
                                 (job_id, job["project_id"], job["owner_id"], "{}"))
            path = f"{base}/nodes/{cards['job']['id']}?board_id={first}"
            self.assertEqual(self.request(other, "patch", path, json={"job_id": job_ids[0]}).status_code, 404)
            self.assertEqual(self.request(owner, "patch", f"{base}/nodes/{cards['job']['id']}?board_id={second}",
                                          json={"job_id": job_ids[0]}).status_code, 404)
            self.assertEqual(self.request(owner, "patch", path, json={"job_id": job_ids[1]}).status_code, 404)
            self.assertEqual(self.request(owner, "patch", path, json={"job_id": job_ids[2]}).status_code, 404)
            self.assertEqual(self.request(owner, "post", f"{base}/nodes?board_id={first}",
                                          json={"kind": "job", "job_id": job_ids[2]}).status_code, 404)
            self.assertEqual(self.request(owner, "post", f"{base}/restore?board_id={first}",
                                          json={"nodes": [{**cards["job"], "job_id": job_ids[2]}],
                                                "edges": []}).status_code, 404)
            self.assertEqual(self.request(owner, "patch", f"{base}/nodes/{cards['text']['id']}?board_id={first}",
                                          json={"job_id": job_ids[0]}).status_code, 400)
            filled = self.request(owner, "patch", path, json={"job_id": job_ids[0]}).json()
            self.assertEqual(filled["job_id"], job_ids[0])
            self.assertIsNone(self.request(owner, "patch", path, json={"job_id": None}).json()["job_id"])
            restored = self.request(owner, "post", f"{base}/restore?board_id={first}",
                                    json={"nodes": [cards["job"]], "edges": []})
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertIsNone(restored.json()["nodes"][0]["job_id"])
        finally:
            for job_id in job_ids:
                A.jobs.pop(job_id, None)

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
