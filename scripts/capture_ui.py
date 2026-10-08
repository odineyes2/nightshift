"""임시 admin으로 지정한 nightshift 화면(route)을 낮·밤 × 1300·390으로 캡처한다.

운영 데이터·.env는 쓰지 않는다 — server/static/templates의 추적 파일만 .ui-captures/ 아래로
복사해 임시 데이터 폴더로 띄우고, 외부 연결과 lifespan(스케줄러·RunPod 동기화)을 막는다.
결과는 .ui-captures/capture-*/ 안의 PNG 네 장과 manifest.json(dev scripts/capture_ui.py와 같은 형식).

    python scripts/capture_ui.py --route "#/"
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from capture_ui_support import (BOOTSTRAP, child_environment, copy_sources, free_port, local_request,
                                manifest_status, parse_route, stop_process, tracked_files)

SIZES = ((1300, 850), (390, 800))
STARTUP_TIMEOUT = 60


def wait_server(process, base, timeout=STARTUP_TIMEOUT):
    """프로세스가 살아 있고 /api/auth/me가 응답할 때까지 기다린다."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'서버가 바로 종료됐다 (code {process.returncode})')
        try:
            urllib.request.urlopen(base + '/api/auth/me', timeout=2)
            return
        except urllib.error.HTTPError:
            return   # 401도 응답이다
        except OSError:
            time.sleep(0.3)
    raise TimeoutError('서버 기동 시간 초과')


@contextmanager
def isolated_environment(env, cwd):
    """Playwright driver도 허용 목록 환경과 임시 cwd만 물려받게 한다."""
    original, original_cwd = dict(os.environ), Path.cwd()
    os.environ.clear()
    os.environ.update(env)
    os.chdir(cwd)
    try:
        yield
    finally:
        os.chdir(original_cwd)
        os.environ.clear()
        os.environ.update(original)


def login(context, base, env):
    response = context.request.post(base + '/api/auth/login', headers={'x-requested-with': 'nightshift'},
                                    data={'username': env['NIGHTSHIFT_ADMIN_USER'],
                                          'password': env['NIGHTSHIFT_ADMIN_PASSWORD']})
    if not response.ok:
        raise RuntimeError(f'테스트 로그인 실패 ({response.status})')
    me = context.request.get(base + '/api/auth/me')
    if not me.ok or (me.json().get('user') or me.json()).get('role') != 'admin':
        raise RuntimeError(f'로그인 후 admin 확인 실패 ({me.status})')


def wait_screen(page, tab, pod_id, project_id):
    """앱이 요청한 탭·범위를 실제로 열었는지 확인한다. 홈으로 돌아가면 실패다."""
    try:
        page.wait_for_function(
            """([tab, pod, project]) => typeof currentUser !== 'undefined' && currentUser
                && currentTab === tab && currentPodId === pod && currentProjectId === project
                && TAB_MAINS[tab] && TAB_MAINS[tab].style.display !== 'none'""",
            arg=[tab, pod_id, project_id], timeout=15000)
    except Exception:
        state = page.evaluate("() => typeof currentTab === 'undefined' ? null : [currentTab, currentPodId, currentProjectId]")
        raise RuntimeError(f'route 화면을 열지 못했다 (앱 상태: {state})')
    page.evaluate('() => document.fonts.ready')
    page.wait_for_timeout(500)


def capture(route='#/'):
    app_hash, tab, pod_id, project_id = parse_route(route)
    output = ROOT / '.ui-captures'
    output.mkdir(exist_ok=True)
    if output.is_symlink() or output.resolve().parent != ROOT.resolve():
        raise ValueError('산출물 폴더는 worktree 안에 있어야 한다')
    directory = Path(tempfile.mkdtemp(prefix='capture-', dir=output))
    manifest = {'version': 1, 'route': route, 'fixture': 'empty', 'captures': [], 'errors': []}
    process, log, stage, env = None, None, 'startup', {}
    try:
        copy_sources(ROOT, tracked_files(ROOT), directory / 'src')
        env = child_environment(directory)
        port = free_port()
        base = f'http://127.0.0.1:{port}'
        (directory / 'capture_app.py').write_text(BOOTSTRAP, encoding='utf-8')
        log = (directory / 'server.log').open('w', encoding='utf-8')
        process = subprocess.Popen([sys.executable, str(directory / 'capture_app.py'), str(ROOT / 'scripts'),
                                    str(directory / 'src' / 'server'), str(port)],
                                   cwd=directory, env=env, stdout=log, stderr=log)
        wait_server(process, base)
        stage = 'browser'
        from playwright.sync_api import sync_playwright
        with isolated_environment(env, directory / 'tmp'), sync_playwright() as playwright:
            browser = playwright.chromium.launch(env=env)
            try:
                for theme in ('light', 'dark'):
                    for width, height in SIZES:
                        stage = f'route/{theme}/{width}'
                        context = browser.new_context(service_workers='block', color_scheme=theme,
                                                      viewport={'width': width, 'height': height})
                        try:
                            context.route('**/*', lambda r: r.continue_() if local_request(r.request.url, base) else r.abort())
                            context.add_init_script(f"localStorage.setItem('nightshift-theme', '{theme}')")
                            stage = f'login/{theme}/{width}'
                            login(context, base, env)
                            stage = f'route/{theme}/{width}'
                            page = context.new_page()
                            page.goto(base + '/' + app_hash)
                            wait_screen(page, tab, pod_id, project_id)
                            filename = f'{theme}-{width}.png'
                            page.screenshot(path=str(directory / filename), full_page=True)
                            manifest['captures'].append({'path': (directory / filename).relative_to(ROOT).as_posix(),
                                                         'theme': theme, 'width': width, 'height': height})
                        except Exception as exc:
                            manifest['errors'].append({'stage': stage, 'reason': type(exc).__name__ + ': ' + str(exc)[:1200]})
                        finally:
                            context.close()
            finally:
                browser.close()
    except Exception as exc:
        manifest['errors'].append({'stage': stage, 'reason': type(exc).__name__ + ': ' + str(exc)[:1200]})
    finally:
        if process is not None:
            stop_process(process)
        if log is not None:
            log.close()
        if manifest['errors'] and manifest['errors'][0]['stage'] == 'startup' and (directory / 'server.log').exists():
            text = (directory / 'server.log').read_text(encoding='utf-8', errors='replace')[-1200:]
            manifest['errors'][0]['log_summary'] = text.replace(env.get('NIGHTSHIFT_ADMIN_PASSWORD') or '\0', '***')
        manifest['status'] = manifest_status(manifest)
        (directory / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return directory, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--route', default='#/')
    args = parser.parse_args(argv)
    try:
        directory, manifest = capture(args.route)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({'manifest': (directory / 'manifest.json').relative_to(ROOT).as_posix(),
                      'status': manifest['status'], 'errors': manifest['errors']}, ensure_ascii=False))
    return 0 if manifest['status'] == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
