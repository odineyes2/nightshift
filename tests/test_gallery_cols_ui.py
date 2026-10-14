"""갤러리·영상 격자 열 개수 슬라이더 정적 검사 (NS-76-1)."""
import pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "static"
html = (S / "index.html").read_text(encoding="utf-8")
css = (S / "css" / "06-models-db.css").read_text(encoding="utf-8")
js = (S / "js" / "15-gallery.js").read_text(encoding="utf-8")

assert 'id="gallery-cols-range"' in html and 'id="video-gallery-cols-range"' in html
assert "--gal-cols" in css and "html[data-gal-cols] #gallery-grid" in css
assert "@media (min-width:641px)" in css
assert "'galleryCols'" in js and "querySelectorAll('.gallery-cols-range')" in js
assert "localStorage.setItem(GALLERY_COLS_KEY" in js
print("ok")
