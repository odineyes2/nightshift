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
                    paste_text = '''(value) => {
                        const transfer = new DataTransfer();
                        transfer.setData('text/plain', value);
                        document.getElementById('board-command-input').dispatchEvent(
                            new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:transfer}));
                    }'''
                    field.fill('')
                    page.evaluate(paste_text, '첫 줄\n\n- 둘째 줄\n1. /text 그대로')
                    choice = page.locator('#board-command-paste-choice')
                    expect(choice).to_be_visible()
                    self.assertIn('3장', choice.inner_text())
                    if os.environ.get('NS99_CAPTURE') == '1':
                        shots = ROOT / '.ui-captures' / 'NS-99-1'
                        shots.mkdir(parents=True, exist_ok=True)
                        for theme in ('light', 'dark'):
                            page.evaluate('(value) => document.documentElement.dataset.theme = value', theme)
                            for width, height in ((1300, 850), (390, 800)):
                                page.set_viewport_size({'width': width, 'height': height})
                                page.evaluate('(mobile) => document.getElementById("board-tree").hidden = mobile', width < 700)
                                page.screenshot(path=str(shots / f'choice-{theme}-{width}.png'), full_page=True)
                        page.set_viewport_size({'width': 1300, 'height': 850})
                        page.evaluate('document.getElementById("board-tree").hidden = false')
                    field.press('Enter')
                    expect(page.locator('.board-node-text')).to_have_count(3)
                    self.assertEqual(page.locator('.board-node-text.selected').count(), 3)
                    self.assertEqual([item.input_value() for item in page.locator('.board-node-textarea').all()],
                                     ['첫 줄', '둘째 줄', '/text 그대로'])
                    positions = page.locator('.board-node-text').evaluate_all('''nodes => nodes.map(el =>
                        ({x:parseFloat(el.style.left), y:parseFloat(el.style.top)}))''')
                    self.assertEqual([item['y'] for item in positions], [positions[0]['y']] * 3)
                    self.assertEqual([item['x'] - positions[0]['x'] for item in positions], [0, 240, 480])
                    field.blur()
                    page.keyboard.press('Control+z')
                    expect(page.locator('.board-node-text')).to_have_count(0)
                    page.evaluate('''() => {
                        window.ns99BoardApi = boardApi;
                        let creates = 0;
                        boardApi = (...args) => {
                            if(args[1] === 'POST' && args[2] === '/nodes' && ++creates === 2) return Promise.resolve(null);
                            return window.ns99BoardApi(...args);
                        };
                    }''')
                    field.fill('')
                    page.evaluate(paste_text, '첫 메모\n둘째 메모\n셋째 메모')
                    field.press('Enter')
                    expect(page.locator('.board-node-text')).to_have_count(1)
                    self.assertIn('3장 중 1장', page.locator('#board-command-error').inner_text())
                    page.evaluate('() => { boardApi = window.ns99BoardApi; }')
                    field.blur()
                    page.keyboard.press('Control+z')
                    expect(page.locator('.board-node-text')).to_have_count(0)
                    field.fill('')
                    page.evaluate(paste_text, '한 줄\n두 줄')
                    field.press('Escape')
                    expect(choice).to_be_hidden()
                    self.assertEqual(field.input_value(), '한 줄\n두 줄')
                    field.press('Enter')
                    expect(page.locator('.board-node-text')).to_have_count(1)
                    self.assertEqual(page.locator('.board-node-textarea').input_value(), '한 줄\n두 줄')
                    field.blur()
                    page.keyboard.press('Control+z')
                    expect(page.locator('.board-node-text')).to_have_count(0)
                    field.fill('')
                    page.evaluate(paste_text, '\n'.join(f'{i}번째 줄' for i in range(51)))
                    field.press('Enter')
                    self.assertIn('50줄까지', page.locator('#board-command-error').inner_text())
                    self.assertEqual(page.locator('.board-node').count(), 0)
                    field.fill('직접')
                    field.press('Shift+Enter')
                    field.type('쓴 글')
                    expect(choice).to_be_hidden()
                    field.press('Enter')
                    expect(page.locator('.board-node-text')).to_have_count(1)
                    field.blur()
                    page.keyboard.press('Control+z')
                    expect(page.locator('.board-node-text')).to_have_count(0)
                    field.fill('')
                    field.press('Enter')
                    self.assertEqual(page.locator('.board-node').count(), 0)
                    self.assertEqual(page.locator('#board-command-error').inner_text(), '')
                    for command in ('/t', '/text'):
                        field.fill(command)
                        field.press('Enter')
                        note = page.locator('.board-node-text.selected .board-node-textarea')
                        note.wait_for()
                        self.assertEqual(note.input_value(), '')
                        self.assertTrue(note.evaluate('(el) => document.activeElement === el'))
                        self.assertEqual(field.input_value(), '')
                        if command == '/t' and os.environ.get('NS94_CAPTURE') == '1':
                            shots = ROOT / '.ui-captures' / 'NS-94-1'
                            shots.mkdir(parents=True, exist_ok=True)
                            for theme in ('light', 'dark'):
                                page.evaluate('(value) => document.documentElement.dataset.theme = value', theme)
                                for width, height in ((1300, 850), (390, 800)):
                                    page.set_viewport_size({'width': width, 'height': height})
                                    page.evaluate('(mobile) => document.getElementById("board-tree").hidden = mobile', width < 700)
                                    page.evaluate('boardFitToNodes()')
                                    page.screenshot(path=str(shots / f'note-{theme}-{width}.png'), full_page=True)
                            page.set_viewport_size({'width': 1300, 'height': 850})
                            page.evaluate('document.getElementById("board-tree").hidden = false')
                        note.blur()
                        page.keyboard.press('Control+z')
                        page.locator('.board-node-text').wait_for(state='detached')
                    field.fill('/job')
                    field.press('Enter')
                    picker = page.locator('#board-asset-picker-modal')
                    picker.wait_for(state='visible')
                    self.assertEqual(page.locator('#board-asset-picker-title').inner_text(), '작업 고르기')
                    self.assertEqual(page.locator('.board-node-job').count(), 0)
                    if os.environ.get('NS94_CAPTURE') == '1':
                        shots = ROOT / '.ui-captures' / 'NS-94-1'
                        for theme in ('light', 'dark'):
                            page.evaluate('(value) => document.documentElement.dataset.theme = value', theme)
                            for width, height in ((1300, 850), (390, 800)):
                                page.set_viewport_size({'width': width, 'height': height})
                                page.evaluate('(mobile) => document.getElementById("board-tree").hidden = mobile', width < 700)
                                page.screenshot(path=str(shots / f'job-{theme}-{width}.png'), full_page=True)
                        page.set_viewport_size({'width': 1300, 'height': 850})
                        page.evaluate('document.getElementById("board-tree").hidden = false')
                    page.locator('#board-asset-picker-close').click()
                    self.assertEqual(field.input_value(), '/job')
                    field.fill('')
                    field.fill('/txet a ')
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
                    video = page.locator('.board-node-video.board-node-empty')
                    video.wait_for()
                    self.assertEqual(video.count(), 1)
                    video.locator('.board-node-fill').click()
                    picker.wait_for(state='visible')
                    self.assertEqual(page.locator('#board-asset-picker-title').inner_text(), '이 카드 채우기')
                    page.locator('#board-asset-picker-grid [data-pick]:visible').click()
                    page.locator('.board-node-video:not(.board-node-empty)').wait_for()
                    self.assertEqual(page.locator('.board-node-video').count(), 1)
                    page.evaluate('document.activeElement.blur()')
                    page.keyboard.press('Control+z')
                    video.wait_for()
                    field.fill('/image')
                    field.press('Enter')
                    empty_image = page.locator('.board-node-image.board-node-empty')
                    empty_image.wait_for()
                    if os.environ.get('NS94_CAPTURE') == '1':
                        shots = ROOT / '.ui-captures' / 'NS-94-2'
                        shots.mkdir(parents=True, exist_ok=True)
                        for theme in ('light', 'dark'):
                            page.evaluate('(value) => document.documentElement.dataset.theme = value', theme)
                            for width, height in ((1300, 850), (390, 800)):
                                page.set_viewport_size({'width': width, 'height': height})
                                page.evaluate('(mobile) => document.getElementById("board-tree").hidden = mobile', width < 700)
                                page.evaluate('boardFitToNodes()')
                                page.screenshot(path=str(shots / f'empty-{theme}-{width}.png'), full_page=True)
                        page.set_viewport_size({'width': 1300, 'height': 850})
                        page.evaluate('document.getElementById("board-tree").hidden = false')
                    empty_image.locator('.board-node-fill').click()
                    picker.wait_for(state='visible')
                    page.keyboard.press('Escape')
                    self.assertFalse(picker.is_visible())
                    empty_image.locator('.board-node-fill').click()
                    page.locator('#board-asset-picker-grid [data-pick]:visible').click()
                    page.locator('.board-node-image:not(.board-node-empty)').wait_for()
                    self.assertEqual(page.locator('.board-node-image').count(), 1)
                    page.evaluate('document.activeElement.blur()')
                    page.keyboard.press('Control+z')
                    empty_image.wait_for()
                    page.evaluate('''(encoded) => {
                        const file = new File([Uint8Array.from(atob(encoded), c => c.charCodeAt(0))], '놓기.png', {type:'image/png'});
                        const transfer = new DataTransfer(); transfer.items.add(file);
                        document.querySelector('.board-node-image.board-node-empty').dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:transfer}));
                    }''', base64.b64encode(png.getvalue()).decode())
                    page.locator('.board-node-image:not(.board-node-empty)').wait_for()
                    self.assertEqual(page.locator('.board-node-image').count(), 1)
                    field.fill('')
                    page.evaluate('''(encoded) => {
                        const file = new File([Uint8Array.from(atob(encoded), c => c.charCodeAt(0))], '붙여넣기.png', {type:'image/png'});
                        const transfer = new DataTransfer(); transfer.items.add(file);
                        document.getElementById('board-command-input').dispatchEvent(new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:transfer}));
                    }''', base64.b64encode(png.getvalue()).decode())
                    expect(page.locator('.board-node-image')).to_have_count(1)
                    command = page.locator('#board-command')
                    opener = page.locator('#board-command-open')
                    field.fill('남겨 둘 초안')
                    page.locator('#board-command-close').click()
                    expect(command).to_be_hidden()
                    expect(opener).to_be_visible()
                    self.assertEqual(field.input_value(), '남겨 둘 초안')
                    self.assertEqual(page.evaluate('localStorage.getItem("nightshift.quickbarOpen")'), 'false')
                    self.assertEqual(page.locator('#board-selection-bar').evaluate('(el) => getComputedStyle(el).bottom'), '16px')
                    opener.click()
                    expect(command).to_be_visible()
                    self.assertEqual(page.locator('#board-selection-bar').evaluate('(el) => getComputedStyle(el).bottom'), '96px')
                    self.assertTrue(field.evaluate('(el) => document.activeElement === el'))
                    if os.environ.get('NS93_CAPTURE') == '1':
                        shots = ROOT / '.ui-captures' / 'NS-93-1'
                        shots.mkdir(parents=True, exist_ok=True)
                        for theme in ('light', 'dark'):
                            page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                            for width, height in ((1300, 850), (390, 800)):
                                page.set_viewport_size({'width': width, 'height': height})
                                page.evaluate('''(mobile) => {
                                    document.getElementById('board-tree').hidden = mobile;
                                    document.getElementById('board-world').style.visibility = 'hidden';
                                }''', width < 700)
                                page.screenshot(path=str(shots / f'open-{theme}-{width}.png'), full_page=True)
                        page.set_viewport_size({'width': 1300, 'height': 850})
                        page.evaluate('''() => {
                            document.getElementById('board-tree').hidden = false;
                            document.getElementById('board-world').style.visibility = '';
                        }''')
                    field.fill('/te')
                    expect(page.locator('#board-command-menu')).to_be_visible()
                    field.press('Escape')
                    expect(page.locator('#board-command-menu')).to_be_hidden()
                    self.assertTrue(field.evaluate('(el) => document.activeElement === el'))
                    field.fill('')
                    field.press('Escape')
                    expect(command).to_be_visible()
                    self.assertFalse(field.evaluate('(el) => document.activeElement === el'))
                    page.keyboard.press('Escape')
                    expect(command).to_be_hidden()
                    page.keyboard.press('/')
                    expect(command).to_be_visible()
                    self.assertTrue(field.evaluate('(el) => document.activeElement === el'))
                    field.fill('초안 유지')
                    field.press('Escape')
                    expect(command).to_be_visible()
                    self.assertEqual(field.input_value(), '초안 유지')
                    page.locator('#board-command-close').click()
                    if os.environ.get('NS93_CAPTURE') == '1':
                        shots = ROOT / '.ui-captures' / 'NS-93-1'
                        shots.mkdir(parents=True, exist_ok=True)
                        for theme in ('light', 'dark'):
                            page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                            for width, height in ((1300, 850), (390, 800)):
                                page.set_viewport_size({'width': width, 'height': height})
                                page.evaluate('''(mobile) => {
                                    document.getElementById('board-tree').hidden = mobile;
                                    document.getElementById('board-world').style.visibility = 'hidden';
                                }''', width < 700)
                                page.screenshot(path=str(shots / f'{theme}-{width}.png'), full_page=True)
                        page.set_viewport_size({'width': 1300, 'height': 850})
                    page.reload()
                    expect(opener).to_be_visible()
                    page.evaluate('async (id) => { await fetchProjects(); showTab("prboard", {projectId: id}); }', project_id)
                    opener.click()
                    self.assertEqual(field.input_value(), '')
                    page.evaluate('document.activeElement.blur()')
                    page.keyboard.press('`')
                    expect(page.locator('#composer')).to_be_visible()
                    expect(command).to_be_hidden()
                    expect(opener).to_be_hidden()
                    page.locator('#cmp-input').press('Escape')
                    expect(page.locator('#composer')).to_be_hidden()
                    expect(command).to_be_visible()
                    page.locator('.board-node-text').filter(has_text='첫째').wait_for()
                    cards_before_exit = page.locator('.board-node').count()
                    history_before_exit = page.evaluate('localStorage.getItem("nightshift.boardCommands")')
                    field.fill('/EXIT')
                    field.press('Enter')
                    expect(command).to_be_hidden()
                    expect(opener).to_be_visible()
                    self.assertEqual(field.input_value(), '')
                    self.assertEqual(page.locator('.board-node').count(), cards_before_exit)
                    self.assertEqual(page.evaluate('localStorage.getItem("nightshift.boardCommands")'), history_before_exit)
                    opener.click()
                    field.fill('/q')
                    field.press('Enter')
                    expect(command).to_be_hidden()
                    self.assertEqual(field.input_value(), '')
                    self.assertEqual(page.evaluate('localStorage.getItem("nightshift.boardCommands")'), history_before_exit)
                    if os.environ.get('NS93_CAPTURE') == '1':
                        shots = ROOT / '.ui-captures' / 'NS-93-2'
                        shots.mkdir(parents=True, exist_ok=True)
                        page.evaluate('''() => {
                            document.getElementById('board-world').style.visibility = 'hidden';
                            document.getElementById('board-selection-bar').style.visibility = 'hidden';
                        }''')
                        for theme in ('light', 'dark'):
                            page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                            for width, height in ((1300, 850), (390, 800)):
                                page.set_viewport_size({'width': width, 'height': height})
                                page.evaluate('(mobile) => document.getElementById("board-tree").hidden = mobile', width < 700)
                                page.screenshot(path=str(shots / f'closed-{theme}-{width}.png'), full_page=True)
                        page.set_viewport_size({'width': 1300, 'height': 850})
                    jobs_before_exit = len(page.request.get(base + '/api/jobs').json()['jobs'])
                    page.keyboard.press('`')
                    composer = page.locator('#composer')
                    expect(composer).to_be_visible()
                    page.evaluate('''() => {
                        cmpAttachments = [{key: 'test', filename: '남길.png', name: '남길.png', state: 'done'}];
                        cmpUpdate();
                    }''')
                    prompt = page.locator('#cmp-input')
                    prompt.fill('  /ExIt  ')
                    if os.environ.get('NS93_CAPTURE') == '1':
                        shots = ROOT / '.ui-captures' / 'NS-93-2'
                        shots.mkdir(parents=True, exist_ok=True)
                        page.evaluate('''() => {
                            document.getElementById('board-world').style.visibility = 'hidden';
                            document.getElementById('board-selection-bar').style.visibility = 'hidden';
                        }''')
                        for theme in ('light', 'dark'):
                            page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                            for width, height in ((1300, 850), (390, 800)):
                                page.set_viewport_size({'width': width, 'height': height})
                                page.evaluate('(mobile) => document.getElementById("board-tree").hidden = mobile', width < 700)
                                page.screenshot(path=str(shots / f'exit-{theme}-{width}.png'), full_page=True)
                        page.set_viewport_size({'width': 1300, 'height': 850})
                    prompt.press('Enter')
                    expect(composer).to_be_hidden()
                    self.assertEqual(prompt.input_value(), '')
                    self.assertEqual(page.evaluate('cmpAttachments.map(a => a.filename)'), ['남길.png'])
                    self.assertEqual(len(page.request.get(base + '/api/jobs').json()['jobs']), jobs_before_exit)
                    opener.click()
                    history_before_switch = page.evaluate('localStorage.getItem("nightshift.boardCommands")')
                    cards_before_switch = page.locator('.board-node').count()
                    field.fill('/퀵')
                    self.assertIn('/퀵런', page.locator('#board-command-menu').inner_text())
                    field.press('Tab')
                    self.assertEqual(field.input_value(), '/퀵런 ')
                    field.fill('/run')
                    field.press('Enter')
                    expect(composer).to_be_visible()
                    expect(command).to_be_hidden()
                    self.assertTrue(prompt.evaluate('(el) => document.activeElement === el'))
                    self.assertEqual(field.input_value(), '')
                    prompt.fill('남은 초안')
                    prompt.fill('/bar')
                    prompt.press('Enter')
                    expect(composer).to_be_hidden()
                    expect(command).to_be_visible()
                    self.assertTrue(field.evaluate('(el) => document.activeElement === el'))
                    field.fill('/퀵런 1girl beach')
                    field.press('Enter')
                    self.assertEqual(prompt.input_value(), '/bar\n1girl beach')
                    self.assertEqual(page.evaluate('cmpAttachments.map(a => a.filename)'), ['남길.png'])
                    prompt.fill('/퀵바 메모')
                    prompt.press('Enter')
                    self.assertEqual(field.input_value(), '메모')
                    self.assertEqual(page.locator('.board-node').count(), cards_before_switch)
                    self.assertEqual(page.evaluate('localStorage.getItem("nightshift.boardCommands")'), history_before_switch)
                    field.fill('/퀵런')
                    field.evaluate('''el => el.dispatchEvent(new KeyboardEvent('keydown',
                        {key: 'Enter', bubbles: true, isComposing: true}))''')
                    expect(composer).to_be_hidden()
                    field.press('Enter')
                    expect(composer).to_be_visible()
                    prompt.fill('/bar')
                    page.evaluate('showTab("home")')
                    prompt.press('Enter')
                    expect(composer).to_be_visible()
                    self.assertEqual(prompt.input_value(), '/bar')
                    self.assertIn('프로젝트 보드 탭에서만', page.locator('#cmp-error').inner_text())
                    page.evaluate('async (id) => { await fetchProjects(); showTab("prboard", {projectId: id}); }', project_id)
                    prompt.press('Enter')
                    expect(composer).to_be_hidden()
                    expect(command).to_be_visible()
                    self.assertEqual(page.evaluate('cmpAttachments.map(a => a.filename)'), ['남길.png'])
                    if os.environ.get('NS96_CAPTURE') == '1':
                        shots = ROOT / '.ui-captures' / 'NS-96-1'
                        shots.mkdir(parents=True, exist_ok=True)
                        page.evaluate('''() => {
                            document.getElementById('board-world').style.visibility = 'hidden';
                            document.getElementById('board-selection-bar').style.visibility = 'hidden';
                        }''')
                        for theme in ('light', 'dark'):
                            page.evaluate('(value) => document.documentElement.dataset.theme = value', theme)
                            for width, height in ((1300, 850), (390, 800)):
                                page.set_viewport_size({'width': width, 'height': height})
                                page.evaluate('(mobile) => document.getElementById("board-tree").hidden = mobile', width < 700)
                                page.screenshot(path=str(shots / f'quickbar-{theme}-{width}.png'), full_page=True)
                                field.fill('/run 1girl beach')
                                field.press('Enter')
                                page.wait_for_timeout(300)
                                page.screenshot(path=str(shots / f'quickrun-{theme}-{width}.png'), full_page=True)
                                prompt.fill('/bar')
                                prompt.press('Enter')
                        page.set_viewport_size({'width': 1300, 'height': 850})
                        page.evaluate('document.getElementById("board-tree").hidden = false')
                    # ---- NS-95-1: /gen ----
                    for name in ('장면 테스트', '장면 비교'):
                        preset = page.request.post(base + '/api/board-presets', data={
                            'name': name, 'template_id': 'seed_batch', 'workflow': {},
                            'options': {'seed_count': '1'}, 'exposed': ['main_prompt'], 'slots': [],
                        }, headers={'X-Requested-With': 'nightshift'})
                        self.assertTrue(preset.ok, preset.text())
                    field.fill('/gen 없는것 프롬프트')
                    field.press('Enter')
                    expect(page.locator('#board-command-error')).to_contain_text('없는 프리셋')
                    self.assertEqual(page.locator('.board-node-gen').count(), 0)
                    field.fill('/g 장면 프롬프트')
                    field.press('Enter')
                    expect(page.locator('#board-command-error')).to_contain_text('장면 테스트')
                    self.assertEqual(page.locator('.board-node-gen').count(), 0)
                    field.fill('/gen 장면')
                    expect(page.locator('#board-command-menu')).to_be_visible()
                    field.press('Tab')
                    self.assertIn(field.input_value(), ('/gen "장면 테스트" ', '/gen "장면 비교" '))
                    field.fill('/gen "장면 테스트" 1girl --ar 2:3 --seed 7')
                    field.press('Control+Enter')
                    gen = page.locator('.board-node-gen')
                    expect(gen).to_have_count(1)
                    expect(gen.locator('[data-gen-field="main_prompt"]')).to_have_value('1girl --ar 2:3 --seed 7')
                    self.assertEqual(len(page.request.get(base + '/api/jobs').json()['jobs']), jobs_before_exit)
                    if os.environ.get('NS95_CAPTURE') == '1':
                        shots = ROOT / '.ui-captures' / 'NS-95-1'
                        shots.mkdir(parents=True, exist_ok=True)
                        page.evaluate('''() => {
                            document.getElementById('board-world').style.visibility = 'hidden';
                            document.getElementById('board-selection-bar').style.visibility = 'hidden';
                        }''')
                        for theme in ('light', 'dark'):
                            page.evaluate('(value) => document.documentElement.dataset.theme = value', theme)
                            for width, height in ((1300, 850), (390, 800)):
                                page.set_viewport_size({'width': width, 'height': height})
                                page.evaluate('(mobile) => document.getElementById("board-tree").hidden = mobile', width < 700)
                                field.fill('/gen 장')
                                expect(page.locator('#board-command-menu')).to_be_visible()
                                page.screenshot(path=str(shots / f'gen-{theme}-{width}.png'), full_page=True)
                        page.set_viewport_size({'width': 1300, 'height': 850})
                        page.evaluate('''() => {
                            document.getElementById('board-world').style.visibility = '';
                            document.getElementById('board-selection-bar').style.visibility = '';
                            document.getElementById('board-tree').hidden = false;
                        }''')
                    field.fill('/gen "장면 테스트" 1girl --ar 2:3 --seed 7')
                    with page.expect_response(lambda r: '/board/nodes/' in r.url and r.url.split('?')[0].endswith('/run')):
                        field.press('Enter')
                    expect(page.locator('.board-node-gen')).to_have_count(2)
                    jobs = page.request.get(base + '/api/jobs').json()['jobs']
                    self.assertEqual(len(jobs), jobs_before_exit + 1)
                    self.assertEqual(jobs[0]['prompt_overrides'][0]['seed'], 7)
                    field.fill('/gen "장면 테스트" 1girl --se')
                    expect(page.locator('#board-command-menu')).to_contain_text('--seed')
                    field.press('Tab')
                    self.assertTrue(field.input_value().endswith('--seed '))
                    image_id = page.evaluate('() => boardNodes.find(n => n.kind === "image" && n.asset_path).id')
                    page.evaluate('(id) => setBoardSelection(new Set([id]))', image_id)
                    image_preset = page.request.post(base + '/api/board-presets', data={
                        'name': '이미지 생성', 'template_id': 'input_image_batch', 'workflow': {},
                        'exposed': ['main_prompt'], 'slots': ['input_image'],
                    }, headers={'X-Requested-With': 'nightshift'})
                    self.assertTrue(image_preset.ok, image_preset.text())
                    field.fill('/gen "이미지 생성" 연결 확인')
                    field.press('Control+Enter')
                    expect(page.locator('.board-node-gen')).to_have_count(3)
                    linked_id = page.evaluate('() => boardNodes.findLast(n => n.kind === "gen").id')
                    page.wait_for_function('(ids) => boardEdges.some(e => e.from_node_id === ids[0] && e.to_node_id === ids[1])', arg=[image_id, linked_id])
                    field.blur()
                    page.keyboard.press('Control+z')
                    expect(page.locator('.board-node-gen')).to_have_count(2)
                    self.assertFalse(page.evaluate('(id) => boardEdges.some(e => e.to_node_id === id)', linked_id))
                    no_prompt = page.request.post(base + '/api/board-presets', data={
                        'name': '글칸 없음', 'template_id': 'seed_batch', 'workflow': {}, 'exposed': [], 'slots': [],
                    }, headers={'X-Requested-With': 'nightshift'})
                    self.assertTrue(no_prompt.ok, no_prompt.text())
                    field.fill('/gen "글칸 없음" 입력')
                    field.press('Control+Enter')
                    expect(page.locator('#board-command-error')).to_contain_text('프롬프트 칸이 없어요')
                    expect(page.locator('.board-node-gen')).to_have_count(2)
                    field.fill('/gen "이미지 생성" 필수 입구 없음')
                    field.press('Enter')
                    expect(page.locator('#board-command-error')).to_contain_text('비어 있는 필수 입구')
                    expect(page.locator('.board-node-gen')).to_have_count(3)
                    self.assertEqual(len(page.request.get(base + '/api/jobs').json()['jobs']), jobs_before_exit + 1)
                    field.fill('/gen')
                    field.press('Enter')
                    expect(page.locator('#board-asset-picker-modal')).to_be_visible()
                    page.locator('#board-asset-picker-grid [data-pick]').filter(has_text='장면 테스트').click()
                    expect(page.locator('.board-node-gen')).to_have_count(4)
                    self.assertEqual(len(page.request.get(base + '/api/jobs').json()['jobs']), jobs_before_exit + 1)
                    field.fill('/gen "장면 비교"')
                    field.press('Enter')
                    expect(page.locator('.board-node-gen')).to_have_count(5)
                    self.assertEqual(len(page.request.get(base + '/api/jobs').json()['jobs']), jobs_before_exit + 1)
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
