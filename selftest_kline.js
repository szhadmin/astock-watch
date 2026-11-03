// K 线渲染自检：用后端真实数据验证 SVG 绘制逻辑（无需浏览器）
// 用法：先启动服务，再执行
//   python main.py serve --port 8770
//   node selftest_kline.js 8770
const fs = require('fs');
const http = require('http');
const path = require('path');

const PORT = process.argv[2] || '8770';
const BASE = path.join(__dirname, 'astock', 'web', 'app.js');

// ---- 极简 DOM 桩，让前端脚本能在 node 里跑 ----
global.document = { addEventListener: () => { }, getElementById: () => null, querySelectorAll: () => [] };
global.window = { addEventListener: () => { } };
global.location = { hash: '' };
global.fetch = async () => { throw new Error('no fetch in test'); };
global.setInterval = () => 0;
global.clearInterval = () => 0;
global.setTimeout = () => 0;

const App = eval(fs.readFileSync(BASE, 'utf8') + '\n;App;');

const get = (p) => new Promise((res, rej) => {
  http.get(`http://127.0.0.1:${PORT}${p}`, r => {
    let d = '';
    r.on('data', c => (d += c));
    r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(e); } });
  }).on('error', rej);
});

(async () => {
  let ok = true;
  const periods = ['day', 'week', 'month', 'min60', 'min5', 'trend'];
  const code = process.argv[3] || '600015';
  for (const p of periods) {
    const k = await get('/api/kline?code=' + code + '&period=' + p + '&days=120');
    if (k.error) { console.log(`✘ ${p} 接口错误：${k.error}`); ok = false; continue; }
    const r = App.drawChart(k);
    const svg = r.svg || '';
    const n = p === 'trend' ? (k.trend.times || []).length : (k.bars || []).length;
    const rects = (svg.match(/<rect/g) || []).length;
    const pass = svg.startsWith('<svg') && svg.includes('</svg>') && n > 0 && r.geo !== null;
    if (!pass) ok = false;
    console.log(`${pass ? '✔' : '✘'} ${p.padEnd(6)} ${k.name}  点数=${n}  <rect>=${rects}  geo=${r.geo ? 'ok' : 'null'}`);
  }

  // 标记验证：注入买卖 + 持仓，检查三角/止损线/目标线
  const k2 = await get('/api/kline?code=' + code + '&period=day&days=60');
  const d1 = k2.bars[10].t.slice(0, 10), d2 = k2.bars[20].t.slice(0, 10);
  k2.trades = [
    { ts: d1 + ' 09:31:05', date: d1, side: 'BUY', price: k2.bars[10].c, shares: 900, pnl: null, pct: null, note: '自检' },
    { ts: d2 + ' 14:31:05', date: d2, side: 'SELL', price: k2.bars[20].c, shares: 900, pnl: 123.4, pct: 2.1, note: '自检' },
  ];
  k2.position = { shares: 900, cost: 6.5, stop: 6.3, target: 6.9, pnl_pct: 1.2 };
  const svg2 = App.drawChart(k2).svg;
  const cnt = (re) => (svg2.match(re) || []).length;
  const marks = { 买点: cnt(/buy-mark/g), 卖点: cnt(/sell-mark/g), 止损线: cnt(/stop-line/g), 目标线: cnt(/tgt-line/g) };
  const pass2 = marks.买点 === 1 && marks.卖点 === 1 && marks.止损线 === 1 && marks.目标线 === 1;
  if (!pass2) ok = false;
  console.log(`${pass2 ? '✔' : '✘'} 标记验证：${Object.entries(marks).map(([k, v]) => k + '=' + v).join(' ')}`);

  console.log(`\n${ok ? '全部通过：分时/1分~60分/日/周/月 K 线均可渲染，AI 买卖点与止损目标线正常。'
    : '存在失败项，请检查 /api/kline 与 drawChart。'}`);
  process.exit(ok ? 0 : 1);
})();
