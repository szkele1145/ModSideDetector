/* =============================================================================
   ModSideDetector 宣传片 · 自动录制（阶段二）
     · 视频轨只录一次（画面与音色无关），男女声各自复用同一批帧
     · 逐帧 seekTo(i * 1000/30) → screenshot
     · 录制前用 page.addStyleTag 强制隐藏 UI（不靠人工按键）

   用法:
     node record.js --frames          只逐帧截图（保留在 work/frames/）
     node record.js --silent          只合成无声视频 → dist/_silent.mp4
     node record.js --voice male      混男声 → dist/ModSideDetector-宣传片.mp4
     node record.js --voice female    混女声 → dist/ModSideDetector-宣传片-女声.mp4
     node record.js --all             一次跑完（截图 → 无声 → 男女声两版）
     node record.js --test 45         --frames 的快速冒烟（只录 45 帧）
   ========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const puppeteer = require('C:\\Users\\一只屑\\Desktop\\ModSync\\宣传片\\node_modules\\puppeteer-core');

const FPS = 30;
const W = 1920, H = 1080;
const HERE = __dirname;
const HTML_FILE = path.join(HERE, 'ModSideDetector-宣传片.html');
const DIST = path.join(HERE, 'dist');
const FRAMES = path.join(HERE, 'work', 'frames');
const SILENT = path.join(DIST, '_silent.mp4');
const OUT = {
  male: path.join(DIST, 'ModSideDetector-宣传片.mp4'),
  female: path.join(DIST, 'ModSideDetector-宣传片-女声.mp4')
};
const EDGE = process.env.EDGE_PATH || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const FFMPEG = process.env.FFMPEG ||
  'C:\\Users\\一只屑\\Desktop\\ModSync\\宣传片\\node_modules\\ffmpeg-static\\ffmpeg.exe';

const argv = process.argv.slice(2);
const has = k => argv.includes(k);
const val = (k, d) => { const i = argv.indexOf(k); return i >= 0 && argv[i + 1] ? argv[i + 1] : d; };
const log = (...a) => console.log('[' + new Date().toTimeString().slice(0, 8) + ']', ...a);
const die = m => { console.error('\n✗ ' + m); process.exit(1); };

function checkEnv(){
  if (!fs.existsSync(EDGE)) die('找不到 Edge: ' + EDGE);
  if (!fs.existsSync(FFMPEG)) die('找不到 ffmpeg: ' + FFMPEG);
  if (!fs.existsSync(HTML_FILE)) die('找不到源文件: ' + HTML_FILE);
  fs.mkdirSync(DIST, { recursive: true });
}

/* ------------------------------------------------------------------ 逐帧截图 */
async function shoot(){
  const TEST = parseInt(val('--test', '0'), 10) || 0;
  fs.rmSync(FRAMES, { recursive: true, force: true });
  fs.mkdirSync(FRAMES, { recursive: true });

  const browser = await puppeteer.launch({
    executablePath: EDGE, headless: true,
    args: ['--autoplay-policy=no-user-gesture-required', '--hide-scrollbars',
           '--force-color-profile=srgb', '--force-device-scale-factor=1',
           '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
           '--disable-backgrounding-occluded-windows', '--font-render-hinting=none',
           '--mute-audio', '--no-first-run', '--no-default-browser-check'],
    defaultViewport: { width: W, height: H, deviceScaleFactor: 1 }
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: W, height: H, deviceScaleFactor: 1 });
    const url = 'file:///' + HTML_FILE.replace(/\\/g, '/') + '?record=1';
    log('打开:', url);
    await page.goto(url, { waitUntil: 'load' });
    // 强制隐藏所有 UI 控件与滚动条（规范 5.4）
    await page.addStyleTag({ content:
      '#hud{display:none !important}' +
      '#prog{display:none !important}' +
      'html,body{overflow:hidden !important;scrollbar-width:none}' +
      '::-webkit-scrollbar{display:none !important;width:0 !important;height:0 !important}' });
    await page.evaluate(() => window.__sync && window.__sync.hideHud());

    const total = await page.evaluate(() => window.__sync.total);
    let frames = Math.round(total / 1000 * FPS);
    if (TEST) frames = Math.min(frames, TEST);
    log(`时间轴 ${total} ms → ${frames} 帧 @ ${FPS}fps (= ${(frames / FPS).toFixed(3)} s)`);

    const t0 = Date.now();
    for (let i = 0; i < frames; i++){
      const ms = i * (1000 / FPS);
      await page.evaluate(m => new Promise(res => {
        window.__sync.seekTo(m);
        requestAnimationFrame(() => requestAnimationFrame(res));
      }), ms);
      await page.screenshot({ path: path.join(FRAMES, 'f' + String(i).padStart(5, '0') + '.png'), type: 'png' });
      if (i % 120 === 0 || i === frames - 1){
        const eta = i ? ((Date.now() - t0) / (i + 1) * (frames - i - 1) / 1000).toFixed(0) : '?';
        log(`  帧 ${i + 1}/${frames} (${((i + 1) / frames * 100).toFixed(1)}%) 剩余约 ${eta}s`);
      }
    }
    log(`截图完成，用时 ${((Date.now() - t0) / 1000).toFixed(1)}s → ${FRAMES}`);
  } finally {
    await browser.close();
  }
}

/* ------------------------------------------------------------ 合成无声视频 */
function buildSilent(){
  if (!fs.existsSync(FRAMES)) die('没有帧目录，请先跑 --frames');
  const n = fs.readdirSync(FRAMES).filter(f => f.endsWith('.png')).length;
  log(`用 ${n} 帧合成无声视频 …`);
  const r = spawnSync(FFMPEG, ['-y', '-hide_banner', '-loglevel', 'error',
    '-framerate', String(FPS), '-start_number', '0', '-i', path.join(FRAMES, 'f%05d.png'),
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
    '-pix_fmt', 'yuv420p', '-r', String(FPS), '-movflags', '+faststart', SILENT
  ], { stdio: 'inherit' });
  if (r.status !== 0) die('ffmpeg 合成失败');
  log('无声视频: ' + SILENT);
}

function audioLen(file){
  const r = spawnSync(FFMPEG, ['-hide_banner', '-i', file], { encoding: 'utf-8' });
  const m = (r.stderr || '').match(/Duration: (\d+):(\d+):([\d.]+)/);
  return m ? +m[1] * 3600 + +m[2] * 60 + parseFloat(m[3]) : 0;
}
function durOf(file){
  const r = spawnSync(FFMPEG, ['-hide_banner', '-i', file], { encoding: 'utf-8' });
  const m = (r.stderr || '').match(/Duration: (\d+):(\d+):([\d.]+)/);
  return m ? +m[1] * 3600 + +m[2] * 60 + parseFloat(m[3]) : 0;
}

/* ------------------------------------------------------------------ 混入配音 */
function mux(voice){
  const man = JSON.parse(fs.readFileSync(path.join(HERE, 'audio', 'manifest.json'), 'utf-8'));
  const shots = man.shots[voice];
  if (!shots) die('manifest 里没有音色 ' + voice);
  if (!fs.existsSync(SILENT)) die('缺少 ' + SILENT + '，请先跑 --silent');
  const vdur = durOf(SILENT);

  const inputs = [], filters = [];
  let k = 0;
  for (const s of shots){
    const f = path.join(HERE, 'audio', voice, s.file);
    if (!fs.existsSync(f)) { log('  跳过缺失音频: ' + s.file); continue; }
    k++;
    /* 关键：ffmpeg 默认 mono→stereo 上混会衰减 3dB。
       这里用 pan 以单位增益把单声道复制到双声道，再统一 +2dB，
       让成品峰值贴近 -2dBFS（上传友好且不削波）。 */
    const chain = 'aresample=48000,aformat=sample_fmts=fltp:channel_layouts=mono,' +
                  'pan=stereo|c0=c0|c1=c0,volume=2dB,adelay=' + s.start + '|' + s.start;
    inputs.push('-i', f);
    filters.push(`[${k}:a]${chain}[a${k}]`);
  }
  if (!k) die('没有可用音频文件');

  const mixIn = Array.from({ length: k }, (_, i) => `[a${i + 1}]`).join('');
  const fc = filters.join(';') + ';' + mixIn +
             `amix=inputs=${k}:duration=longest:normalize=0,apad,` +
             `aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[aout]`;

  log(`混音: ${k} 段 ${voice}，视频 ${vdur.toFixed(3)}s`);
  const r = spawnSync(FFMPEG, ['-y', '-hide_banner', '-loglevel', 'error',
    '-i', SILENT, ...inputs, '-filter_complex', fc,
    '-map', '0:v', '-map', '[aout]',
    '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
    '-t', vdur.toFixed(3), '-movflags', '+faststart', OUT[voice]
  ], { stdio: 'inherit' });
  if (r.status !== 0) die('ffmpeg 混音失败');
  log('已输出 ' + voice + ' 版: ' + OUT[voice]);
}

/* ------------------------------------------------------------------ 主流程 */
(async () => {
  checkEnv();
  log('Edge  :', EDGE);
  log('ffmpeg:', FFMPEG);
  const ALL = has('--all');
  if (ALL || has('--frames')) await shoot();
  if (ALL || has('--silent')) buildSilent();
  if (ALL || has('--voice')) mux(val('--voice', 'male'));
  if (ALL){
    mux('male');
    mux('female');
  }
  if (!ALL && !has('--frames') && !has('--silent') && !has('--voice')){
    log('用法: node record.js --all | --frames | --silent | --voice male|female');
  }
})().catch(e => die(e && e.stack || String(e)));
