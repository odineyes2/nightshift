"""잡 종료 알림·관리자 API 회귀 검사. 임시 DB와 모의 전송만 사용한다."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")
import app as A
import db
import notify as N
from fastapi import HTTPException


def req(role="admin", body=None):
    async def read():
        return json.dumps(body or {}).encode()
    return SimpleNamespace(state=SimpleNamespace(user={"id": 1, "role": role}), body=read)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM meta WHERE key='notify_settings'")
        N._finished.clear()
        N.save_settings({"enabled": True, "topic": "test_topic"})

    def test_api_validation_and_token(self):
        for fn in (A.get_notification_settings, A.put_notification_settings, A.test_notification_settings):
            with self.assertRaises(HTTPException) as error:
                result = fn(req("user"))
                if asyncio.iscoroutine(result):
                    asyncio.run(result)
            self.assertEqual(error.exception.status_code, 403)
        for body in ({"mode": "bad"}, {"topic": "bad/topic"}, {"topic": ""},
                     {"server": "ftp://ntfy.sh"}, {"server": "https://ntfy.sh/?x=y"},
                     {"server": "https://ntfy.sh\n"}, {"enabled": 1}, {"token": "a\nb"}):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(A.put_notification_settings(req(body=body)))
            self.assertEqual(error.exception.status_code, 400)
        asyncio.run(A.put_notification_settings(req(body={"token": "secret"})))
        out = A.get_notification_settings(req())
        self.assertTrue(out["token_set"])
        self.assertNotIn("secret", json.dumps(out))
        asyncio.run(A.put_notification_settings(req(body={"token": ""})))
        self.assertEqual(N.get_settings()["token"], "secret")
        asyncio.run(A.put_notification_settings(req(body={"clear_token": True})))
        self.assertFalse(A.get_notification_settings(req())["token_set"])

    def test_each_and_disabled(self):
        N.save_settings({"mode": "each"})
        job = {"id": "j1", "name": "작업", "started_at": "2026-10-08T00:00:00+00:00",
               "finished_at": "2026-10-08T00:00:12+00:00", "options": {"prompt": "private"}}
        with patch.object(N, "send_async") as send:
            for status in ("done", "failed", "interrupted"):
                N.job_finished({**job, "status": status}, {})
            self.assertEqual(send.call_count, 2)
            self.assertIn("12초", send.call_args.args[2])
            self.assertNotIn("private", send.call_args.args[2])
            N.save_settings({"enabled": False, "topic": ""})
            N.job_finished({**job, "status": "done"}, {})
            self.assertEqual(send.call_count, 2)

    def test_all_concurrent_completions(self):
        jobs = {"a": {"status": "running"}, "b": {"status": "queued"},
                "pending": {"status": "pending"}, "deleted": {"status": "running", "deleted": True}}
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        def finish(key, status):
            barrier.wait()
            with lock:
                jobs[key]["status"] = status
                N.job_finished(jobs[key], jobs)
        with patch.object(N, "send_async") as send:
            threads = [threading.Thread(target=finish, args=("a", "done")),
                       threading.Thread(target=finish, args=("b", "failed"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(send.call_count, 2)  # 모두 끝난 뒤 잡별 전송
            self.assertEqual({c.args[1] for c in send.call_args_list}, {"잡 완료", "잡 실패"})
            N.job_finished({"status": "done"}, {})
            self.assertEqual(send.call_count, 3)
            self.assertIsNone(send.call_args.args[3])  # id 없으면 링크 없음

    def test_click_url(self):
        N.save_settings({"mode": "each"})
        with patch.object(N, "send_async") as send, patch.dict(os.environ, {"NIGHTSHIFT_PUBLIC_URL": "https://x.test/"}):
            N.job_finished({"id": "a b/#1", "status": "failed"}, {})
            self.assertEqual(send.call_args.args[3], "https://x.test/#job/a%20b%2F%231")
        os.environ.pop("NIGHTSHIFT_PUBLIC_URL", None)
        self.assertEqual(N._click_url("j1"), "https://nightshift.lomebrote.com/#job/j1")
        with patch.object(N.urllib.request, "urlopen", return_value=MagicMock()) as opened:
            N.send(N.get_settings(), "잡 완료", "m", "https://x.test/#job/j1")
            self.assertEqual(opened.call_args.args[0].get_header("Click"), "https://x.test/#job/j1")
            N.send(N.get_settings(), "시험 알림", "m")
            self.assertIsNone(opened.call_args.args[0].get_header("Click"))

    def test_transport_and_test_api(self):
        N.save_settings({"token": "secret", "enabled": False})
        with patch.object(N.urllib.request, "urlopen", return_value=MagicMock()) as opened:
            out = asyncio.run(A.test_notification_settings(req()))
            self.assertTrue(out["ok"])
            request = opened.call_args.args[0]
            self.assertEqual(request.full_url, "https://ntfy.sh/test_topic")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
            self.assertTrue(request.get_header("Title").startswith("=?utf-8?"))
            self.assertIn("시험", request.data.decode())
            self.assertEqual(opened.call_args.kwargs["timeout"], 10)
        with patch.object(N.urllib.request, "urlopen", side_effect=RuntimeError("secret")) as opened:
            out = N.send(N.get_settings(), "잡 완료", "메시지")
            self.assertFalse(out["ok"])
            self.assertNotIn("secret", out["error"])
            self.assertEqual(opened.call_count, 2)
        with patch.object(N, "get_settings", side_effect=RuntimeError("secret")):
            N.job_finished({"status": "done"}, {})
        with patch.object(N.threading, "Thread") as thread:
            N.send_async(N.get_settings(), "잡 완료", "본문")
            self.assertTrue(thread.call_args.kwargs["daemon"])
            thread.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
