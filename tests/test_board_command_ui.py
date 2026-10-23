"""격리 서버와 브라우저에서 보드 빠른 명령의 카드 생성·오류·실행 취소를 검사한다."""
import os
import io
import base64
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


class BoardCommandUITests(unittest.TestCase):
    def test_first_stage_commands(self):
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
                    page = browser.new_page(viewport={'width': 1300, 'height': 850})
                    base = f'http://127.0.0.1:{port}'
                    for _ in range(100):
                        try:
                            if page.request.get(base + '/api/health').ok:
                                break
                        except Exception:
                            time.sleep(.1)
                    else:
                        if server.poll() is not None:
                            self.fail('격리 서버 시작 실패: ' + server.stderr.read().decode('utf-8', 'replace')[-2000:])
                        self.fail('격리 서버가 시작되지 않았어요')
                    login = page.request.post(base + '/api/auth/login', data={
                        'username': 'admin', 'password': password,
                    }, headers={'X-Requested-With': 'nightshift'})
                    self.assertTrue(login.ok, login.text())
                    project = page.request.post(base + '/api/projects', data={'name': '명령 테스트'},
                                                headers={'X-Requested-With': 'nightshift'})
                    self.assertTrue(project.ok, project.text())
                    project_id = project.json()['id']
                    page.goto(base + '/')
                    page.evaluate('async (id) => { await fetchProjects(); showTab("prboard", {projectId: id}); }', project_id)
                    parsed = page.evaluate('''() => [
                        parseBoardCommand('메모'), parseBoardCommand('/t 제목'),
                        parseBoardCommand('//slash'), parseBoardCommand('/txet')
                    ]''')
                    self.assertEqual([item.get('cmd') for item in parsed[:3]], ['text', 'text', 'text'])
                    self.assertEqual(parsed[2]['args'], '/slash')
                    self.assertIn('모르는 명령', parsed[3]['error'])
                    aliases = page.evaluate('''() => [
                        parseBoardCommand('/image'), parseBoardCommand('/img'),
                        parseBoardCommand('/i 고양이'), parseBoardCommand('/video'),
                        parseBoardCommand('/v 영상')
                    ]''')
                    self.assertEqual([item['cmd'] for item in aliases], ['image', 'image', 'image', 'video', 'video'])
                    self.assertEqual(aliases[2]['args'], '고양이')
                    field = page.locator('#board-command-input')
                    field.wait_for(state='visible')
                    page.locator('#board-viewport').click(position={'x': 350, 'y': 250})
                    page.keyboard.press('/')
                    self.assertTrue(field.evaluate('(el) => document.activeElement === el'))
                    field.fill('안녕')
                    field.press('Enter')
                    page.locator('.board-node-text').filter(has_text='안녕').wait_for()
                    self.assertEqual(page.locator('.board-node-text').count(), 1)
                    self.assertEqual(page.locator('.board-node-text.selected').count(), 1)
                    field.blur()
                    page.keyboard.press('Control+z')
                    page.locator('.board-node-text').wait_for(state='detached')
                    field.fill('/txet a')
                    field.press('Enter')
                    self.assertIn('모르는 명령', page.locator('#board-command-error').inner_text())
                    self.assertEqual(page.locator('.board-node-text').count(), 0)
                    field.fill('/te')
                    field.press('Tab')
                    self.assertEqual(field.input_value(), '/text ')
                    field.fill('/job 없는작업')
                    field.press('Enter')
                    expect(page.locator('#board-command-error')).to_contain_text('찾지 못했어요')
                    self.assertEqual(page.locator('.board-node-job').count(), 0)
                    field.fill('/text 조합')
                    field.dispatch_event('keydown', {'key': 'Enter', 'isComposing': True})
                    self.assertEqual(page.locator('.board-node-text').count(), 0)
                    field.fill('첫째')
                    field.press('Enter')
                    page.locator('.board-node-text').filter(has_text='첫째').wait_for()
                    field.fill('둘째')
                    field.press('Enter')
                    page.locator('.board-node-text').filter(has_text='둘째').wait_for()
                    field.fill('셋째')
                    field.press('Enter')
                    page.locator('.board-node-text').filter(has_text='셋째').wait_for()
                    boxes = page.locator('.board-node-text').evaluate_all(
                        '(nodes) => nodes.map(n => ({x: parseFloat(n.style.left), y: parseFloat(n.style.top)}))')
                    self.assertEqual(len({(box['x'], box['y']) for box in boxes}), 3)
                    field.fill('/frame 장면1')
                    field.press('Enter')
                    page.locator('.board-frame').wait_for()
                    self.assertEqual(page.locator('.board-frame-title').input_value(), '장면1')
                    field.fill('?')
                    field.press('Enter')
                    page.locator('#board-command-help').wait_for(state='visible')
                    page.keyboard.press('Escape')
                    self.assertFalse(page.locator('#board-command-help').is_visible())
                    png = io.BytesIO()
                    Image.new('RGB', (2, 2), 'red').save(png, format='PNG')
                    for name, mime, content in [('찾을사진.png', 'image/png', png.getvalue()),
                                                ('찾을영상.mp4', 'video/mp4', b'video')]:
                        uploaded = page.request.post(base + '/api/output-assets/upload', multipart={
                            'files': {'name': name, 'mimeType': mime, 'buffer': content},
                            'project_id': str(project_id),
                        }, headers={'X-Requested-With': 'nightshift'})
                        self.assertTrue(uploaded.ok, uploaded.text())
                    field.fill('/image 찾을사진')
                    expected_position = page.evaluate('() => boardCommandGeometry("image")')
                    field.press('Enter')
                    picker = page.locator('#board-asset-picker-modal')
                    picker.wait_for(state='visible')
                    page.locator('#board-asset-picker-grid [data-pick]:visible').wait_for()
                    self.assertEqual(page.locator('#board-asset-picker-search').input_value(), '찾을사진')
                    page.locator('#board-asset-picker-grid [data-pick]:visible').click()
                    image = page.locator('.board-node-image')
                    image.wait_for()
                    self.assertEqual(image.count(), 1)
                    self.assertEqual(page.locator('.board-node-image.selected').count(), 1)
                    placed_position = image.evaluate('(el) => ({x: parseFloat(el.style.left), y: parseFloat(el.style.top)})')
                    self.assertEqual(placed_position, {key: expected_position[key] for key in ('x', 'y')})
                    self.assertEqual(field.input_value(), '')
                    field.blur()
                    page.keyboard.press('Control+z')
                    image.wait_for(state='detached')
                    field.fill('/video')
                    field.press('Enter')
                    picker.wait_for(state='visible')
                    page.locator('#board-asset-picker-grid [data-pick]:visible').click()
                    page.locator('.board-node-video.selected').wait_for()
                    field.fill('/image')
                    field.press('Enter')
                    picker.wait_for(state='visible')
                    page.keyboard.press('Escape')
                    self.assertFalse(picker.is_visible())
                    self.assertTrue(field.evaluate('(el) => document.activeElement === el'))
                    self.assertEqual(field.input_value(), '/image')
                    field.fill('')
                    page.evaluate('''(encoded) => {
                        const file = new File([Uint8Array.from(atob(encoded), c => c.charCodeAt(0))], '붙여넣기.png', {type:'image/png'});
                        const transfer = new DataTransfer(); transfer.items.add(file);
                        document.getElementById('board-command-input').dispatchEvent(new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:transfer}));
                    }''', base64.b64encode(png.getvalue()).decode())
                    expect(page.locator('.board-node-image')).to_have_count(1)
                    page.evaluate('showTab("home")')
                    self.assertFalse(field.is_visible())
                    browser.close()
            finally:
                server.terminate()
                server.wait(timeout=10)
                if server.stderr:
                    server.stderr.close()


if __name__ == '__main__':
    unittest.main()
