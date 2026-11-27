"""Builds web/facility-dashboard.html from web-src/facility-dashboard.template.html.

The province and district outlines embedded in the page are carried over from the current
web/facility-dashboard.html, so a rebuild never changes the map. Run: python web-src/build_dashboard.py
"""
import json
import pathlib

root = pathlib.Path(__file__).resolve().parent.parent
out_path = root / 'web' / 'facility-dashboard.html'
tpl = (root / 'web-src' / 'facility-dashboard.template.html').read_text(encoding='utf-8')
current = out_path.read_text(encoding='utf-8')


def grab(name):
    i = current.index(f'const {name}=') + len(name) + 7
    return json.dumps(json.JSONDecoder().raw_decode(current[i:])[0], separators=(',', ':'))


out = tpl.replace('__PROVINCES__', grab('PROVINCES')).replace('__DISTRICTS__', grab('DISTRICTS'))
out_path.write_text(out, encoding='utf-8', newline='')
print('web/facility-dashboard.html', len(out) // 1024, 'KB')
