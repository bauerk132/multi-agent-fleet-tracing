"""Build-only bundler: embed the supplied dashboard's Tailwind CSS and font assets.
The resulting HTML has no build-tool or remote-resource dependency.
"""
import base64
import html
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent
original = Path(sys.argv[1])
target = ROOT / 'm8-node-graph-dashboard.html'
source = original.read_text(encoding='utf-8')
content = target.read_text(encoding='utf-8')
compiler = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const postcss = require('./.build/node_modules/postcss');
const tailwind = require('./.build/node_modules/tailwindcss');
const original = fs.readFileSync(process.argv[1], 'utf8');
const content = fs.readFileSync(process.argv[2], 'utf8');
const configScript = original.match(/<script id="tailwind-config">([\s\S]*?)<\/script>/)[1];
const sandbox = { globalThis: {} };
vm.runInNewContext(configScript, sandbox);
const config = sandbox.globalThis.tailwind.config;
// Resolve two undefined compact utility tokens already used by the Stitch markup.
config.theme.extend.fontSize['label-xs'] = ['10px', {lineHeight:'14px',letterSpacing:'0.2px',fontWeight:'500'}];
config.theme.extend.spacing = {'0.2':'0.05rem'};
config.content = [{ raw: content, extension:'html' }];
postcss([tailwind(config)]).process('@tailwind base; @tailwind components; @tailwind utilities;', {from:undefined})
  .then(result => process.stdout.write(result.css))
  .catch(error => { console.error(error); process.exitCode = 1; });
"""
css = subprocess.check_output(['node', '-e', compiler, str(original), str(target)], cwd=ROOT, text=True, encoding='utf-8')
cache = ROOT / '.build' / 'embedded-fonts.css'
if cache.exists():
    fonts = cache.read_text(encoding='utf-8')
else:
    urls = [html.unescape(url) for url in re.findall(r'<link[^>]+href="([^"]+)"[^>]+rel="stylesheet"[^>]*>', source)]
    fonts = ''
    for url in urls:
        sheet = urllib.request.urlopen(url, timeout=60).read().decode('utf-8')
        def embed(match):
            asset_url = match.group(1)
            response = urllib.request.urlopen(asset_url, timeout=60)
            asset = response.read()
            mime = 'font/woff2' if '.woff2' in asset_url else 'font/ttf'
            return 'url(data:' + mime + ';base64,' + base64.b64encode(asset).decode('ascii') + ')'
        fonts += re.sub(r'url\((https://[^)]+)\)', embed, sheet) + '\n'
    cache.write_text(fonts, encoding='utf-8')
# Keep the original markup and authored scrollbar/layout CSS intact.
content = re.sub(r'\s*<script src="https://cdn\.tailwindcss\.com[^\"]*"><\/script>', '', content)
content = re.sub(r'\s*<script id="tailwind-config">[\s\S]*?<\/script>', '', content)
content = re.sub(r'\s*<link[^>]+href="https://fonts\.(?:googleapis|gstatic)\.com[^>]*>', '', content)
content = re.sub(r'\s*<style id="offline-utilities">[\s\S]*?<\/style>', '', content)
content = content.replace('  <style>', '  <style id="offline-utilities">\n' + fonts + css + '\n  </style>\n  <style>', 1)
target.write_text(content, encoding='utf-8')
classes = sorted(set(token for value in re.findall(r'class="([^"]*)"', content) for token in value.split()))
(ROOT / '.build' / 'utility-inventory.json').write_text(json.dumps(classes, indent=2), encoding='utf-8')
assert not re.search(r'(?:src|href)="https?://', content)
assert not re.search(r'url\([\"\x27]?https?://', content)
print(f'Embedded {len(classes)} markup classes; HTML size {target.stat().st_size:,} bytes; no remote rendering resources.')
