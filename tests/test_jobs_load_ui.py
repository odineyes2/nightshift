"""잡 목록의 설정 불러오기를 격리한 실제 앱에서 검사하고 네 화면을 캡처한다."""
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from capture_ui import wait_server, isolated_environment, login, wait_screen
from capture_ui_support import (BOOTSTRAP, child_environment, copy_sources, free_port,
                                local_request, stop_process, tracked_files)


def main():
    output = ROOT / '.ui-captures'
    output.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='jobs-load-', dir=output)).resolve()
    copy_sources(ROOT, tracked_files(ROOT), directory / 'src')
    env = child_environment(directory)
    port = free_port()
    base = f'http://127.0.0.1:{port}'
    bootstrap = directory / 'capture_app.py'
    bootstrap.write_text(BOOTSTRAP, encoding='utf-8')
    template = dict(id='fixture', label='복원 검사', requires_csv=True, options=[
        dict(name='seed_count', label='시드 개수', type='number', default=1)])
    jobs = [dict(id=str(i), status=status, template_id='fixture', template_label='복원 검사',
                 options={'seed_count': i + 10}, workflow_original_name=f'workflow-{i}.json',
                 csv_original_name=f'rows-{i}.csv')
            for i, status in [(1, 'queued'), (2, 'pending')]]
    mutations = []
    reads = []
    process = None
    with (directory / 'server.log').open('w', encoding='utf-8') as log:
        try:
            process = subprocess.Popen([sys.executable, str(bootstrap), str(ROOT / 'scripts'),
                                        str(directory / 'src/server'), str(port)],
                                       cwd=directory, env=env, stdout=log, stderr=log)
            wait_server(process, base)
            from playwright.sync_api import sync_playwright, expect
            with isolated_environment(env, directory / 'tmp'), sync_playwright() as pw:
                browser = pw.chromium.launch(env=env)
                try:
                    for theme in ('light', 'dark'):
                        for width in (1300, 390):
                            context = browser.new_context(viewport={'width': width, 'height': 850},
                                                          color_scheme=theme, service_workers='block')
                            try:
                                login(context, base, env)
                                def route(r):
                                    request = r.request
                                    if not local_request(request.url, base):
                                        return r.abort()
                                    path = urlsplit(request.url).path
                                    if path == '/api/validate-workflow' and request.method == 'POST':
                                        return r.fulfill(json={'auto': True})
                                    if request.method != 'GET':
                                        mutations.append((request.method, path))
                                        return r.abort()
                                    if path == '/api/jobs':
                                        return r.fulfill(json={'jobs': jobs, 'pending_count': 2, 'running': False, 'pods': {}})
                                    if path == '/api/templates':
                                        return r.fulfill(json=[template])
                                    if path.startswith('/api/jobs/'):
                                        reads.append(path)
                                        if path.endswith('/workflow'):
                                            return r.fulfill(json={'fixture': {'class_type': 'Fixture', 'inputs': {}}})
                                        if path.endswith('/csv'):
                                            return r.fulfill(body='prompt,seed\nfixture,42\n', content_type='text/csv')
                                        raise AssertionError('unexpected job request: ' + path)
                                    return r.continue_()
                                context.route('**/*', route)
                                context.add_init_script(f"localStorage.setItem('nightshift-theme', '{theme}')")
                                page = context.new_page()
                                page.goto(base + '/#jobs')
                                wait_screen(page, 'jobs', None, None)
                                buttons = page.locator('.job-card-actions .load-btn')
                                expect(buttons).to_have_count(2)
                                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                                for button in buttons.all():
                                    box = button.bounding_box()
                                    assert box['width'] >= 40 and box['height'] >= 40
                                    assert box['x'] >= 0 and box['x'] + box['width'] <= width
                                    assert button.get_attribute('title') == '설정 불러오기'
                                assert page.evaluate('''() => [...document.querySelectorAll('.job-card-actions')].every(a => {
                                    const boxes = [...a.querySelectorAll('button')].map(b => b.getBoundingClientRect());
                                    return boxes.every((b,i) => boxes.slice(i+1).every(c => b.right <= c.left || c.right <= b.left || b.bottom <= c.top || c.bottom <= b.top));
                                })''')
                                page.screenshot(path=str(directory / f'{theme}-{width}.png'), full_page=True)
                                for job in jobs:
                                    page.get_by_role('button', name=f"작업 {job['id']} 설정 불러오기", exact=True).click()
                                    expect(page.locator('#new-job-modal')).to_be_visible()
                                    expect(page.locator('#job-detail-modal')).not_to_be_visible()
                                    expect(page.locator('#template-select')).to_have_value('fixture')
                                    expect(page.locator('#options-fields [data-name="seed_count"]')).to_have_value(str(job['options']['seed_count']))
                                    assert page.evaluate('selectedFiles.workflow.name') == job['workflow_original_name']
                                    assert page.evaluate('selectedFiles.csv.name') == job['csv_original_name']
                                    assert 'fixture,42' in page.evaluate('selectedFiles.csv.text()')
                                    expect(page.locator('#load-notice')).to_contain_text(job['id'])
                                    page.locator('#new-job-modal-close').click()
                                    page.evaluate('''() => { document.activeElement.blur(); lastJobsRowsSignature = null; return fetchJobs(); }''')
                                assert not mutations, mutations
                                assert all(f"/api/jobs/{j['id']}/{kind}" in reads for j in jobs for kind in ('workflow', 'csv'))
                            finally:
                                context.close()
                finally:
                    browser.close()
        finally:
            if process is not None:
                stop_process(process)
    print('PASS: restoration, rerender, no submission, 40x40 and layout; captures: ' + str(directory))


if __name__ == '__main__':
    main()
