"""capture_ui.py의 격리 부품 — route 검사·환경 허용 목록·코드 선택 복사·서버 bootstrap.

브라우저나 서버를 띄우지 않는 순수 함수만 두어 tests/test_capture_ui.py가 바로 검사한다.
"""
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

# 09-routing-results.js의 TAB_MAINS·POD_TABS, 11-projects.js의 PROJECT_TABS와 맞춘다.
GLOBAL_TABS = ('dashboard', 'pods', 'jobs', 'gallery', 'video-gallery', 'danbooru',
               'admin', 'db', 'settings', 'models', 'library')
POD_TABS = ('pgallery', 'pvideo', 'pmodels', 'results', 'psettings')
PROJECT_TABS = ('prboard', 'jobs', 'prgallery', 'prvideo')

# 복사 대상: 이 세 폴더의 git 추적 파일만. .env·data/·output/·assets/·옛 루트 런타임 파일은 그 밖이다.
COPY_DIRS = ('server', 'static', 'templates')

# 자식 프로세스(서버·Playwright driver·Chromium)가 물려받아도 되는 시스템 변수뿐이다.
ENV_ALLOW = ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PATH', 'SYSTEMDRIVE',
             'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS', 'OS',
             'LOCALAPPDATA', 'USERPROFILE', 'HOME', 'PLAYWRIGHT_BROWSERS_PATH', 'LANG')


def parse_route(route):
    """`#/...`만 받아 (앱 hash, 기대 탭, 파드 id, 프로젝트 id)로 바꾼다. 아니면 ValueError.

    앱 라우터는 `#gallery`·`#pod/{id}/{sub}` 꼴이라 앞의 `/`만 떼어 넘긴다."""
    m = re.fullmatch(r'#/([A-Za-z0-9_/-]*)', route or '')
    if route and ':/' in route:   # Git Bash가 "#/x"를 Windows 경로로 바꾼 흔적
        raise ValueError(f'route가 셸에서 경로로 바뀌었다({route!r}) — MSYS_NO_PATHCONV=1을 붙이거나 PowerShell에서 실행한다')
    if not m or '//' in route or route.endswith('/') and route != '#/':
        raise ValueError(f'로컬 hash 경로만 받는다({route!r}): #/, #/gallery, #/pod/<id>/pgallery, #/project/<id>/prboard')
    parts = m.group(1).split('/') if m.group(1) else []
    if not parts:
        return '#dashboard', 'dashboard', None, None
    if len(parts) == 1 and parts[0] in GLOBAL_TABS:
        return '#' + parts[0], parts[0], None, None
    if parts[0] == 'pod' and len(parts) == 3 and parts[2] in POD_TABS:
        return '#' + '/'.join(parts), parts[2], parts[1], None
    if parts[0] == 'project' and len(parts) == 3 and parts[2] in PROJECT_TABS:
        pid = parts[1] if parts[1] == 'unassigned' else int(parts[1]) if parts[1].isdigit() else None
        if pid is not None:
            return '#' + '/'.join(parts), parts[2], None, pid
    raise ValueError(f'알 수 없는 화면: {route}')


def local_request(url, base):
    """브라우저 요청은 이번 캡처 서버 origin만 보낸다."""
    parsed = urlsplit(url)
    return parsed.scheme in ('http', 'https', 'ws') and parsed.netloc == urlsplit(base).netloc or parsed.scheme in ('data', 'blob', 'about')


def child_environment(directory, source_env=None):
    """허용 목록만 물려받고 데이터·임시 경로와 테스트 admin을 강제한 환경을 만든다."""
    source_env = os.environ if source_env is None else source_env
    upper = {k.upper(): v for k, v in source_env.items()}
    env = {k: upper[k] for k in ENV_ALLOW if k in upper}
    tmp = directory / 'tmp'
    for name in ('data', 'output', 'assets', 'tmp'):
        (directory / name).mkdir(parents=True, exist_ok=True)
    env.update({
        'NIGHTSHIFT_DATA_DIR': str(directory / 'data'),
        'NIGHTSHIFT_DB_PATH': str(directory / 'data' / 'nightshift.db'),
        'NIGHTSHIFT_OUTPUT_DIR': str(directory / 'output'),
        'NIGHTSHIFT_ASSETS_DIR': str(directory / 'assets'),
        'NIGHTSHIFT_ADMIN_USER': 'capture-admin',
        'NIGHTSHIFT_ADMIN_PASSWORD': 'Capture-Only-' + os.urandom(8).hex(),
        'TEMP': str(tmp), 'TMP': str(tmp), 'TMPDIR': str(tmp),
        'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
    })
    return env


def tracked_files(root):
    """COPY_DIRS 안의 git 추적 파일 상대 경로."""
    out = subprocess.run(['git', 'ls-files', '-z', '--', *COPY_DIRS], cwd=root, capture_output=True)
    if out.returncode:
        raise RuntimeError('git ls-files 실패: ' + out.stderr.decode('utf-8', 'replace')[:300])
    return [p for p in out.stdout.decode('utf-8').split('\0') if p]


def copy_sources(root, files, dest):
    """허용 폴더의 일반 파일만 dest로 복사한다. 링크·경로 탈출·비밀 파일은 거절한다."""
    root = Path(root).resolve()
    copied = []
    for rel in files:
        parts = Path(rel).parts
        if not parts or parts[0] not in COPY_DIRS or '..' in parts or Path(rel).is_absolute():
            continue
        if any(p == '.env' or p.startswith('.env.') for p in parts):
            continue
        src = root / rel
        if src.is_symlink() or not src.is_file():
            continue
        if root not in src.resolve().parents:
            raise ValueError(f'원본 경로가 worktree 밖이다: {rel}')
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        copied.append(rel)
    return copied


def install_network_guard(allowed_port):
    """127.0.0.1:allowed_port 외의 연결·이름 조회를 막는다. 서버 bootstrap이 앱 import 전에 부른다."""
    import threading
    local = threading.local()
    orig_connect, orig_connect_ex = socket.socket.connect, socket.socket.connect_ex
    orig_pair, orig_getaddrinfo = socket.socketpair, socket.getaddrinfo

    def allowed(sock, address):
        if getattr(local, 'pair', False):   # Windows asyncio의 socketpair는 자기 리스너로 붙는다
            return True
        if sock.family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, tuple):
            return False
        return address[0] in ('127.0.0.1', '::1', 'localhost') and int(address[1]) == allowed_port

    def connect(self, address):
        if not allowed(self, address):
            raise ConnectionRefusedError(f'capture_ui: 외부 연결 차단 {address!r}')
        return orig_connect(self, address)

    def connect_ex(self, address):
        if not allowed(self, address):
            return 111
        return orig_connect_ex(self, address)

    def socketpair(*a, **k):
        local.pair = True
        try:
            return orig_pair(*a, **k)
        finally:
            local.pair = False

    def getaddrinfo(host, *a, **k):
        if host not in ('127.0.0.1', '::1', 'localhost', None):
            raise socket.gaierror(f'capture_ui: 이름 조회 차단 {host!r}')
        return orig_getaddrinfo(host, *a, **k)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.socketpair, socket.getaddrinfo = socketpair, getaddrinfo


# 임시 사본의 server/에서 실행한다. lifespan(스케줄러·RunPod 동기화·결과 동기화)을 끄고
# 로그인과 빈 화면에 필요한 초기화만 직접 한다.
BOOTSTRAP = '''
import os, sys
sys.path.insert(0, sys.argv[1])
import capture_ui_support
port = int(sys.argv[3])
capture_ui_support.install_network_guard(port)
os.chdir(sys.argv[2])
sys.path.insert(0, sys.argv[2])
import app as m
m.load_state()
admin = m.auth.ensure_admin()
m.pod_registry.load()
if admin is not None:
    m.pod_registry.assign_orphans(admin["id"])
m.recent_workflows_store.load()
m.recent_csvs_store.load()
import uvicorn
uvicorn.run(m.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
'''


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def stop_process(process, timeout=5):
    """terminate → 기다림 → kill → 기다림. 이미 끝났으면 회수만 한다."""
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def manifest_status(manifest):
    if len(manifest['captures']) == 4 and not manifest['errors']:
        return 'ok'
    return 'partial' if manifest['captures'] else 'failed'
