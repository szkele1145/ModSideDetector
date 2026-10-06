/**
 * 把 封面.html 里的两个版面分别截成封面图（各自适配，不是拉伸）
 *   16:9 → dist/cover-16x9.png (1920×1080)
 *    4:3 → dist/cover-4x3.png  (1440×1080)
 * 另存 JPG（quality 94，确认 <5MB）
 * 用法：node shoot-cover.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const puppeteer = require('C:\\Users\\一只屑\\Desktop\\ModSync\\宣传片\\node_modules\\puppeteer-core');

const EDGE_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'
];
const HERE = __dirname;
const HTML = path.join(HERE, '封面.html');
const DIST = path.join(HERE, 'dist');

function findEdge(){
  for (const p of EDGE_CANDIDATES) if (fs.existsSync(p)) return p;
  throw new Error('找不到 Edge');
}

(async () => {
  fs.mkdirSync(DIST, { recursive: true });
  const edge = findEdge();
  const browser = await puppeteer.launch({
    executablePath: edge, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--hide-scrollbars', '--force-color-profile=srgb']
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 1 });
  await page.goto('file:///' + HTML.replace(/\\/g, '/'), { waitUntil: 'networkidle0' });
  await new Promise(r => setTimeout(r, 1200));      // 等内嵌字体与渐变稳定

  const jobs = [
    { id: 'c16', w: 1920, h: 1080, name: 'cover-16x9' },
    { id: 'c43', w: 1440, h: 1080, name: 'cover-4x3'  }
  ];
  for (const j of jobs){
    const el = await page.$('#' + j.id);
    if (!el) throw new Error('找不到版面 #' + j.id);
    const png = path.join(DIST, j.name + '.png');
    const jpg = path.join(DIST, j.name + '.jpg');
    await el.screenshot({ path: png, type: 'png' });
    await el.screenshot({ path: jpg, type: 'jpeg', quality: 94 });
    const box = await el.boundingBox();
    const kb = f => (fs.statSync(f).size / 1024).toFixed(0) + ' KB';
    console.log(`${j.name}: CSS ${box.width}×${box.height} → PNG ${kb(png)} / JPG ${kb(jpg)}`);
  }
  await browser.close();
})().catch(e => { console.error('失败:', e.message); process.exit(1); });
