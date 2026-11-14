"""Asset provenance indexing, persistence and authenticated response coverage."""
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ('DATA', 'OUTPUT', 'ASSETS'):
    os.environ[f'NIGHTSHIFT_{key}_DIR'] = str(Path(TMP.name) / key.lower())
os.environ.setdefault('NIGHTSHIFT_ADMIN_USER', 'admin')
os.environ.setdefault('NIGHTSHIFT_ADMIN_PASSWORD', 'Test-Passw0rd-xyz!')
sys.path.insert(0, str(ROOT / 'server'))
os.chdir(ROOT / 'server')
import app as A
import db
from fastapi.testclient import TestClient
from PIL import Image

login_secret = secrets.token_urlsafe(16) + 'Aa1!'
FIELDS = ('compute_owner_id', 'billing', 'via_service_id')


class ProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.users = {}
        for name in ('prov-admin', 'prov-member', 'prov-bot'):
            user = A.auth.register(name, name + '@example.test', login_secret)
            cls.users[name] = user['id']
            with db.connect() as conn:
                conn.execute("UPDATE users SET status='active', role=? WHERE id=?",
                             ('admin' if name == 'prov-admin' else 'user', user['id']))

    def test_admin_gallery_filters(self):
        admin, member, bot = (self.users[n] for n in ('prov-admin', 'prov-member', 'prov-bot'))
        fixtures = {
            'self': (admin, admin, 'member', None),
            'paid': (member, admin, 'admin', bot),
            'member': (member, member, 'member', None),
            'via': (member, member, 'member', bot),
            'unknown': (bot, None, None, None),
        }
        paths = []
        for label, values in fixtures.items():
            for ext in ('png', 'mp4'):
                rel = f'filter-{label}/result.{ext}'
                path = Path(A.OUTPUT_DIR) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                if ext == 'png':
                    Image.new('RGB', (8, 8)).save(path)
                else:
                    path.write_bytes(b'fake video')
                A.assets_index.register_upload(rel, values[0], None)
                paths.append(rel)
                with db.connect() as conn:
                    conn.execute('UPDATE assets SET compute_owner_id=?, billing=?, via_service_id=? WHERE path=?',
                                 (*values[1:], rel))
        try:
            for username in ('prov-admin', 'prov-member'):
                client = TestClient(A.app)
                self.addCleanup(client.close)
                login = client.post('/api/auth/login', json=dict(zip(('username', 'password'), (username, login_secret))),
                                    headers={'X-Requested-With': 'nightshift'})
                self.assertEqual(login.status_code, 200, login.text)
                for kind, ext in (('images', 'png'), ('videos', 'mp4')):
                    def names(params):
                        response = client.get('/api/output-' + kind, params=params)
                        self.assertEqual(response.status_code, 200, response.text)
                        items = response.json()[kind]
                        for item in items:
                            if username == 'prov-admin':
                                self.assertTrue(set(FIELDS).issubset(item))
                            else:
                                self.assertTrue(set(FIELDS).isdisjoint(item))
                                self.assertEqual(item['owner_id'], member)
                        return {i['name'] for i in items}
                    def expected(*labels):
                        return {f'filter-{label}/result.{ext}' for label in labels}
                    if username == 'prov-admin':
                        self.assertEqual(names({}), expected(*fixtures))
                        self.assertEqual(names({'owner': 'me'}), expected('self'))
                        self.assertEqual(names({'owner': str(member)}), expected('paid', 'member', 'via'))
                        self.assertEqual(names({'source': 'paid'}), expected('paid'))
                        self.assertEqual(names({'source': 'member_gpu'}), expected('member', 'via'))
                        self.assertEqual(names({'source': 'via'}), expected('paid', 'via'))
                        self.assertEqual(names({'owner': 'me', 'source': 'paid'}), set())
                        self.assertEqual(names({'owner': str(member), 'source': 'via'}), expected('paid', 'via'))
                        self.assertEqual(names({'owner': '999999'}), set())
                        for params in ({'source': 'invalid'}, {'owner': 'bad'}, {'owner': '0'},
                                       {'owner': '-1'}, {'owner': '1.5'}, {'owner': '9' * 40}):
                            response = client.get('/api/output-' + kind, params=params)
                            self.assertEqual(response.status_code, 400, response.text)
                    else:
                        own = expected('paid', 'member', 'via')
                        self.assertEqual(names({'owner': str(admin), 'source': 'paid'}), own)
                        self.assertEqual(names({'owner': 'invalid', 'source': 'invalid'}), own)
        finally:
            with db.connect() as conn:
                conn.executemany('DELETE FROM assets WHERE path=?', [(p,) for p in paths])
            for rel in paths:
                (Path(A.OUTPUT_DIR) / rel).unlink(missing_ok=True)

    def test_provenance_lifecycle_and_responses(self):
        admin, member, bot = (self.users[n] for n in ('prov-admin', 'prov-member', 'prov-bot'))
        jobs = {
            'paid': dict(owner_id=member, pod_id='admin-pod', billing='admin', via_service=bot),
            'normal': dict(owner_id=member, pod_id='member-pod'),
            'via': dict(owner_id=member, pod_id='member-pod', via_service=bot),
            'fallback': dict(owner_id=member, pod_id='gone', billing='admin', via_service=999999),
            'unknown': dict(owner_id=member, pod_id='gone', via_service=999999),
        }
        db.save_jobs(jobs)
        A.jobs.update(jobs)
        expected = {
            'paid': (admin, 'admin', bot), 'normal': (member, 'member', None),
            'via': (member, 'member', bot), 'direct': (member, None, None),
            'fallback': (admin, 'admin', None), 'unknown': (None, 'member', None),
        }
        paths = {}
        for name in expected:
            for ext in ('png', 'mp4'):
                rel = f'{name}/result.{ext}'
                path = Path(A.OUTPUT_DIR) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                if ext == 'png':
                    Image.new('RGB', (8, 8)).save(path)
                else:
                    path.write_bytes(b'fake video')
                paths[rel] = name
        origins = {p: 'member-pod' for p in paths if p.startswith('direct/')}
        pod_owners = {'admin-pod': admin, 'member-pod': member}
        with mock.patch.object(A.assets_index, '_pod_owner', side_effect=pod_owners.get), \
             mock.patch.object(A.assets_index, '_origin_pods', return_value=origins), \
             mock.patch.object(A.assets_index.auth, 'admin_id', return_value=admin):
            A.assets_index.sync(force=True)
            def snapshot():
                with db.read() as conn:
                    return {r['path']: dict(r) for r in conn.execute('SELECT * FROM assets')}
            first = snapshot()
            owners = {p: r['owner_id'] for p, r in first.items()}
            self.assertEqual(set(owners.values()), {member})
            for p, name in paths.items():
                self.assertEqual(tuple(first[p][k] for k in FIELDS), expected[name])
            # Simulate pre-v33 rows, including an unknown job and a partially populated row.
            with db.connect() as conn:
                # A previously assigned owner must survive even when it differs from the job owner.
                conn.execute("UPDATE assets SET owner_id=? WHERE path='normal/result.mp4'", (admin,))
                owners['normal/result.mp4'] = admin
                conn.execute('UPDATE assets SET compute_owner_id=NULL, billing=NULL, via_service_id=NULL')
                conn.execute("UPDATE assets SET billing='admin' WHERE path='normal/result.png'")
                conn.execute('DELETE FROM meta WHERE key=?', (A.assets_index._BACKFILL_PROVENANCE_KEY,))
            A.assets_index.sync(force=True)
            filled = snapshot()
            self.assertEqual({p: r['owner_id'] for p, r in filled.items()}, owners)
            for p, name in paths.items():
                value = (None, None, None) if name == 'direct' else expected[name]
                if p == 'normal/result.png':
                    value = (None, 'admin', None)
                self.assertEqual(tuple(filled[p][k] for k in FIELDS), value)
            # A second sync does not refill rows after the one-time marker is set.
            with db.connect() as conn:
                conn.execute("UPDATE assets SET compute_owner_id=NULL, billing=NULL, via_service_id=NULL WHERE path='unknown/result.png'")
            A.assets_index.sync(force=True)
            self.assertIsNone(snapshot()['unknown/result.png']['billing'])
            before_delete = snapshot()
            db.save_jobs({})
            A.assets_index.sync(force=True)
            after_delete = snapshot()
            self.assertEqual({p: tuple(r[k] for k in FIELDS) for p, r in before_delete.items()},
                             {p: tuple(r[k] for k in FIELDS) for p, r in after_delete.items()})
            self.assertEqual({p: r['owner_id'] for p, r in after_delete.items()}, owners)

        for username in ('prov-member', 'prov-admin'):
            client = TestClient(A.app)  # No lifespan: no scheduler or external workers.
            self.addCleanup(client.close)
            response = client.post('/api/auth/login', json=dict(zip(('username', 'password'), (username, login_secret))),
                                   headers={'X-Requested-With': 'nightshift'})
            self.assertEqual(response.status_code, 200, response.text)
            for endpoint, key in (('/api/output-assets', 'assets'), ('/api/output-images', 'images'),
                                  ('/api/output-videos', 'videos')):
                response = client.get(endpoint)
                self.assertEqual(response.status_code, 200, response.text)
                items = response.json()[key]
                self.assertTrue(items)
                if username == 'prov-member':
                    for item in items:
                        self.assertTrue(set(FIELDS).isdisjoint(item))
            response = client.get('/api/output-assets/detail', params={'path': 'paid/result.png'})
            self.assertEqual(response.status_code, 200, response.text)
            detail = response.json()
            if username == 'prov-member':
                self.assertTrue(set(FIELDS).isdisjoint(detail))
            else:
                self.assertEqual(tuple(detail[k] for k in FIELDS), expected['paid'])

        # Uploading over an existing indexed path must clear inherited provenance.
        A.assets_index.register_upload('paid/result.png', member, None)
        with db.read() as conn:
            row = conn.execute("SELECT * FROM assets WHERE path='paid/result.png'").fetchone()
        self.assertEqual(tuple(row[k] for k in FIELDS), (None, None, None))
        self.assertEqual(row['owner_id'], member)
        self.assertIsNone(row['job_id'])


if __name__ == '__main__':
    unittest.main()

