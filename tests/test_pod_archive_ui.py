"""Workers 보관·복원과 폴링·실패·반응형 접근을 저장소 화면과 모의 API로 검사한다."""
import re
import os
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
HTML = re.sub(r'<script\b[^>]*>.*?</script>', '', (ROOT / 'static/index.html').read_text(encoding='utf-8'), flags=re.S)
HTML = re.sub(r'<link\b[^>]*>', '', HTML)
SCRIPT = (ROOT / 'static/js/08-pods.js').read_text(encoding='utf-8')
CSS = '\n'.join(p.read_text(encoding='utf-8') for p in sorted((ROOT / 'static/css').glob('*.css')))
STUBS = """
let podsById = {}, currentUser = {id:1}, currentTab = 'pods', currentPodId = null;
let lastJobs = [{pod_id:'remote', status:'done', template_id:'retained-job'}];
const STATUS_LABEL = {done:'완료'};
const escapeHtml = s => String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const ico = name => `<svg class="ico"><use href="#i-${name}"/></svg>`;
const isAdminUser = () => true;
const fmtGalleryDateTime = () => ''; const podName = id => id;
const updatePodBarStatus = () => {}; const fetchProjects = () => {};
const podSubtabs = () => ['psettings']; const podDefaultTab = () => 'psettings';
window.__nightshiftMediaUrl = s => s;
async function fetchPods(){ const r = await fetch('/api/pods'); const d = await r.json(); podsById = Object.fromEntries(d.map(p => [p.id,p])); }
function showTab(tab, opts={}){
  currentTab=tab; currentPodId=opts.podId || null;
  document.querySelectorAll('main').forEach(el => el.style.display='none');
  if(tab==='psettings'){ document.getElementById('tab-psettings').style.display='block'; openPodSettingsPage(opts.podId); }
  else { closePodSettingsPage(); document.getElementById('tab-dashboard').style.display='block'; renderDashboard(); }
}
"""


def check_action(page, label, icon, busy=False):
    button = page.locator('#pod-edit-archive')
    expect(button).to_have_text('')
    expect(button).to_have_attribute('title', label)
    expect(button).to_have_attribute('aria-label', label)
    expect(button).to_have_attribute('aria-busy', str(busy).lower())
    expect(button).to_have_attribute('aria-describedby', 'pod-archive-description')
    expect(button.locator('svg')).to_have_attribute('aria-hidden', 'true')
    expect(button.locator('use')).to_have_attribute('href', '#i-' + icon)
    if busy:
        expect(button).to_be_disabled()
        expect(button.locator('svg')).to_have_class('ico ico-spin')
    else:
        expect(button).to_be_enabled()


def check_layout(page):
    delete = page.locator('#pod-edit-delete')
    archive = page.locator('#pod-edit-archive')
    a, b = delete.bounding_box(), archive.bounding_box()
    assert a['width'] >= 40 and a['height'] >= 40
    assert b['width'] >= 40 and b['height'] >= 40
    assert abs(a['y'] - b['y']) < 1 and 0 <= b['x'] - (a['x'] + a['width']) <= 12
    assert page.evaluate("document.getElementById('pod-edit-delete').nextElementSibling.id") == 'pod-edit-archive'
    for selector in ('#pod-edit-test', '#pod-edit-runpod-test', '#pod-edit-save'):
        other = page.locator(selector)
        if other.is_visible():
            c = other.bounding_box()
            assert c['x'] >= b['x'] + b['width'] or c['y'] >= b['y'] + b['height']
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    delete.focus()
    page.keyboard.press('Tab')
    expect(archive).to_be_focused()
    assert archive.evaluate("el => getComputedStyle(el).outlineStyle") != 'none'


def main():
    with sync_playwright() as pw, tempfile.TemporaryDirectory(prefix='ns-archive-ui-') as captures:
        # Uses installed Chromium only. No install/download command is issued.
        browser = pw.chromium.launch()
        try:
            for width in (390, 1300):
                for theme in ('light', 'dark'):
                    page = browser.new_page(viewport={'width': width, 'height': 900})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)+error.stack))
                    pods = [dict(id='remote',name='Remote worker',url='https://worker.example',kind='comfyui',enabled=True,archived=False,owner_id=1,max_concurrent=1),
                            dict(id='local',name='Local worker',url='http://localhost:8188',kind='comfyui',enabled=False,archived=True,owner_id=1,max_concurrent=1)]
                    requests = []
                    state = {'fail': False, 'hold': False, 'pending': None}
                    def api(route):
                        req = route.request
                        path = req.url.split('/api/')[1]
                        if req.method == 'GET':
                            if path == 'pods': route.fulfill(json=pods)
                            elif path == 'runpod/tiers': route.fulfill(json={})
                            elif path == 'pods/summary': route.fulfill(json={'pods': pods, 'totals': {'pods':len(pods),'online':1}})
                            else: raise AssertionError(path)
                        else:
                            requests.append((req.method, path, req.post_data_json))
                            assert req.method == 'PUT' and path in ('pods/remote','pods/local'), requests
                            assert set(req.post_data_json) == {'archived'}, requests
                            if state['fail']: route.fulfill(status=400,json={'detail':'저장 실패: 다시 시도해 주세요.'})
                            elif state['hold']: state['pending'] = route
                            else:
                                next(p for p in pods if p['id']==path.split('/')[-1]).update(req.post_data_json)
                                route.fulfill(json={'ok':True})
                    page.route('**/api/**',api)
                    page.route('http://127.0.0.1/',lambda route:route.fulfill(body=HTML,content_type='text/html'))
                    page.route(re.compile(r'https://.*'), lambda route: route.abort())
                    page.goto('http://127.0.0.1/')
                    page.add_style_tag(content=CSS)
                    page.add_script_tag(content=STUBS+SCRIPT)
                    assert not errors, errors
                    page.evaluate("async theme => {document.documentElement.dataset.theme=theme; localStorage.setItem(SHOW_LOCAL_KEY,'0'); await fetchPods(); await fetchDashboard(); showTab('pods');}", theme)
                    assert page.evaluate('dashArchived') is False
                    cards = page.locator('[data-card-type="pod"]')
                    expect(cards).to_have_count(1)
                    page.locator('[data-pod-edit="remote"]').first.click()
                    check_action(page, '보관', 'archive')
                    check_layout(page)
                    state['fail'] = True
                    page.locator('#pod-edit-archive').click()
                    expect(page.locator('#pod-edit-error')).to_contain_text('저장 실패')
                    check_action(page, '보관', 'archive')
                    assert not pods[0]['archived']
                    assert page.evaluate('currentTab') == 'psettings'
                    state.update(fail=False,hold=True)
                    page.locator('#pod-edit-archive').click()
                    check_action(page, '저장 중…', 'loader-circle', busy=True)
                    page.evaluate("setPodArchived('remote',true)")
                    assert len(requests)==2
                    pods[0]['archived']=True
                    state['pending'].fulfill(json={'ok':True})
                    state['hold']=False
                    expect(page.locator('#pod-archive-notice')).to_contain_text('보관했어요')
                    expect(cards).to_have_count(0)
                    expect(page.locator('#dash-empty')).to_be_visible()
                    assert page.locator('[data-card-type="recent-jobs"]').count()==1
                    page.locator('#pod-archive-notice button').click()
                    expect(cards).to_have_count(1)
                    assert not pods[0]['archived']
                    page.locator('[data-pod-edit="remote"]').first.click()
                    page.locator('#pod-edit-archive').click()
                    expect(cards).to_have_count(0)
                    page.locator('#dash-archive-filter').click()
                    expect(cards).to_have_count(2)
                    # Summary polling keeps archive scope and exposes archived local workers.
                    page.evaluate('fetchDashboard()')
                    expect(page.locator('#dash-archive-filter')).to_have_attribute('aria-pressed','true')
                    expect(cards).to_have_count(2)
                    for selector in ('#dash-archive-filter','[data-pod-restore="local"]'):
                        box=page.locator(selector).bounding_box()
                        assert box['width']>=40 and box['height']>=40
                    capture_dir = Path(os.environ.get('NS_ARCHIVE_CAPTURE_DIR', captures))
                    capture_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(capture_dir/f'{width}-{theme}-archive.png'),full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width,theme)
                    state['fail'] = True
                    page.locator('[data-pod-restore="local"]').click()
                    expect(page.locator('#pod-archive-notice')).to_contain_text('저장 실패')
                    expect(cards).to_have_count(2)
                    assert page.evaluate('dashArchived') is True
                    state['fail'] = False
                    page.locator('[data-pod-restore="local"]').click()
                    expect(cards).to_have_count(1)
                    assert page.evaluate('currentTab')=='pods'
                    # Settings also provides restore for direct worker routes.
                    page.locator('[data-pod-edit="remote"]').first.click()
                    check_action(page, '복원', 'undo-2')
                    check_layout(page)
                    state['fail'] = True
                    page.keyboard.press('Enter')
                    expect(page.locator('#pod-edit-error')).to_contain_text('저장 실패')
                    check_action(page, '복원', 'undo-2')
                    state['fail'] = False
                    box=page.locator('#pod-edit-archive').bounding_box()
                    assert box['height']>=40
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(capture_dir/f'{width}-{theme}-settings.png'),full_page=True)
                    check_layout(page)
                    page.keyboard.press('Space')
                    expect(cards).to_have_count(0)
                    expect(page.locator('#dash-empty')).to_have_text('보관한 워커가 없어요.')
                    page.locator('#dash-archive-filter').click()
                    expect(cards).to_have_count(1)  # restored local is still hidden by normal local filter
                    assert not any(p['archived'] for p in pods)
                    assert all(p['enabled']==(p['id']=='remote') for p in pods)
                    page.evaluate('openPodEditModal(null)')
                    expect(page.locator('#pod-edit-archive')).to_be_hidden()
                    expect(page.locator('#pod-archive-action')).to_be_hidden()
                    expect(page.locator('#pod-edit-delete')).to_be_hidden()
                    assert page.locator('#pod-edit-archive').count() == 1
                    # A fresh page/script starts in the regular list.
                    assert not errors, errors
                    page.close()
                    print(f'ok: {width}px {theme}; archive/undo/restore/failure/duplicate/polling/layout')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
