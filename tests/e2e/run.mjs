#!/usr/bin/env node
/**
 * GUI 端到端回归 —— 用真浏览器驱动真实点击。
 *
 * ## 设计原则
 *
 * 1. **自己起 GUI、跑完自己关**：不依赖外部已有进程，一条命令能跑。
 * 2. **端口随机**：避免和用户正在用的 GUI 撞车（实测踩到过）。
 * 3. **每条断言都要有编号**：失败时能直接说出"第几条失败了"。
 * 4. **不留垃圾**：输出目录写进临时目录，跑完删掉。
 * 5. **退出码非零 = 失败**：可直接接 CI。
 *
 * ## 为什么不用 puppeteer 自带的 Chromium
 *
 * `puppeteer-core` 不下载浏览器（省 ~200MB），用机器上已有的 Chrome/Edge。
 * 没装浏览器时报错要说人话，而不是抛一堆 stack。
 */
import puppeteer from 'puppeteer-core';
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, rm, readdir } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const CHROME = process.env.CHROME_PATH ||
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const ROOT = path.resolve(import.meta.dirname, '../..');
const PY = process.env.KF_PY || path.join(ROOT, 'venv/bin/python');

const results = [];
let failures = 0;

function check(n, name, cond, detail = '') {
  const ok = !!cond;
  results.push({ n, name, ok, detail });
  console.log(`  ${ok ? 'PASS' : 'FAIL'} [${n}] ${name}${ok ? '' : '  ← ' + detail}`);
  if (!ok) failures++;
  return ok;
}

async function startGui(port) {
  const proc = spawn(PY, ['-m', 'kf.cli', '--gui', '--no-browser', '--port', String(port)], {
    cwd: ROOT,
    env: { ...process.env, PYTHONPATH: path.join(ROOT, 'src') },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let buf = '';
  const token = await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('GUI 30 秒内没起来:\n' + buf)), 30000);
    proc.stdout.on('data', d => {
      buf += d.toString();
      const m = buf.match(/token=([a-f0-9]+)/);
      if (m) { clearTimeout(t); resolve(m[1]); }
    });
    proc.stderr.on('data', d => { buf += d.toString(); });
    proc.on('exit', c => { clearTimeout(t); reject(new Error(`GUI 提前退出 code=${c}\n${buf}`)); });
  });
  return { proc, token };
}

async function main() {
  const port = Number(process.env.KF_PORT || (20000 + Math.floor(Math.random() * 20000)));
  // **临时目录必须建在项目目录之下**，不能用系统 tmpdir。
  // 原因：GUI 的输出目录白名单是「家目录或项目目录」，`os.tmpdir()` 给出的是
  // `/var/folders/...`，会被正确拒绝（这是安全设计，不该为了测试去放宽）。
  // 同时它已在 .gitignore 里（`eval/shots-*/`），跑完即删，不会污染仓库。
  await mkdir(path.join(ROOT, 'eval'), { recursive: true });
  const outDirTmp = await mkdtemp(path.join(ROOT, 'eval', 'shots-e2e-'));

  console.log(`\n=== KF-Extract GUI 端到端回归 ===`);
  console.log(`  端口 ${port} · 输出 ${outDirTmp}`);

  const gui = await startGui(port);
  const url = `http://127.0.0.1:${port}/?token=${gui.token}`;

  let browser;
  try {
    browser = await puppeteer.launch({
      executablePath: CHROME,
      headless: 'new',
      args: ['--no-sandbox', '--disable-gpu'],
    });
  } catch (e) {
    console.error(`\n❌ 起不来浏览器：${CHROME}\n${e.message}`);
    console.error('   装一个 Chrome/Edge，或用 CHROME_PATH=... 指定路径。');
    gui.proc.kill();
    process.exit(2);
  }

  const page = await browser.newPage();
  const jsErrors = [];
  const expectedHttpErrors = [];     // 我们**故意**触发的 4xx（如非法目录）
  page.on('pageerror', e => jsErrors.push(e.message));
  page.on('console', m => {
    if (m.type() !== 'error') return;
    const t = m.text();
    // 浏览器对失败的 fetch 会自动打一条 console error，内容是 "Failed to load resource: ... 404/400"。
    // 这类**不是 JS 代码错误**，是 HTTP 状态；而且我们有一条用例就是**故意**触发 400。
    // 必须分开记录，否则"无 JS 错误"这条永远是红的（假红、而且会训练人忽略它）。
    if (/Failed to load resource.*status of (400|404|403)/.test(t)) {
      expectedHttpErrors.push(t); return;
    }
    jsErrors.push('console: ' + t);
  });

  try {
    // ---------------------------------------------------------------- 1 首页
    const resp = await page.goto(url, { waitUntil: 'networkidle2' });
    check(1, '首页 HTTP 200', resp.status() === 200, `status=${resp.status()}`);
    const html = await page.content();
    check(2, '首页非空且 token 注入', html.length > 3000 && !html.includes('%%KF_TOKEN%%'),
      `len=${html.length}`);
    await page.waitForFunction(() => document.querySelectorAll('#video option').length > 3,
      { timeout: 20000 });
    check(3, '素材列表已加载', await page.evaluate(
      () => document.querySelectorAll('#video option').length > 3));

    // 选最小的素材（trailer），跑得快
    const picked = await page.evaluate(() => {
      const opts = [...document.querySelectorAll('#video option')];
      const t = opts.find(o => o.textContent.includes('trailer.mp4'));
      return t ? t.value : opts[0].value;
    });
    await page.select('#video', picked);

    // ---------------------------------------------------------------- 2 跑一个任务
    // **必须显式给临时输出目录**：留空会写到素材旁的 eval/shots/，
    // 那正是上次误把 84 个 png 提交进仓库的路径（DECISIONS D-043）。
    // e2e 测试绝不许污染仓库。
    await page.evaluate((out) => {
      document.querySelector('#k').value = '8';
      document.querySelector('#fps').value = '12';
      document.querySelector('#out').value = out;
    }, outDirTmp);
    await page.click('#go');
    await page.waitForFunction(
      () => document.querySelector('#hint').textContent.startsWith('完成'),
      { timeout: 180000 });
    await new Promise(r => setTimeout(r, 1200));

    const r1 = await page.evaluate(() => ({
      sheets: document.querySelectorAll('#sheets img').length,
      stats: [...document.querySelectorAll('.stat b')].map(b => b.textContent),
      k: document.querySelector('#stats .stat b')?.textContent,
    }));
    check(4, '第一个任务跑完且有统计', r1.stats.length >= 5, JSON.stringify(r1.stats));
    check(5, '产出了拼图', r1.sheets > 0, `sheets=${r1.sheets}`);

    // ---------------------------------------------------------------- 3 图片真的加载了
    const imgs = await page.evaluate(() => [...document.querySelectorAll('#sheets img, #frames img')]
      .map(i => ({ ok: i.complete && i.naturalWidth > 0, w: i.naturalWidth, src: i.src.slice(-40) })));
    const badImgs = imgs.filter(i => !i.ok);
    check(6, `所有图片真的加载（${imgs.length} 张）`, imgs.length > 0 && badImgs.length === 0,
      badImgs.length ? `坏了 ${badImgs.length} 张，首个: ${badImgs[0].src}` : '');

    // ---------------------------------------------------------------- 4 再跑一个，测历史
    await page.evaluate(() => { document.querySelector('#k').value = '16'; });
    await page.click('#go');
    await page.waitForFunction(
      () => document.querySelector('#hint').textContent.startsWith('完成'),
      { timeout: 180000 });
    await new Promise(r => setTimeout(r, 1500));

    const jobs = await page.evaluate(() => [...document.querySelectorAll('#jobs .job')]
      .map(j => ({
        nm: j.querySelector('.nm').textContent.trim(),
        meta: j.querySelector('.meta').textContent.trim(),
      })));
    check(7, '任务历史有 2 条', jobs.length === 2, `实际 ${jobs.length}: ${JSON.stringify(jobs)}`);
    check(8, '历史按时间倒序（新的在前）',
      jobs.length === 2 && jobs[0].meta.includes('k=16') && jobs[1].meta.includes('k=8'),
      JSON.stringify(jobs.map(j => j.meta)));

    // 点回旧任务（k=8 那条）
    await page.evaluate(() => {
      const js = [...document.querySelectorAll('#jobs .job')];
      js[js.length - 1].click();
    });
    await new Promise(r => setTimeout(r, 1200));
    const back = await page.evaluate(() => ({
      hint: document.querySelector('#hint').textContent,
      k: document.querySelector('#stats .stat b')?.textContent,
    }));
    check(9, '点回旧任务恢复了它的结果（不重跑）',
      back.k === '8' && back.hint.includes('恢复'), JSON.stringify(back));

    // ---------------------------------------------------------------- 5 输出目录错误
    await page.evaluate(() => { document.querySelector('#out').value = '/etc/shots-kf-test'; });
    await page.click('#go');
    await new Promise(r => setTimeout(r, 1500));
    const badOut = await page.evaluate(() => ({
      hint: document.querySelector('#hint').textContent,
      focused: document.activeElement.id,
    }));
    check(10, '非法输出目录给出可读中文原因', badOut.hint.includes('输出目录不可用'), badOut.hint);
    check(11, '错误后焦点回到输出目录输入框', badOut.focused === 'out', `focused=${badOut.focused}`);
    await page.evaluate(() => { document.querySelector('#out').value = ''; });

    // ---------------------------------------------------------------- 6 viewer
    await page.evaluate(() => { document.querySelector('#sheets img').click(); });
    await new Promise(r => setTimeout(r, 500));
    const v1 = await page.evaluate(() => ({
      on: document.querySelector('#viewer').classList.contains('on'),
      pos: document.querySelector('#vpos').textContent,
    }));
    check(12, 'viewer 能打开', v1.on, JSON.stringify(v1));
    check(13, 'viewer 显示位置与总数', /\/\s*\d+/.test(v1.pos), v1.pos);

    await page.keyboard.press('ArrowRight');
    await new Promise(r => setTimeout(r, 400));
    const v2 = await page.evaluate(() => document.querySelector('#vpos').textContent);
    check(14, '→ 能切到下一张', v2 !== v1.pos, `${v1.pos} → ${v2}`);

    // ---- 缩放：读清小字是这个产品的卖点，viewer 必须能放大
    await page.click('#vzi');
    await page.click('#vzi');
    await new Promise(r => setTimeout(r, 300));
    const zm = await page.evaluate(() => ({
      lvl: document.querySelector('#vlvl').textContent,
      zoomed: document.querySelector('#viewer').classList.contains('zoomed'),
    }));
    check(15, 'viewer 能放大', zm.zoomed && parseFloat(zm.lvl) > 1.5, JSON.stringify(zm));

    await page.click('#vzfit');
    await new Promise(r => setTimeout(r, 300));
    const fit = await page.evaluate(() => ({
      lvl: document.querySelector('#vlvl').textContent,
      zoomed: document.querySelector('#viewer').classList.contains('zoomed'),
    }));
    check(16, '「适应」能回到 1.0× 且退出放大态',
      !fit.zoomed && parseFloat(fit.lvl) <= 1.05, JSON.stringify(fit));

    // 1:1 必须是「**按原始像素**」而不是「缩放 1.0」。
    //
    // 实测教训：第一版断言 `比例 >= 1`，宽屏上 1066 宽的图算出正好 ≈1.0，
    // 把实现退回成 `setZoom(1)` 照样通过（假绿）。
    // 这里换个**不依赖具体数值**的判据：先量"适应态下的渲染宽度"，
    // 再看 1:1 后是否**按比例放大到等于图片自然宽度**。
    await page.setViewport({ width: 900, height: 700 });   // 故意收窄，让图必须放大
    await page.click('#vzfit');
    await new Promise(r => setTimeout(r, 400));
    const fitW = await page.evaluate(() => {
      const im = document.querySelector('#vimg');
      return { natural: im.naturalWidth, rendered: Math.round(im.getBoundingClientRect().width) };
    });
    await page.click('#vz1');
    await new Promise(r => setTimeout(r, 400));
    const oneW = await page.evaluate(() => {
      const im = document.querySelector('#vimg');
      return { rendered: Math.round(im.getBoundingClientRect().width),
               lvl: parseFloat(document.querySelector('#vlvl').textContent) };
    });
    // 断言：1:1 之后的渲染宽度 ≈ 自然宽度（允许 8% 误差，缩放是连续的）
    const ratio = oneW.rendered / Math.max(1, fitW.natural);
    check(17, '1:1 让渲染宽度追上图片原始像素宽',
      fitW.natural > 0 && oneW.lvl >= 1.0 && Math.abs(ratio - 1) < 0.08,
      `natural=${fitW.natural} fit渲染=${fitW.rendered} 1:1渲染=${oneW.rendered} ` +
      `lvl=${oneW.lvl}× 比例=${ratio.toFixed(3)}`);
    await page.setViewport({ width: 1280, height: 900 });

    // 切图后必须回到适应态（否则在放大态下切图会看不到新图）
    await page.keyboard.press('ArrowRight');
    await new Promise(r => setTimeout(r, 400));
    const afterSwitch = await page.evaluate(() => ({
      lvl: document.querySelector('#vlvl').textContent,
      pos: document.querySelector('#vpos').textContent,
    }));
    check(18, '切图后自动回到适应态', parseFloat(afterSwitch.lvl) <= 1.05,
      JSON.stringify(afterSwitch));

    await page.keyboard.press('Escape');
    await new Promise(r => setTimeout(r, 400));
    check(19, 'Esc 能关闭 viewer',
      await page.evaluate(() => !document.querySelector('#viewer').classList.contains('on')));

    // ---------------------------------------------------------------- 7 长静默文案存在
    const hasStale = await page.evaluate(() =>
      document.body.innerHTML.includes('仍在工作') ||
      document.documentElement.innerHTML.includes('仍在工作'));
    check(20, '长静默提示的文案在页面里', hasStale);

    // ---------------------------------------------------------------- 8 无 JS 错误
    check(21, '全程无 JS 错误', jsErrors.length === 0, jsErrors.slice(0, 3).join(' | '));
    // 预期内的 4xx 是**故意**打的（非法目录用例）；打印出来让它可以被复核，
    // 而不是默默吞掉 —— 吞掉的话"无 JS 错误"这条就变成永不红。
    console.log(`  (预期内的 HTTP 4xx: ${expectedHttpErrors.length} 条，来自非法目录用例)`);

    // ---------------------------------------------------------------- 9 产物没写进仓库
  } finally {
    await browser?.close().catch(() => {});
    gui.proc.kill('SIGTERM');
    await rm(outDirTmp, { recursive: true, force: true }).catch(() => {});
  }

  // ---------------------------------------------------------------- 10 仓库没被污染
  // **这两条必须在 finally 之后**：检查点放在清理之前，永远会看到自己刚建的那个目录
  // （实测踩到：`finally 没删干净` 是假红，其实是我检查得太早）。
  const leaked = await readdir(path.join(ROOT, 'eval')).catch(() => []);
  const leftovers = leaked.filter(x => x.startsWith('shots-e2e-'));
  check(22, 'e2e 临时产物目录已清理', leftovers.length === 0,
    leftovers.length ? `${JSON.stringify(leftovers)}（finally 没删干净）` : '');
  check(23, '没有把产物写进 eval/shots（上次误提交的那条路径）',
    !leaked.includes('shots'), 'GUI 默认输出目录被写进了仓库');

  console.log(`\n${failures === 0 ? '✅ 全部通过' : `❌ ${failures} 条失败`}  ` +
    `(${results.length - failures}/${results.length})`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch(e => { console.error('\n💥 未捕获异常:', e); process.exit(3); });
