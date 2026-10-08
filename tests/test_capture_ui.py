"""scripts/capture_ui.py 격리·계약 검사(NS-55-1).

모의 검사: route 변환·거절, 환경 허용 목록, 코드 선택 복사(.env·data·옛 루트 파일 제외),
네트워크 차단, lifespan off, 로그인 헤더, 부분 성공·실패 manifest, 프로세스 종료.
Playwright가 설치돼 있으면 #/와 #/gallery를 실제로 캡처해 네 장·활성 화면을 확인한다.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import capture_ui
import capture_ui_support as s


def test_routes():
    assert s.parse_route('#/') == ('#dashboard', 'dashboard', None, None)
    assert s.parse_route('#/gallery') == ('#gallery', 'gallery', None, None)
    assert s.parse_route('#/pod/abc/pmodels') == ('#pod/abc/pmodels', 'pmodels', 'abc', None)
    assert s.parse_route('#/project/3/prboard') == ('#project/3/prboard', 'prboard', None, 3)
    assert s.parse_route('#/project/unassigned/jobs')[3] == 'unassigned'
    for bad in ('', '#gallery', 'http://x/#/', '#/nope', '#/pod/abc', '#/pod/abc/lora', '#/project/x/jobs',
                '#/../x', '#/gallery/', '#//evil.com', '#/%2e%2e', '#\\gallery', '#/gallery?x=1',
                '#C:/Program Files/Git/gallery'):
        try:
            s.parse_route(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_child_environment():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        env = s.child_environment(d, {'Path': 'C:/bin', 'SystemRoot': 'C:/Windows', 'RUNPOD_API_KEY': 'secret-rp',
                                      'NIGHTSHIFT_DATA_DIR': 'C:/prod/data', 'NIGHTSHIFT_DB_PATH': 'C:/prod/db',
                                      'NIGHTSHIFT_COOKIE_DOMAIN': '.lomebrote.com', 'HTTPS_PROXY': 'http://p',
                                      'ANTHROPIC_API_KEY': 'secret-an', 'NIGHTSHIFT_MCP_KEY': 'secret-mcp',
                                      'NTFY_TOPIC': 'secret-ntfy', 'NIGHTSHIFT_INPUT_IMAGES_DIR': 'C:/prod/in'})
        assert env['PATH'] == 'C:/bin' and env['SYSTEMROOT'] == 'C:/Windows'
        assert 'secret' not in json.dumps(env) and 'prod' not in json.dumps(env)
        for key in ('NIGHTSHIFT_DATA_DIR', 'NIGHTSHIFT_DB_PATH', 'NIGHTSHIFT_OUTPUT_DIR', 'NIGHTSHIFT_ASSETS_DIR', 'TEMP', 'TMP'):
            assert Path(env[key]).resolve().is_relative_to(d.resolve()), key
        assert not any(k in env for k in ('NIGHTSHIFT_COOKIE_DOMAIN', 'HTTPS_PROXY', 'RUNPOD_API_KEY'))
        assert env['NIGHTSHIFT_ADMIN_USER'] == 'capture-admin' and len(env['NIGHTSHIFT_ADMIN_PASSWORD']) > 12


def test_copy_sources():
    with tempfile.TemporaryDirectory() as tmp:
        src, dest = Path(tmp) / 'src', Path(tmp) / 'dest'
        for rel in ('server/app.py', 'static/index.html', 'templates/manifest.json', '.env', 'server/.env',
                    'data/nightshift.db', 'jobs_state.json', 'pods.json', 'output/a.png', 'assets/b.png'):
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_text('x', encoding='utf-8')
        files = ['server/app.py', 'static/index.html', 'templates/manifest.json', '.env', 'server/.env',
                 'data/nightshift.db', 'jobs_state.json', 'pods.json', 'output/a.png', '../x', 'server/../.env']
        copied = s.copy_sources(src, files, dest)
        assert copied == ['server/app.py', 'static/index.html', 'templates/manifest.json'], copied
        assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob('*') if p.is_file()) == sorted(copied)
        assert (src / 'jobs_state.json').exists() and (src / 'pods.json').exists()   # 옛 루트 파일은 그대로
    # 실제 추적 목록도 세 폴더 밖을 담지 않는다
    assert all(p.split('/')[0] in s.COPY_DIRS for p in s.tracked_files(ROOT))


def test_uncommitted_sources():
    """커밋 전 새 서버 모듈도 복사하되 ignore 파일과 비밀 파일은 빠진다."""
    with tempfile.TemporaryDirectory() as tmp:
        root, dest = Path(tmp) / 'repo', Path(tmp) / 'copy'
        root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        for rel in ('server/app.py', 'server/lighting_library.py', 'server/.env',
                    'server/ignored.py', 'data/nightshift.db'):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('x', encoding='utf-8')
        (root / '.gitignore').write_text('server/ignored.py\n', encoding='utf-8')
        subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
        # 새 모듈을 untracked로 남겨 실제 Task의 커밋 전 상태를 재현한다.
        subprocess.run(['git', 'rm', '--cached', 'server/lighting_library.py'], cwd=root, check=True,
                       capture_output=True)
        copied = s.copy_sources(root, s.tracked_files(root), dest)
        assert copied == ['server/app.py', 'server/lighting_library.py'], copied


def test_network_guard():
    """자식 프로세스에서 허용 포트만 붙고 다른 localhost 포트·외부 이름은 막히는지 본다."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(4)
    other = socket.socket()
    other.bind(('127.0.0.1', 0))
    other.listen(4)
    code = f'''
import sys, socket, asyncio
sys.path.insert(0, {str(ROOT / "scripts")!r})
import capture_ui_support as s
s.install_network_guard({listener.getsockname()[1]})
socket.create_connection(("127.0.0.1", {listener.getsockname()[1]})).close()
for target in (("127.0.0.1", {other.getsockname()[1]}), ("127.0.0.1", 8000), ("1.1.1.1", 443)):
    try:
        socket.create_connection(target, timeout=2); print("LEAK", target); sys.exit(1)
    except OSError: pass
try:
    socket.getaddrinfo("api.runpod.io", 443); print("LEAK dns"); sys.exit(1)
except OSError: pass
asyncio.run(asyncio.sleep(0))   # Windows 이벤트 루프의 socketpair는 동작해야 한다
print("OK")
'''
    try:
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == 'OK', out.stdout + out.stderr
    finally:
        listener.close()
        other.close()


def test_bootstrap_contract():
    b = s.BOOTSTRAP
    assert b.index('install_network_guard') < b.index('import app')   # 앱 import 전에 차단
    assert 'lifespan="off"' in b and 'host="127.0.0.1"' in b
    assert 'scheduler' not in b and 'sync_runtimes' not in b


class FakeResponse:
    def __init__(self, status, body=None):
        self.status, self.ok, self._body = status, 200 <= status < 300, body or {}

    def json(self):
        return self._body


def test_login_headers_and_failures():
    sent = []
    env = {'NIGHTSHIFT_ADMIN_USER': 'u', 'NIGHTSHIFT_ADMIN_PASSWORD': 'p'}

    def ctx(status, role='admin'):
        req = SimpleNamespace(post=lambda url, headers, data: sent.append((url, headers, data)) or FakeResponse(status),
                              get=lambda url: FakeResponse(200, {'user': {'role': role}}))
        return SimpleNamespace(request=req)

    capture_ui.login(ctx(200), 'http://127.0.0.1:1', env)
    assert sent[0] == ('http://127.0.0.1:1/api/auth/login', {'x-requested-with': 'nightshift'}, {'username': 'u', 'password': 'p'})
    for status, role in ((403, 'admin'), (401, 'admin'), (200, 'user')):
        try:
            capture_ui.login(ctx(status, role), 'http://127.0.0.1:1', env)
        except RuntimeError:
            continue
        raise AssertionError(status)


def test_local_request():
    base = 'http://127.0.0.1:5000'
    assert s.local_request(base + '/js/bundle.js', base) and s.local_request('data:image/png;base64,', base)
    for url in ('http://127.0.0.1:8000/', 'https://fonts.googleapis.com/x', 'http://localhost:5000/'):
        assert not s.local_request(url, base), url


def test_manifest_status():
    cap = {'path': 'x'}
    assert s.manifest_status({'captures': [cap] * 4, 'errors': []}) == 'ok'
    assert s.manifest_status({'captures': [cap] * 3, 'errors': [{}]}) == 'partial'
    assert s.manifest_status({'captures': [], 'errors': [{}]}) == 'failed'


def test_stop_process_kills_after_timeout():
    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    calls = []
    real_wait = p.wait

    def wait(timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired('x', timeout)   # terminate가 안 먹은 경우
        return real_wait(timeout=timeout)
    p.wait = wait
    s.stop_process(p, timeout=5)
    assert p.poll() is not None and len(calls) == 2


def test_startup_failure_manifest():
    """서버가 바로 죽으면 failed manifest와 log_summary를 남기고 비밀번호는 가린다."""
    original = s.BOOTSTRAP
    capture_ui.BOOTSTRAP = 'import os, sys; print("boom", os.environ["NIGHTSHIFT_ADMIN_PASSWORD"]); sys.exit(3)'
    try:
        directory, manifest = capture_ui.capture('#/')
    finally:
        capture_ui.BOOTSTRAP = original
    saved = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    assert saved['status'] == 'failed' and saved['captures'] == [] and saved['fixture'] == 'empty'
    assert saved['errors'][0]['stage'] == 'startup' and 'boom ***' in saved['errors'][0]['log_summary'], saved
    assert directory.parent == ROOT / '.ui-captures'
    assert not (directory / 'src' / '.env').exists() and not (directory / 'src' / 'data').exists()


def test_artifacts_ignored():
    out = subprocess.run(['git', 'check-ignore', '-q', '.ui-captures/capture-x/light-1300.png'], cwd=ROOT)
    assert out.returncode == 0


def test_real_capture():
    try:
        import playwright  # noqa: F401
    except ImportError:
        print('  (Playwright 없음 — 실제 캡처 건너뜀)')
        return
    def run(route):
        # 명령 한 번이 캡처 한 번이다(Playwright용 환경 교체는 프로세스 안에서 한 번만 안전하다)
        out = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'capture_ui.py'), '--route', route],
                             cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=300)
        result = json.loads(out.stdout.strip().splitlines()[-1])
        manifest = json.loads((ROOT / result['manifest']).read_text(encoding='utf-8'))
        assert result['status'] == manifest['status'] and out.returncode == (0 if result['status'] == 'ok' else 1)
        return result, manifest

    for route in ('#/', '#/gallery'):
        result, manifest = run(route)
        assert manifest['status'] == 'ok' and manifest['route'] == route, manifest
        names = sorted(Path(c['path']).name for c in manifest['captures'])
        assert names == ['dark-1300.png', 'dark-390.png', 'light-1300.png', 'light-390.png']
        assert all((ROOT / c['path']).stat().st_size > 1000 for c in manifest['captures'])
        print('  ', route, '→', result['manifest'])
    # 데이터가 없는 파드는 홈으로 떨어지므로 성공 PNG가 아니라 실패여야 한다
    result, manifest = run('#/pod/no-such-pod/pgallery')
    assert manifest['status'] == 'failed' and manifest['errors'][0]['stage'].startswith('route/'), manifest


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print('ok', name)
