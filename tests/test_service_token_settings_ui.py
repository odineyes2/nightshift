"""격리 브라우저에서 Settings 토큰의 일회 표시·폐기와 생성 프리셋 편집을 확인한다."""
import os
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / '.ui-captures' / 'ns-115-4'


def capture_settings(page, name):
    CAPTURES.mkdir(parents=True, exist_ok=True)
    for theme in ('light', 'dark'):
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        for width in (1300, 390):
            page.set_viewport_size({'width': width, 'height': 850})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), f'{name}: {theme} {width} 가로 스크롤'
            page.screenshot(path=str(CAPTURES / f'{name}-{theme}-{width}.png'), full_page=True)
    page.set_viewport_size({'width': 1300, 'height': 850})


class ServiceTokenSettingsUITests(unittest.TestCase):
    def test_token_and_preset_controls(self):
        with tempfile.TemporaryDirectory() as temp, socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            sock.close()
            password = secrets.token_urlsafe(24)
            env = os.environ.copy()
            for name in ('DATA', 'OUTPUT', 'ASSETS'):
                env[f'NIGHTSHIFT_{name}_DIR'] = str(Path(temp) / name.lower())
            env['NIGHTSHIFT_ADMIN_USER'] = 'admin'
            env['NIGHTSHIFT_ADMIN_PASSWORD'] = password
            server = subprocess.Popen(
                [sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(port)],
                cwd=ROOT / 'server', env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={'width': 1300, 'height': 850},
                                             permissions=['clipboard-read', 'clipboard-write'])
                    base = f'http://127.0.0.1:{port}'
                    for _ in range(100):
                        try:
                            if page.request.get(base + '/api/health').ok:
                                break
                        except Exception:
                            time.sleep(.1)
                    else:
                        self.fail('격리 서버가 시작되지 않았어요')
                    login = page.request.post(base + '/api/auth/login', data={
                        'username': 'admin', 'password': password,
                    }, headers={'X-Requested-With': 'nightshift'})
                    self.assertTrue(login.ok, login.text())
                    user = page.request.post(base + '/api/auth/register', data={
                        'username': 'member', 'email': 'member@example.test', 'password': password,
                    }, headers={'X-Requested-With': 'nightshift'})
                    self.assertTrue(user.ok, user.text())
                    # 활성화는 관리자 UI와 같은 API로 진행한다.
                    users = page.request.get(base + '/api/admin/users').json()['users']
                    member = next(item for item in users if item['username'] == 'member')
                    activate = page.request.post(base + f'/api/admin/users/{member["id"]}/status',
                                                 data={'status': 'active'}, headers={'X-Requested-With': 'nightshift'})
                    self.assertTrue(activate.ok, activate.text())
                    page.goto(base + '/#settings')
                    page.locator('[data-settings-tab="service-tokens"]').click()
                    expect(page.locator('#service-token-user option')).to_have_count(1)
                    page.locator('#service-token-name').fill('Moai')
                    page.locator('#service-token-form button[type="submit"]').click()
                    expect(page.locator('#service-token-issued')).to_be_visible()
                    issued = page.locator('#service-token-value').input_value()
                    self.assertTrue(issued.startswith('nst_'))
                    expect(page.locator('#service-token-list')).to_contain_text(issued[-4:])
                    self.assertIn(issued[-4:], page.locator('#service-token-list').inner_text())
                    self.assertNotIn(issued, page.locator('#service-token-list').inner_text())
                    capture_settings(page, 'service-token-issued')
                    page.locator('#service-token-copy').click()
                    expect(page.locator('#service-token-copy-status')).to_have_text('복사했어요')
                    self.assertEqual(page.evaluate('navigator.clipboard.readText()'), issued)
                    # 원문은 설정 화면을 떠나면 지워진다.
                    page.evaluate("location.hash = '#jobs'")
                    expect(page.locator('#service-token-issued')).to_be_hidden()
                    page.evaluate("location.hash = '#settings'")
                    page.locator('[data-settings-tab="service-tokens"]').click()
                    self.assertEqual(page.locator('#service-token-value').input_value(), '')
                    capture_settings(page, 'service-tokens')
                    page.once('dialog', lambda dialog: dialog.accept())
                    page.locator('[data-revoke-token]').click()
                    expect(page.locator('#service-token-list')).to_contain_text('폐기됨')
                    self.assertEqual(page.locator('[data-revoke-token]').count(), 0)
                    page.locator('[data-settings-tab="generate-presets"]').click()
                    expect(page.locator('#generate-preset-users input')).to_have_count(1)
                    page.locator('#generate-preset-name').fill('article')
                    workflow = {
                        '1': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'positive'}},
                        '2': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'negative'}},
                        '3': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512, 'height': 512, 'batch_size': 1}},
                        '4': {'class_type': 'KSampler', 'inputs': {'positive': ['1', 0], 'negative': ['2', 0],
                                                                  'latent_image': ['3', 0], 'steps': 20, 'seed': 1}},
                    }
                    page.locator('#generate-preset-workflow').fill(json.dumps(workflow))
                    page.locator('#generate-preset-users input').check()
                    page.locator('#generate-preset-save').click()
                    expect(page.locator('#generate-preset-list')).to_contain_text('article')
                    expect(page.locator('#generate-preset-save')).to_have_text('프리셋 저장')
                    capture_settings(page, 'generate-presets')
                    page.locator('[data-edit-preset]').click()
                    expect(page.locator('#generate-preset-name')).to_have_value('article')
                    page.locator('#generate-preset-steps').fill('40')
                    page.locator('#generate-preset-save').click()
                    expect(page.locator('#generate-preset-list')).to_contain_text('40단계')
                    page.once('dialog', lambda dialog: dialog.accept())
                    page.locator('[data-delete-preset]').click()
                    expect(page.locator('#generate-preset-list')).to_contain_text('등록된 생성 프리셋이 없어요')
                    browser.close()
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                server.stderr.close()


if __name__ == '__main__':
    unittest.main()
