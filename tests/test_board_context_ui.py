"""격리 서버의 보드 빈 공간 메뉴와 빈 카드 생성·복사·되돌리기를 브라우저에서 검사한다."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from capture_ui import isolated_environment, login, wait_server
from capture_ui_support import BOOTSTRAP, child_environment, copy_sources, free_port, local_request, stop_process, tracked_files


class BoardContextUITests(unittest.TestCase):
    def test_empty_context_cards_and_history(self):
        from playwright.sync_api import sync_playwright

        (ROOT / '.ui-captures').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / '.ui-captures') as tmp:
            directory = Path(tmp)
            copy_sources(ROOT, tracked_files(ROOT), directory / 'src')
            env = child_environment(directory)
            port = free_port()
            base = f'http://127.0.0.1:{port}'
            (directory / 'capture_app.py').write_text(BOOTSTRAP, encoding='utf-8')
            with (directory / 'server.log').open('w', encoding='utf-8') as log:
                process = subprocess.Popen([sys.executable, str(directory / 'capture_app.py'), str(ROOT / 'scripts'),
                                            str(directory / 'src' / 'server'), str(port)],
                                           cwd=directory, env=env, stdout=log, stderr=log)
                try:
                    wait_server(process, base)
                    with isolated_environment(env, directory / 'tmp'), sync_playwright() as playwright:
                        browser = playwright.chromium.launch(env=env)
                        try:
                            context = browser.new_context(service_workers='block', viewport={'width': 1300, 'height': 850})
                            context.route('**/*', lambda route: route.continue_() if local_request(route.request.url, base) else route.abort())
                            login(context, base, env)
                            created = context.request.post(base + '/api/projects',
                                                           headers={'x-requested-with': 'nightshift',
                                                                    'content-type': 'application/json'},
                                                           data=json.dumps({'name': 'Context UI'})).json()
                            project_id = created['id']
                            page = context.new_page()
                            page.goto(f'{base}/#project/{project_id}/prboard')
                            page.wait_for_function("() => currentTab === 'prboard' && currentBoardId !== null")
                            viewport = page.locator('#board-viewport')
                            for index, (kind, label) in enumerate([('text', '빈 텍스트 카드'), ('image', '빈 이미지 카드'),
                                                                    ('video', '빈 영상 카드'), ('job', '빈 작업 카드')]):
                                box = viewport.bounding_box()
                                x, y = box['x'] + 80 + index * 280, box['y'] + 100
                                page.mouse.click(x, y, button='right')
                                menu = page.locator('#board-ctx-menu')
                                self.assertEqual(menu.locator('[data-ctx^="new-"]').count(), 4)
                                self.assertTrue(menu.get_by_text(label).is_visible())
                                world = page.evaluate('''([x,y]) => { const r = document.getElementById('board-viewport').getBoundingClientRect();
                                    return {x:(x-r.left-boardState.x)/boardState.scale, y:(y-r.top-boardState.y)/boardState.scale}; }''', [x, y])
                                menu.get_by_text(label).click()
                                page.wait_for_function('(kind) => boardNodes.some(n => n.kind === kind)', arg=kind)
                                card = page.evaluate('(kind) => boardNodes.find(n => n.kind === kind)', kind)
                                self.assertLess(abs(card['x'] - world['x']), 1)
                                self.assertLess(abs(card['y'] - world['y']), 1)
                                self.assertEqual(card['text'], '')
                                if kind in ('image', 'video'):
                                    self.assertIsNone(card['asset_path'])
                                if kind == 'job':
                                    self.assertIsNone(card['job_id'])
                                    page.locator(f'[data-node-id="{card["id"]}"] [data-board-fill]').click()
                                    self.assertTrue(page.locator('#board-asset-picker-modal').is_visible())
                                    page.locator('#board-asset-picker-close').click()
                            page.mouse.click(box['x'] + 24, box['y'] + 24, button='right')
                            self.assertTrue(page.locator('#board-ctx-menu [data-ctx="copy"]').count() == 0)
                            page.keyboard.press('Escape')
                            self.assertEqual(page.locator('#board-ctx-menu').count(), 0)
                            page.evaluate('() => copyBoardNodes([boardNodes[0].id])')
                            page.mouse.click(box['x'] + 28, box['y'] + 28, button='right')
                            self.assertTrue(page.locator('#board-ctx-menu [data-ctx="paste"]').is_visible())
                            count = page.evaluate('() => boardNodes.length')
                            page.locator('#board-ctx-menu [data-ctx="paste"]').click()
                            page.wait_for_function('(count) => boardNodes.length === count + 1', arg=count)
                            page.evaluate('() => boardUndoRedo("undo")')
                            page.wait_for_function('(count) => boardNodes.length === count', arg=count)
                            stored = context.request.get(f'{base}/api/projects/{project_id}/board').json()['nodes']
                            self.assertEqual(len(stored), 4)
                            shots = ROOT / '.ui-captures' / 'NS-100-1'
                            shots.mkdir(exist_ok=True)
                            for theme in ('light', 'dark'):
                                for width, height in ((1300, 850), (390, 800)):
                                    shot_context = browser.new_context(service_workers='block', color_scheme=theme,
                                                                       viewport={'width': width, 'height': height})
                                    shot_context.route('**/*', lambda route: route.continue_() if local_request(route.request.url, base) else route.abort())
                                    shot_context.add_init_script(f"localStorage.setItem('nightshift-theme', '{theme}')")
                                    login(shot_context, base, env)
                                    shot_page = shot_context.new_page()
                                    shot_page.goto(f'{base}/#project/{project_id}/prboard')
                                    shot_page.wait_for_function("() => currentTab === 'prboard' && boardNodes.length === 4")
                                    if width == 390 and not shot_page.locator('#board-tree').evaluate('(el) => el.hidden'):
                                        shot_page.locator('#board-tree-toggle').click()
                                    shot_box = shot_page.locator('#board-viewport').bounding_box()
                                    shot_page.evaluate('([x,y]) => openBoardCtxMenu(x,y,null)',
                                                       [shot_box['x'] + 20, shot_box['y'] + 80])
                                    self.assertTrue(shot_page.locator('#board-ctx-menu [data-ctx="new-job"]').is_visible())
                                    self.assertLessEqual(shot_page.evaluate('() => document.documentElement.scrollWidth'), width)
                                    source = directory / f'{theme}-{width}.png'
                                    shot_page.screenshot(path=str(source), full_page=True)
                                    shutil.copyfile(source, shots / source.name)
                                    shot_context.close()
                            context.close()
                        finally:
                            browser.close()
                finally:
                    stop_process(process)


if __name__ == '__main__':
    unittest.main()
