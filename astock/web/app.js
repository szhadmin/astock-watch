/* astock-watch Web 控制台 —— 原生 JS，无依赖 */
const App = (() => {
  const S = { tab: 'dashboard', ov: null, job: null, jobTimer: null, style: null };
  const $ = id => document.getElementById(id);

  /* ---------------- 基础工具 ---------------- */
  const n2 = (v, d = 2) => (v === null || v === undefined || v === '' || isNaN(v))
    ? '--' : Number(v).toFixed(d);
  const pct = v => (v === null || v === undefined || isNaN(v)) ? '--'
    : (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%';
  const cls = v => (v === null || v === undefined || isNaN(v)) ? 'dim'
    : (v > 0 ? 'up' : (v < 0 ? 'down' : 'dim'));
  const esc = s => String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
  const money = v => (v === null || v === undefined) ? '--'
    : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  async function api(path, opts) {
    const r = await fetch(path, opts || {});
    let j = null;
    try { j = await r.json(); } catch (e) {
      throw new Error('返回的不是 JSON（HTTP ' + r.status + '）——后端可能没重启');
    }
    if (r.status === 404) {
      throw new Error('接口不存在（404）：' + path + '\n后端还是旧版本，请关闭服务窗口后重新启动');
    }
    if (j && j.error) throw new Error(j.error);
    return j;
  }
  const post = (path, body) => api(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });

  function toast(msg, isErr) {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'toast' + (isErr ? ' err' : '');
    clearTimeout(S.toastTimer);
    S.toastTimer = setTimeout(() => t.classList.add('hidden'), 2600);
  }

  /* ---------------- 顶栏 ---------------- */
  function renderHeader(ov) {
    const a = ov.account || {};
    $('hd-equity').textContent = money(a.equity);
    $('hd-return').textContent = pct(a.total_return_pct);
    $('hd-return').className = 'mono ' + cls(a.total_return_pct);
    $('hd-cash').textContent = money(a.cash);
    $('hd-pos').textContent = (ov.positions || []).length + ' 只';
    const ses = $('hd-session');
    ses.textContent = ov.in_session ? '交易中' : '非交易时段';
    ses.className = 'pill ' + (ov.in_session ? 'live' : 'off');
    $('hd-provider').textContent = (ov.ai.provider || '?') + ' / ' + (ov.ai.model || '?');
    $('hd-clock').textContent = ov.server_time || '';
    $('hd-provider').className = 'pill ' + (ov.ai.provider && ov.ai.key ? 'ghost' : 'off');
    if (ov.job) watchJob(ov.job, true);
  }

  /* ---------------- 权益曲线（内联 SVG） ---------------- */
  function chart(points, w = 900, h = 190) {
    if (!points || points.length < 2) return '<div class="dim" style="padding:40px 0;text-align:center">暂无足够数据（运行几次决策后生成权益曲线）</div>';
    const vals = points.map(p => p.equity);
    let min = Math.min(...vals), max = Math.max(...vals);
    const pad = (max - min) * 0.15 || 1;
    min -= pad; max += pad;
    const X = i => 8 + i * (w - 60) / (points.length - 1);
    const Y = v => h - 22 - (v - min) / (max - min) * (h - 40);
    let d = '', area = '';
    points.forEach((p, i) => {
      d += (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(p.equity).toFixed(1) + ' ';
      area += (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(p.equity).toFixed(1) + ' ';
    });
    area += `L${X(points.length - 1).toFixed(1)} ${h - 22} L${X(0).toFixed(1)} ${h - 22} Z`;
    const first = points[0].equity, last = points[points.length - 1].equity;
    const color = last >= first ? '#e04a4a' : '#3fa66a';
    const ticks = [max - pad, (min + max) / 2, min + pad].map(v =>
      `<line class="grid-line" x1="0" x2="${w - 52}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"/>
       <text x="${w - 48}" y="${(Y(v) + 3).toFixed(1)}">${n2(v, 0)}</text>`).join('');
    return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      ${ticks}
      <path class="area" d="${area}" style="fill:${color}22"/>
      <path class="line" d="${d}" style="stroke:${color}"/>
      <text x="8" y="${h - 6}">${esc(points[0].date || '')}</text>
      <text x="${w - 90}" y="${h - 6}">${esc(points[points.length - 1].date || '')}</text>
    </svg>`;
  }

  /* ---------------- 仪表盘：我的持仓 + AI 建议卡 ---------------- */
  function myHoldCardHtml(h, adv) {
    if (!h || h.error) return '<div class="dim">我的持仓读取中…</div>';
    const s = h.summary || {};
    if (!h.items || !h.items.length) {
      return `<div class="dim">还没有录入真实持仓。</div>
        <div class="row" style="margin-top:8px"><button class="btn tiny" onclick="App.go('holdings')">去录入</button></div>`;
    }
    const advMap = {};
    (adv && adv.items || []).forEach(a => { advMap[a.code] = a; });
    const rows = h.items.map(r => {
      if (!r.ok) return `<tr><td class="l">${esc(r.code)}</td><td colspan="5" class="dim">${esc(r.error || '')}</td></tr>`;
      const a = advMap[r.code];
      const advTxt = a ? `<span class="${a['建议'] === '加仓' ? 'up' : (['减仓','清仓','止损'].includes(a['建议']) ? 'down' : 'dim')}">${esc(a['建议'])}</span>
        <span class="dim" style="font-size:11px"> ${esc((a['触发位'] || '').slice(0, 14))}</span>` : '<span class="dim">--</span>';
      return `<tr title="点击看K线" style="cursor:pointer" onclick="App.openKline('${r.code}')">
        <td class="l">${esc(r.name || '')} <span class="dim mono">${r.code}</span></td>
        <td>${r.shares}</td><td>${n2(r.cost, 3)}</td><td>${n2(r.price)}</td>
        <td class="${cls(r.pnl_pct)}">${pct(r.pnl_pct)}</td><td class="l">${advTxt}</td></tr>`;
    }).join('');
    return `
      <div class="row">
        <span class="tag">市值 ${money(s.total_mv)}</span>
        <span class="tag ${cls(s.pnl)}">浮盈 ${s.pnl >= 0 ? '+' : ''}${money(s.pnl)}（${pct(s.pnl_pct)}）</span>
        <span class="tag">仓位 ${n2(s.position_pct, 1)}%</span>
        <span class="tag">可用 ${money(s.cash)}</span>
        <span class="spacer"></span>
        <button class="btn tiny" onclick="App.go('holdings')">录入/编辑</button>
        <button class="btn tiny" onclick="App.run('decide')">让 AI 给建议</button>
      </div>
      <table style="margin-top:8px"><thead><tr><th>标的</th><th>股数</th><th>成本</th>
        <th>现价</th><th>浮盈</th><th class="l">AI 建议</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="dim" style="font-size:11px;margin-top:6px">
        ${adv && adv.ts ? 'AI 建议更新于 ' + esc(adv.ts) + (adv.market_view ? '｜' + esc(String(adv.market_view).slice(0, 50)) : '') : '还没有 AI 建议：点右侧按钮让它决策一次'}</div>`;
  }

  async function loadMyHoldDashboard() {
    let h = null, adv = null;
    try { h = await api('/api/holdings'); } catch (e) { h = { error: e.message }; }
    try { adv = await api('/api/holdings_advice'); } catch (e) { adv = null; }
    const box = $('myhold-body');
    if (box) box.innerHTML = myHoldCardHtml(h, adv);
  }

  /* ---------------- 邮件通知设置 ---------------- */
  async function loadMail() {
    const box = $('mail-body');
    if (!box) return;
    try {
      const m = await api('/api/mail');
      box.innerHTML = `
        <div class="row" style="margin-bottom:8px">
          <label class="row" style="gap:6px;font-size:13px">
            <input type="checkbox" id="mail-enabled" ${m.enabled ? 'checked' : ''} style="width:auto"> 启用邮件通知</label>
          <span class="tag ${m.ready ? 'ok' : 'warn'}">${m.ready ? '已就绪' : esc(m.status || '未配置')}</span>
        </div>
        <div class="grid g2">
          <div>
            <label class="dim" style="font-size:12px">SMTP 服务器
              <input id="mail-host" value="${esc(m.smtp_host || '')}" placeholder="smtp.qq.com" style="width:100%"></label>
            <label class="dim" style="font-size:12px">端口
              <input id="mail-port" type="number" value="${m.smtp_port || 465}" style="width:100%"></label>
            <label class="dim" style="font-size:12px">加密
              <select id="mail-ssl" style="width:100%">
                <option value="1" ${m.use_ssl ? 'selected' : ''}>SSL（465）</option>
                <option value="0" ${!m.use_ssl ? 'selected' : ''}>STARTTLS（587）</option>
              </select></label>
          </div>
          <div>
            <label class="dim" style="font-size:12px">发件邮箱
              <input id="mail-user" value="${esc(m.user || '')}" placeholder="you@qq.com" style="width:100%"></label>
            <label class="dim" style="font-size:12px">授权码 / 密码
              <input id="mail-pass" type="password" value="${esc(m.password || '')}" placeholder="留空=不修改" style="width:100%"></label>
            <label class="dim" style="font-size:12px">收件邮箱（多个用逗号）
              <input id="mail-to" value="${esc(m.to || '')}" placeholder="you@qq.com" style="width:100%"></label>
          </div>
        </div>
        <div class="row" style="margin-top:8px">
          <label class="row" style="gap:5px;font-size:12px"><input type="checkbox" id="mail-ev-trade" ${(m.events||{}).trade ? 'checked' : ''} style="width:auto"> 买卖成交</label>
          <label class="row" style="gap:5px;font-size:12px"><input type="checkbox" id="mail-ev-review" ${(m.events||{}).review ? 'checked' : ''} style="width:auto"> 收盘复盘</label>
          <label class="row" style="gap:5px;font-size:12px"><input type="checkbox" id="mail-ev-plan" ${(m.events||{}).plan ? 'checked' : ''} style="width:auto"> 次日计划/日报</label>
          <span class="spacer"></span>
          <button class="btn primary" onclick="App.saveMail()">保存</button>
          <button class="btn" onclick="App.testMail()">发测试邮件</button>
        </div>
        <div class="dim" style="font-size:12px;margin-top:6px">
          QQ/163 邮箱要填「授权码」（在邮箱设置里开启 SMTP 后获得），不是登录密码。
        </div>`;
    } catch (e) { box.innerHTML = '<div class="dim">读取失败：' + esc(e.message) + '</div>'; }
  }

  async function saveMail() {
    const body = {
      enabled: ($('mail-enabled') || {}).checked || false,
      smtp_host: ($('mail-host') || {}).value || '',
      smtp_port: parseInt(($('mail-port') || {}).value || 465, 10),
      use_ssl: ($('mail-ssl') || {}).value === '1',
      user: ($('mail-user') || {}).value || '',
      password: ($('mail-pass') || {}).value || '',
      to: ($('mail-to') || {}).value || '',
      events: {
        trade: ($('mail-ev-trade') || {}).checked || false,
        review: ($('mail-ev-review') || {}).checked || false,
        plan: ($('mail-ev-plan') || {}).checked || false,
      },
    };
    try {
      const r = await post('/api/mail', body);
      toast(r.ready ? '已保存，邮件通知就绪' : '已保存，但还没配好：' + (r.status || ''));
      loadMail();
    } catch (e) { toast('保存失败：' + e.message, true); }
  }

  async function testMail() {
    toast('正在发送…');
    try {
      const r = await post('/api/mail/test', {});
      toast(r.message || (r.ok ? '已发送' : '发送失败'), !r.ok);
    } catch (e) { toast('发送失败：' + e.message, true); }
  }

  /* ---------------- 页面：仪表盘（局部刷新，不闪） ---------------- */
  /* ---------------- 页面：仪表盘（局部刷新，不闪） ---------------- */
  function kpiCardsHtml(ov) {
    const st = ov.stats || {};
    return `
        <div class="card"><h3>总权益</h3>
          <div class="big ${cls(ov.account.total_return_pct)}">${money(ov.account.equity)}</div>
          <div class="${cls(ov.account.total_return_pct)}">${pct(ov.account.total_return_pct)} · 本金 ${money(ov.account.capital)}</div></div>
        <div class="card"><h3>交易统计</h3>
          <div class="big">${st.closed_trades || 0} <span class="dim" style="font-size:13px">笔已平仓</span></div>
          <div class="dim">胜率 ${n2(st.win_rate_pct, 1)}%｜盈亏比 ${st.profit_factor || '--'}｜回撤 ${n2(st.max_drawdown_pct)}%</div></div>
        <div class="card"><h3>模型消耗</h3>
          <div class="big">${ov.usage.calls || 0} <span class="dim" style="font-size:13px">次调用</span></div>
          <div class="dim">输入 ${ov.usage.prompt_tokens || 0} / 输出 ${ov.usage.completion_tokens || 0} tokens</div></div>
        <div class="card" id="market-card"><h3>大盘温度 ${ov._tempTag || ''}</h3>
          <div id="market-card-body">${marketHtml(S.market)}</div></div>`;
  }

  function marketHtml(m) {
    if (!m || m.error) return '<div class="dim">大盘数据获取中…</div>';
    const t = m.temp || {}, g = m.gate || {};
    const color = t.temp >= 60 ? 'up' : (t.temp < 40 ? 'down' : 'dim');
    const bars = (m.sectors || []).slice(0, 5).map(s =>
      `<span class="tag" title="主力净流入 ${n2(s.main_in_wan, 0)} 万｜涨${s.up}/跌${s.down}｜领涨 ${esc(s.leader || '')}">
         ${esc(s.name)} <span class="${cls(s.pct)}">${s.pct > 0 ? '+' : ''}${n2(s.pct)}%</span></span>`).join('');
    return `
      <div class="row"><span class="mono ${color}" style="font-size:26px;font-weight:600">${t.temp ?? '--'}</span>
        <span class="tag ${t.temp >= 60 ? 'hot' : (t.temp < 40 ? 'warn' : '')}">${esc(t.level || '')}</span>
        <span class="dim" style="font-size:12px">${esc(g.note || '')}</span></div>
      <div class="dim" style="font-size:12px;margin-top:6px">
        指数 ${n2((t.detail || {})['指数平均涨跌%'])}% ｜ 上涨占比 ${n2((t.detail || {})['上涨家数占比%'], 1)}%
        ｜ 涨停/跌停 ${esc((t.detail || {})['涨停/跌停'] || '-')} ｜ ${esc((t.detail || {})['上证趋势'] || '')}</div>
      <div class="row" style="margin-top:8px">${bars || '<span class="dim">板块数据不可用</span>'}</div>`;
  }

  function positionsHtml(ov) {
    const pos = ov.positions || [];
    if (!pos.length) return '<div class="dim">空仓中。AI 可以选择空仓，空仓也是一种决策。</div>';
    return `<table><thead><tr><th>标的</th><th>股数</th><th>成本</th><th>现价</th><th>浮盈</th><th>止损</th><th>目标</th></tr></thead><tbody>
      ${pos.map(p => `<tr title="点击查看K线与AI买卖点" style="cursor:pointer" onclick="App.openKline('${p.code}')">
        <td class="l">${esc(p.name || '')} <span class="dim mono">${p.code}</span></td>
        <td>${p.shares}</td><td>${n2(p.cost, 3)}</td><td>${n2(p.price)}</td>
        <td class="${cls(p.pnl_pct)}">${pct(p.pnl_pct)}</td>
        <td class="mono">${p.stop || '--'}</td><td class="mono">${p.target || '--'}</td></tr>`).join('')}
      </tbody></table>`;
  }

  function watchHtml(ov) {
    const w = ov.watch || [];
    if (!w.length) return '<div class="dim">观察池为空 —— 到「选股池」页让 AI 选股。</div>';
    return `<table><thead><tr><th>标的</th><th>现价</th><th>涨跌</th></tr></thead><tbody>
      ${w.map(x => `<tr title="点击查看K线" style="cursor:pointer" onclick="App.openKline('${x.code}')">
        <td class="l">${esc(x.name || '')} <span class="dim mono">${x.code}</span></td>
        <td>${n2(x.price)}</td><td class="${cls(x.pct)}">${pct(x.pct)}</td></tr>`).join('')}
      </tbody></table>`;
  }

  function plansHtml(ov) {
    const plans = ov.plans || [];
    if (!plans.length) return '<div class="dim">无待执行计划单。收盘后的决策会存为计划单、次日开盘才成交（避免用收盘价事后偷价）。</div>';
    return `<table><thead><tr><th>标的</th><th>方向</th><th>股数</th><th>信号价</th><th>止损</th><th>信号时间</th></tr></thead><tbody>
      ${plans.map(p => `<tr title="点击查看K线" style="cursor:pointer" onclick="App.openKline('${p.code}')">
        <td class="l">${esc(p.name || '')} <span class="dim mono">${p.code}</span></td>
        <td class="${p.action === 'BUY' ? 'up' : 'down'}">${p.action === 'BUY' ? '买入' : '卖出'}</td>
        <td>${p.shares}</td><td>${n2(p.signal_price, 3)}</td>
        <td class="mono">${p.stop || '--'}</td>
        <td class="dim mono" style="font-size:12px">${esc(p.signal_time || '')}</td></tr>`).join('')}
      </tbody></table>
      <div class="row" style="margin-top:10px">
        <button class="btn primary" onclick="App.run('execute')">立即尝试执行</button>
        <span class="dim" style="font-size:12px">高开 &gt;1.5% 或跳空跌破止损则自动取消</span></div>`;
  }

  function railsHtml(ov) {
    const rs = ov.rails || {};
    return `<div class="dim" style="margin-bottom:8px">${esc(ov.rails_desc)}</div>
      <div class="row">
        <span class="tag">单股 ≤${rs.max_position_pct}%</span>
        <span class="tag">总仓 ≤${rs.max_total_position_pct}%</span>
        <span class="tag">每日 ≤${rs.max_trades_per_day} 笔</span>
        <span class="tag">单笔风险 ≤${rs.max_risk_per_trade_pct}%</span>
        <span class="tag warn">组合风险 ≤${rs.max_total_risk_pct || 2}%</span>
        <span class="tag warn">当日亏 ${rs.daily_loss_limit_pct}% 停手</span>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="btn primary" onclick="App.run('decide')">立即决策</button>
        <button class="btn" onclick="App.run('pick')">AI 选股</button>
        <button class="btn" onclick="App.run('review')">收盘复盘</button>
        <button class="btn" onclick="App.refreshDashboard()">刷新数据</button>
      </div>
      <div class="dim" style="margin-top:8px;font-size:12px">
        调度：${ov.schedule.enabled ? '开启' : '关闭'}｜盘中每 ${ov.schedule.decide_every_min} 分钟决策
        ｜${ov.schedule.pick_at} 选股｜${ov.schedule.review_at} 复盘</div>`;
  }

  async function loadDashboardData() {
    const [ov, eq, mkt] = await Promise.all([
      api('/api/overview'),
      api('/api/equity').catch(() => ({ equity: [] })),
      api('/api/market').catch(() => null),
    ]);
    S.market = mkt;
    return { ov, eq, mkt };
  }

  async function viewDashboard() {
    const { ov, eq } = await loadDashboardData();
    S.ov = ov;
    renderHeader(ov);
    loadMyHoldDashboard();
    $('view').innerHTML = `
      <div class="grid g3" id="kpi-cards">${kpiCardsHtml(ov)}</div>
      <div class="grid g2" style="margin-top:14px">
        <div class="card"><h3>权益曲线</h3><div id="kpi-equity">${chart(eq.equity)}</div></div>
        <div class="card"><h3>当前持仓</h3><div id="pos-card-body">${positionsHtml(ov)}</div></div>
      </div>
      <div class="grid g2" style="margin-top:14px">
        <div class="card"><h3>AI 观察池</h3><div id="watch-card-body">${watchHtml(ov)}</div></div>
        <div class="card"><h3>待执行计划单 <span class="dim" style="font-weight:400">（收盘后决策 → 次日开盘执行）</span></h3>
          <div id="plans-card-body">${plansHtml(ov)}</div></div>
      </div>
      <div class="card" style="margin-top:14px"><h3>硬风控红线（AI 不可越过）</h3>
        <div id="rails-card-body">${railsHtml(ov)}</div></div>`;
  }

  /* 只更新数字与表格，不重建整页 → 无闪烁、保留滚动位置 */
  async function refreshDashboard() {
    if (S.tab !== 'dashboard') return;
    let data;
    try { data = await loadDashboardData(); } catch (e) { return; }
    const { ov, eq } = data;
    S.ov = ov;
    renderHeader(ov);
    const setHtml = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
    setHtml('kpi-cards', kpiCardsHtml(ov));
    setHtml('kpi-equity', chart(eq.equity));
    setHtml('pos-card-body', positionsHtml(ov));
    setHtml('watch-card-body', watchHtml(ov));
    setHtml('plans-card-body', plansHtml(ov));
    setHtml('rails-card-body', railsHtml(ov));
    loadMyHoldDashboard();
    const flash = $('view');
    if (flash) { flash.classList.add('soft-updated'); setTimeout(() => flash.classList.remove('soft-updated'), 400); }
  }

  /* ---------------- 页面：K 线复盘（分时 / 多周期 / 十字光标 / 点击详情） ---------------- */
  const PERIODS = [['trend', '分时'], ['min1', '1分'], ['min5', '5分'], ['min15', '15分'],
  ['min30', '30分'], ['min60', '60分'], ['day', '日K'], ['week', '周K'], ['month', '月K']];
  const KDAYS = [60, 120, 250];

  function klineState() {
    if (!S.kline) {
      S.kline = { code: S.klineCode || '600015', period: S.klinePeriod || 'day',
                  days: S.klineDays || 120, sel: null, auto: true, timer: null, data: null, geo: null };
    }
    return S.kline;
  }

  /* ---------- 图表绘制 ---------- */
  function drawChart(k) {
    const isTrend = k.period === 'trend';
    const W = 1220, H = 560;
    const L = 10, R = 74, T = 14;
    const mainH = 356, gap = 22, volH = 92, axisH = 22;
    const n = isTrend ? (k.trend.times || []).length : (k.bars || []).length;
    if (!n) return { svg: '<div class="dim" style="padding:40px;text-align:center">无数据</div>', geo: null };
    const plotW = W - L - R;
    const step = plotW / n;
    const bw = Math.max(1.1, Math.min(11, step * 0.62));
    const volBase = T + mainH + gap + volH;

    let hi = -Infinity, lo = Infinity, series = [];
    if (isTrend) {
      series = k.trend.prices;
      hi = Math.max(...series, ...k.trend.avgs, k.prev_close);
      lo = Math.min(...series, ...k.trend.avgs, k.prev_close);
      const mid = k.prev_close || (hi + lo) / 2;
      const half = Math.max(hi - mid, mid - lo) * 1.12 || mid * 0.01;
      hi = mid + half; lo = mid - half;
    } else {
      k.bars.forEach(b => { hi = Math.max(hi, b.h); lo = Math.min(lo, b.l); });
      ['ma5', 'ma10', 'ma20', 'ma60'].forEach(key =>
        ((k.ma || {})[key] || []).forEach(v => { if (v) { hi = Math.max(hi, v); lo = Math.min(lo, v); } }));
      const p = k.position;
      if (p) {
        if (p.stop) { hi = Math.max(hi, p.stop); lo = Math.min(lo, p.stop); }
        if (p.target) { hi = Math.max(hi, p.target); lo = Math.min(lo, p.target); }
      }
      const pad = (hi - lo) * 0.07 || 0.1;
      hi += pad; lo -= pad;
    }
    const X = i => L + i * step + step / 2;
    const Y = v => T + (hi - v) / (hi - lo) * mainH;
    const vols = isTrend ? k.trend.vols : k.bars.map(b => b.v || 0);
    const vmax = Math.max(1, ...vols);
    const p = [];
    const upColor = '#e04a4a', downColor = '#3fa66a';

    // 网格 + 价格轴（分时：中间线为昨收 + 右侧显示涨跌幅）
    for (let i = 0; i <= 4; i++) {
      const v = hi - (hi - lo) * i / 4;
      const y = Y(v).toFixed(1);
      p.push(`<line class="axis" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/>`);
      p.push(`<text x="${W - R + 6}" y="${(+y + 3).toFixed(1)}">${n2(v, 2)}</text>`);
      if (isTrend && k.prev_close) {
        const pctv = (v / k.prev_close - 1) * 100;
        p.push(`<text x="${W - R + 44}" y="${(+y + 3).toFixed(1)}" fill="${pctv > 0 ? upColor : pctv < 0 ? downColor : '#6b7c96'}">${pctv > 0 ? '+' : ''}${pctv.toFixed(2)}%</text>`);
      }
    }

    if (isTrend) {
      // 昨收参考线
      p.push(`<line class="axis" x1="${L}" x2="${W - R}" y1="${Y(k.prev_close).toFixed(1)}" y2="${Y(k.prev_close).toFixed(1)}" stroke-dasharray="4 4"/>`);
      // 价格线
      let dl = '', da = '';
      series.forEach((v, i) => {
        dl += (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(v).toFixed(1) + ' ';
        da += (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(k.trend.avgs[i]).toFixed(1) + ' ';
      });
      p.push(`<path d="${dl}" fill="none" stroke="#e8c46a" stroke-width="1.4"/>`);
      p.push(`<path d="${da}" fill="none" stroke="#5fb3e8" stroke-width="1.1" opacity="0.9"/>`);
      // 成交量
      k.trend.vols.forEach((v, i) => {
        const h = v / vmax * volH;
        const prev = i ? series[i - 1] : series[0];
        p.push(`<rect x="${(X(i) - bw / 2).toFixed(1)}" y="${(volBase - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0.5, h).toFixed(1)}" fill="${series[i] >= prev ? upColor : downColor}" opacity="0.55"/>`);
      });
      // 时间轴
      const marks = ['09:30', '10:30', '11:30', '14:00', '15:00'];
      k.trend.times.forEach((t, i) => {
        if (marks.indexOf(t) >= 0) {
          p.push(`<text x="${X(i).toFixed(1)}" y="${H - 4}" text-anchor="middle">${t}</text>`);
          p.push(`<line class="axis" x1="${X(i).toFixed(1)}" x2="${X(i).toFixed(1)}" y1="${T}" y2="${T + mainH}" stroke-dasharray="2 4" opacity="0.5"/>`);
        }
      });
      // AI 当天成交（按时间定位）
      const times = k.trend.times;
      (k.trades || []).forEach(t => {
        const hhmm = (t.ts || '').slice(11, 16);
        let idx = times.indexOf(hhmm);
        if (idx < 0) idx = times.length - 1;
        const isBuy = t.side === 'BUY';
        const base = isBuy ? t.price * 0.995 : t.price * 1.005;
        const yv = Y(Math.min(Math.max(base, lo), hi));
        const y = isBuy ? yv + 12 : yv - 12;
        p.push(isBuy
          ? `<path class="buy-mark" d="M${(X(idx) - 5).toFixed(1)} ${(y + 9).toFixed(1)} L${(X(idx) + 5).toFixed(1)} ${(y + 9).toFixed(1)} L${X(idx).toFixed(1)} ${y.toFixed(1)} Z"><title>${esc(t.ts + ' 买入 ' + t.shares + '股 @' + n2(t.price, 3) + '\n' + (t.note || ''))}</title></path>`
          : `<path class="sell-mark" d="M${(X(idx) - 5).toFixed(1)} ${(y - 9).toFixed(1)} L${(X(idx) + 5).toFixed(1)} ${(y - 9).toFixed(1)} L${X(idx).toFixed(1)} ${y.toFixed(1)} Z"><title>${esc(t.ts + ' 卖出 ' + t.shares + '股 @' + n2(t.price, 3) + '\n' + (t.note || ''))}</title></path>`);
      });
    } else {
      // K 线
      k.bars.forEach((b, i) => {
        const x = X(i), up = b.c >= b.o;
        p.push(`<line class="${up ? 'wick-up' : 'wick-down'}" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${Y(b.h).toFixed(1)}" y2="${Y(b.l).toFixed(1)}"/>`);
        const yTop = Y(Math.max(b.o, b.c)), yBot = Y(Math.min(b.o, b.c));
        p.push(`<rect class="${up ? 'body-up' : 'body-down'}" x="${(x - bw / 2).toFixed(1)}" y="${yTop.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, yBot - yTop).toFixed(1)}"/>`);
      });
      // 成交量
      k.bars.forEach((b, i) => {
        const h = (b.v || 0) / vmax * volH;
        p.push(`<rect class="${b.c >= b.o ? 'body-up' : 'body-down'}" opacity="0.5" x="${(X(i) - bw / 2).toFixed(1)}" y="${(volBase - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0.5, h).toFixed(1)}"/>`);
      });
      // 均线
      [['ma5'], ['ma10'], ['ma20'], ['ma60']].forEach(([key]) => {
        const arr = (k.ma || {})[key] || [];
        let d = '', open = false;
        arr.forEach((v, i) => {
          if (!v) { open = false; return; }
          d += (open ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(v).toFixed(1) + ' ';
          open = true;
        });
        if (d) p.push(`<path class="${key}" d="${d}"/>`);
      });
      // 止损 / 目标
      if (k.position && k.position.stop) {
        p.push(`<line class="stop-line" x1="${L}" x2="${W - R}" y1="${Y(k.position.stop).toFixed(1)}" y2="${Y(k.position.stop).toFixed(1)}"/>`);
        p.push(`<text x="${L + 4}" y="${(Y(k.position.stop) - 4).toFixed(1)}" fill="#d99b2b">止损 ${n2(k.position.stop)}</text>`);
      }
      if (k.position && k.position.target) {
        p.push(`<line class="tgt-line" x1="${L}" x2="${W - R}" y1="${Y(k.position.target).toFixed(1)}" y2="${Y(k.position.target).toFixed(1)}"/>`);
        p.push(`<text x="${L + 4}" y="${(Y(k.position.target) - 4).toFixed(1)}" fill="#4a8fe0">目标 ${n2(k.position.target)}</text>`);
      }
      // AI 成交（按日期定位）
      const idxByDate = {};
      k.bars.forEach((b, i) => { idxByDate[b.t.slice(0, 10)] = i; });
      (k.trades || []).forEach(t => {
        const i = idxByDate[t.date];
        if (i === undefined) return;
        const b = k.bars[i], x = X(i), isBuy = t.side === 'BUY';
        const tip = t.ts + (isBuy ? ' 买入 ' : ' 卖出 ') + t.shares + '股 @' + n2(t.price, 3)
          + (t.pnl !== null && t.pnl !== undefined ? ' 盈亏' + n2(t.pnl) + '元' : '')
          + (t.note ? '\n' + t.note : '');
        if (isBuy) {
          const y = Y(b.l) + 10;
          p.push(`<path class="buy-mark" d="M${(x - 5).toFixed(1)} ${(y + 9).toFixed(1)} L${(x + 5).toFixed(1)} ${(y + 9).toFixed(1)} L${x.toFixed(1)} ${y.toFixed(1)} Z"><title>${esc(tip)}</title></path>`);
        } else {
          const y = Y(b.h) - 10;
          p.push(`<path class="sell-mark" d="M${(x - 5).toFixed(1)} ${(y - 9).toFixed(1)} L${(x + 5).toFixed(1)} ${(y - 9).toFixed(1)} L${x.toFixed(1)} ${y.toFixed(1)} Z"><title>${esc(tip)}</title></path>`);
        }
      });
      // 计划单
      (k.plans || []).forEach(pl => {
        const i = idxByDate[pl.signal_date] !== undefined ? idxByDate[pl.signal_date] : n - 1;
        const x = X(i), y = Y(pl.signal_price);
        p.push(`<g><circle class="plan-mark" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="7"/>
          <text x="${x.toFixed(1)}" y="${(y + 3.5).toFixed(1)}" text-anchor="middle" fill="#e8c46a">P</text>
          <title>${esc('计划单 ' + pl.signal_date + ' ' + pl.signal_time + ' ' + pl.action + ' ' + pl.shares + '股 信号价' + n2(pl.signal_price, 3) + '\n' + (pl.reason || ''))}</title></g>`);
      });
      // 决策时点
      (k.decisions || []).forEach(dc => {
        const i = idxByDate[(dc.ts || '').slice(0, 10)];
        if (i === undefined) return;
        const color = dc.action === 'BUY' ? '#e04a4a' : (dc.action === 'SELL' ? '#3fa66a' : '#4a8fe0');
        p.push(`<g><line class="dec-mark" x1="${X(i).toFixed(1)}" x2="${X(i).toFixed(1)}" y1="${(T + mainH + 3).toFixed(1)}" y2="${(T + mainH + 12).toFixed(1)}" stroke="${color}"/>
          <title>${esc((dc.ts || '') + ' 【' + dc.kind + '】' + dc.action + ' ' + (dc.shares || '') + '\n' + (dc.reason || ''))}</title></g>`);
      });
      // 日期轴
      const every = Math.max(1, Math.floor(n / 8));
      k.bars.forEach((b, i) => {
        if (i % every && i !== n - 1) return;
        p.push(`<text x="${X(i).toFixed(1)}" y="${H - 4}" text-anchor="middle">${b.t.slice(5, 10)}</text>`);
      });
    }

    const svg = `<svg id="ksvg" class="kline-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      <rect id="k-sel" x="0" y="0" width="0" height="0" fill="#4a8fe0" opacity="0.12"/>
      ${p.join('')}
      <line id="k-ch-v" x1="0" x2="0" y1="${T}" y2="${volBase}" stroke="#5a6d8c" stroke-width="1" stroke-dasharray="3 3" opacity="0"/>
      <line id="k-ch-h" x1="${L}" x2="${W - R}" y1="0" y2="0" stroke="#5a6d8c" stroke-width="1" stroke-dasharray="3 3" opacity="0"/>
    </svg>`;
    return { svg, geo: { W, H, L, R, T, mainH, step, n, X, Y, isTrend, bw } };
  }

  /* ---------- 工具条 / 图表卡 / 详情卡 ---------- */
  function klineShell(ov, k) {
    const st = klineState();
    const chips = [...(ov.positions || []).map(x => x.code), ...((ov.watch || []).map(x => x.code)),
    ...((ov.plans || []).map(x => x.code))].filter((v, i, a) => a.indexOf(v) === i);
    const priceCls = cls(k.pct);
    return `
      <div class="card">
        <div class="row">
          <h3 style="margin:0">行情</h3>
          <span class="spacer"></span>
          <input id="kline-code" placeholder="6 位代码" value="${esc(st.code)}" style="width:110px"
                 onkeydown="if(event.key==='Enter')App.openKline(this.value)">
          <button class="btn tiny" onclick="App.openKline(document.getElementById('kline-code').value)">查看</button>
          <select id="kline-kdays" style="width:96px" onchange="App.setKlineDays(this.value)">
            ${KDAYS.map(d => `<option value="${d}" ${d === st.days ? 'selected' : ''}>${d} 根</option>`).join('')}
          </select>
          <label class="row" style="gap:5px;font-size:12px"><input type="checkbox" id="k-auto"
            ${st.auto ? 'checked' : ''} onchange="App.toggleAuto(this.checked)" style="width:auto"> 自动刷新</label>
          <button class="btn tiny" onclick="App.refreshKline()">刷新</button>
          ${chips.map(c => `<span class="tag" onclick="App.openKline('${c}')" style="cursor:pointer">${c}</span>`).join('')}
        </div>
        <div class="row" style="margin-top:10px;gap:6px" id="k-periods">
          ${PERIODS.map(([v, label]) => `<button class="btn tiny ${st.period === v ? 'primary' : ''}"
            onclick="App.setKlinePeriod('${v}')">${label}</button>`).join('')}
          <span class="spacer"></span>
          <b style="font-size:16px">${esc(k.name)} <span class="dim mono">${k.code}</span></b>
          <span class="mono ${priceCls}" style="font-size:18px">${n2(k.price)}</span>
          <span class="${priceCls}">${pct(k.pct)}</span>
          ${k.position ? `<span class="tag ok">持仓 ${k.position.shares}股 ${pct(k.position.pnl_pct)}</span>` : ''}
          <span class="tag ${k.live ? 'hot' : ''}">${k.live ? '● 实时' : '收盘'} ${esc(k.updated || '')}</span>
        </div>
        <div class="legend" style="margin-top:8px">
          ${k.period === 'trend'
        ? '<span><i style="background:#e8c46a"></i>价格</span><span><i style="background:#5fb3e8"></i>均价</span><span><i style="background:#4a8fe0"></i>昨收</span>'
        : '<span><i class="tri-up" style="border-radius:0"></i>AI 买入</span><span><i class="tri-down" style="border-radius:0"></i>AI 卖出</span><span><i class="ring"></i>计划单</span><span><i class="tick"></i>AI 决策</span><span><i style="background:#e8c46a"></i>MA5</span><span><i style="background:#5fb3e8"></i>MA10</span><span><i style="background:#c07ae0"></i>MA20</span><span><i style="background:#8a99b0"></i>MA60</span>'}
          <span class="dim">鼠标移动看十字光标 · 点击某根 K 线锁定详情</span>
        </div>
      </div>`;
  }

  function klineChartCard(k) {
    const { svg } = drawChart(k);
    return `<div class="card" style="margin-top:14px">
      <div class="kchart" id="kchart">${svg}
        <div class="k-tip hidden" id="ktip"></div>
      </div>
    </div>`;
  }

  function klineDetailCard(k, idx) {
    const sel = idx === null || idx === undefined ? null : idx;
    if (sel === null) {
      return `<div class="card hidden" id="k-detail" style="margin-top:14px"><h3>选中详情</h3>
        <div class="dim">点击上图任意一根 K 线（分时上任意一点）查看该周期详情，以及 AI 在当时做了什么。</div></div>`;
    }
    let head = '', body = '', aiRows = '';
    if (k.period === 'trend') {
      const t = k.trend.times[sel], pr = k.trend.prices[sel], av = k.trend.avgs[sel];
      const pc = k.prev_close ? (pr / k.prev_close - 1) * 100 : 0;
      head = `<b>${esc(k.date || '')} ${esc(t)}</b>`;
      body = `<table><tbody>
        <tr><th>价格</th><td class="mono ${cls(pc)}">${n2(pr)}</td><th>涨跌幅</th><td class="${cls(pc)}">${pc > 0 ? '+' : ''}${pc.toFixed(2)}%</td></tr>
        <tr><th>均价</th><td class="mono">${n2(av)}</td><th>成交量</th><td>${n2(k.trend.vols[sel] / 100, 0)} 手</td></tr></tbody></table>`;
      const hhmm = k.trend.times[sel];
      const hits = (k.trades || []).filter(x => (x.ts || '').slice(11, 16) === hhmm);
      aiRows = hits.map(x => `<div class="item"><span class="${x.side === 'BUY' ? 'up' : 'down'}">${x.side === 'BUY' ? '买入' : '卖出'}</span>
        ${x.shares}股 @${n2(x.price, 3)}<div class="meta">${esc(x.note || '')}</div></div>`).join('');
    } else {
      const b = k.bars[sel];
      const amp = b.l ? (b.h - b.l) / b.l * 100 : 0;
      head = `<b>${esc(b.t)}</b>`;
      body = `<table><tbody>
        <tr><th>开</th><td class="mono">${n2(b.o)}</td><th>高</th><td class="mono up">${n2(b.h)}</td></tr>
        <tr><th>低</th><td class="mono down">${n2(b.l)}</td><th>收</th><td class="mono ${cls(b.pct === 0 ? 0 : (b.c - b.o))}">${n2(b.c)}</td></tr>
        <tr><th>涨跌幅</th><td class="${cls(b.pct)}">${b.pct > 0 ? '+' : ''}${n2(b.pct)}%</td><th>振幅</th><td>${n2(amp)}%</td></tr>
        <tr><th>成交量</th><td>${n2(b.v / 1e4, 1)} 万手</td><th>成交额</th><td>${n2(b.amt / 1e8, 2)} 亿</td></tr>
        <tr><th>换手率</th><td>${n2(b.turn)}%</td><th>均线</th><td class="dim">MA5 ${n2((k.ma.ma5 || [])[sel])} / MA20 ${n2((k.ma.ma20 || [])[sel])}</td></tr>
        </tbody></table>`;
      const d10 = b.t.slice(0, 10);
      const trs = (k.trades || []).filter(x => x.date === d10);
      const pls = (k.plans || []).filter(x => x.signal_date === d10);
      const dcs = (k.decisions || []).filter(x => (x.ts || '').slice(0, 10) === d10);
      aiRows = [
        ...trs.map(x => `<div class="item"><span class="${x.side === 'BUY' ? 'up' : 'down'}">${x.side === 'BUY' ? '买入' : '卖出'}</span>
          ${x.shares}股 @${n2(x.price, 3)} <span class="dim">${esc((x.ts || '').slice(11))}</span>
          <div class="meta">${esc(x.note || '')}</div></div>`),
        ...pls.map(x => `<div class="item"><span class="warn">计划单</span> ${x.action} ${x.shares}股
          <div class="meta">${esc(x.reason || '')}</div></div>`),
        ...dcs.map(x => `<div class="item"><span class="tag">${esc(x.kind)}</span> ${esc(x.action)}
          <span class="dim">${esc((x.ts || '').slice(11))}</span><div class="meta">${esc(x.reason || '')}</div></div>`),
      ].join('');
    }
    return `<div class="card" id="k-detail" style="margin-top:14px">
      <div class="row"><h3 style="margin:0">选中详情</h3><span class="spacer"></span>
        <span class="dim" style="font-size:12px">${head}</span></div>
      <div class="grid g2" style="margin-top:8px">
        <div>${body}</div>
        <div><h3 style="margin:0 0 6px">AI 在这一刻</h3>
          <div class="list" style="max-height:200px">${aiRows || '<div class="dim">该周期内 AI 无动作</div>'}</div></div>
      </div>
    </div>`;
  }

  function klineTables(k) {
    return `<div class="grid g2" style="margin-top:14px">
      <div class="card"><h3>AI 成交明细（含成交时间）</h3>
        ${(k.trades || []).length ? `<table><thead><tr><th>时间</th><th>方向</th><th>价格</th><th>股数</th><th>盈亏</th><th>理由</th></tr></thead><tbody>
          ${k.trades.slice().reverse().map(t => `<tr>
            <td class="l mono" style="font-size:12px">${esc(t.ts)}</td>
            <td class="${t.side === 'BUY' ? 'up' : 'down'}">${t.side === 'BUY' ? '买入' : '卖出'}</td>
            <td>${n2(t.price, 3)}</td><td>${t.shares}</td>
            <td class="${cls(t.pnl)}">${t.pnl === null || t.pnl === undefined ? '--' : money(t.pnl)}</td>
            <td class="l dim" style="font-size:12px">${esc((t.note || '').slice(0, 46))}</td></tr>`).join('')}
          </tbody></table>` : '<div class="dim">还没有成交。AI 开仓后会在此按时间列出，并在图上标出买卖点。</div>'}
      </div>
      <div class="card"><h3>待执行计划单</h3>
        ${(k.plans || []).length ? `<table><thead><tr><th>信号时间</th><th>方向</th><th>股数</th><th>信号价</th><th>止损</th></tr></thead><tbody>
          ${k.plans.map(p => `<tr><td class="l mono" style="font-size:12px">${esc(p.signal_date)} ${esc(p.signal_time)}</td>
            <td class="${p.action === 'BUY' ? 'up' : 'down'}">${p.action === 'BUY' ? '买入' : '卖出'}</td>
            <td>${p.shares}</td><td>${n2(p.signal_price, 3)}</td><td>${n2(p.stop, 3)}</td></tr>`).join('')}
          </tbody></table>` : '<div class="dim">无计划单</div>'}
        <h3 style="margin-top:14px">该股 AI 决策记录</h3>
        <div class="list" style="max-height:240px">
          ${(k.decisions || []).slice().reverse().map(d => `<div class="item">
            <div class="row"><span class="tag">${esc(d.kind)}</span>
              <span class="dim mono" style="font-size:12px">${esc(d.ts)}</span>
              <span class="${d.action === 'BUY' ? 'up' : (d.action === 'SELL' ? 'down' : 'dim')}">${esc(d.action)}</span></div>
            <div class="meta">${esc(d.reason || '')}</div></div>`).join('') || '<div class="dim">暂无</div>'}
        </div>
      </div>
    </div>`;
  }

  /* ---------- 交互：十字光标 / 点击选中 ---------- */
  function bindChart(k) {
    const st = klineState();
    const g = drawChart(k).geo;
    st.geo = g;
    const box = $('kchart');
    if (!box || !g) return;
    const tip = $('ktip');
    const cv = $('k-ch-v'), ch = $('k-ch-h'), selRect = $('k-sel');
    const svg = $('ksvg');

    function locate(ev) {
      const r = box.getBoundingClientRect();
      const ratio = r.width / g.W;
      const px = (ev.clientX - r.left) / ratio;          // 转到 viewBox 坐标
      const py = (ev.clientY - r.top) / ratio;
      let i = Math.round((px - g.L - g.step / 2) / g.step);
      i = Math.max(0, Math.min(g.n - 1, i));
      return { i, px, py, ratio, r };
    }

    function tipHtml(i) {
      if (g.isTrend) {
        const t = k.trend.times[i], pr = k.trend.prices[i], av = k.trend.avgs[i];
        const pc = k.prev_close ? (pr / k.prev_close - 1) * 100 : 0;
        return `<div class="kt">${esc(t)}</div>
          <div>价格 <b class="${cls(pc)}">${n2(pr)}</b> <span class="${cls(pc)}">${pc > 0 ? '+' : ''}${pc.toFixed(2)}%</span></div>
          <div>均价 ${n2(av)}　量 ${n2(k.trend.vols[i] / 100, 0)}手</div>`;
      }
      const b = k.bars[i];
      return `<div class="kt">${esc(b.t)}</div>
        <div>开 ${n2(b.o)}　高 ${n2(b.h)}</div>
        <div>低 ${n2(b.l)}　收 <b class="${cls(b.pct)}">${n2(b.c)}</b></div>
        <div>涨跌 <b class="${cls(b.pct)}">${b.pct > 0 ? '+' : ''}${n2(b.pct)}%</b>　量 ${n2(b.v / 1e4, 1)}万手</div>
        <div class="dim">额 ${n2(b.amt / 1e8, 2)}亿　换手 ${n2(b.turn)}%</div>`;
    }

    box.onmousemove = (ev) => {
      const { i, px, ratio, r } = locate(ev);
      const x = g.X(i);
      cv.setAttribute('x1', x); cv.setAttribute('x2', x); cv.setAttribute('opacity', '0.9');
      const rectTop = (ev.clientY - r.top) / ratio;
      ch.setAttribute('y1', rectTop); ch.setAttribute('y2', rectTop); ch.setAttribute('opacity', '0.7');
      tip.innerHTML = tipHtml(i);
      tip.classList.remove('hidden');
      const left = Math.min(px * ratio + 14, r.width - 190);
      const top = Math.max(4, Math.min(rectTop * ratio - 10, r.height - 110));
      tip.style.left = left + 'px';
      tip.style.top = top + 'px';
    };
    box.onmouseleave = () => {
      cv.setAttribute('opacity', '0');
      ch.setAttribute('opacity', '0');
      tip.classList.add('hidden');
    };
    box.onclick = (ev) => {
      const { i } = locate(ev);
      st.sel = i;
      // 高亮选中 K 线
      selRect.setAttribute('x', (g.X(i) - g.bw).toFixed(1));
      selRect.setAttribute('y', g.T);
      selRect.setAttribute('width', (g.step).toFixed(1));
      selRect.setAttribute('height', (g.H - g.T - 24).toFixed(1));
      const card = $('k-detail');
      if (card) {
        const tmp = document.createElement('div');
        tmp.innerHTML = klineDetailCard(k, i);
        card.replaceWith(tmp.firstElementChild);
      }
    };
  }

  /* ---------- 页面主流程 ---------- */
  async function viewKline() {
    const ov = S.ov || await api('/api/overview');
    S.ov = ov;
    renderHeader(ov);
    const st = klineState();
    let k;
    try { k = await fetchKline(); }
    catch (e) {
      $('view').innerHTML = `<div class="card"><h3>行情加载失败</h3><pre>${esc(e.message)}</pre></div>`;
      return;
    }
    if (k.error) {
      $('view').innerHTML = `<div class="card"><h3>行情加载失败</h3><pre>${esc(k.error)}</pre></div>`;
      return;
    }
    st.data = k;
    $('view').innerHTML = klineShell(ov, k) + klineChartCard(k) + klineDetailCard(k, st.sel)
      + klineTables(k) + altCardHtml();
    bindChart(k);
    loadStockAlt(st.code);
    const totalPts = k.period === 'trend' ? (k.trend.times || []).length : (k.bars || []).length;
    if (st.sel !== null && st.sel < totalPts) {
      const box = $('kchart');
      if (box && st.geo) {
        const rect = $('k-sel');
        if (rect) {
          rect.setAttribute('x', (st.geo.X(st.sel) - st.geo.bw).toFixed(1));
          rect.setAttribute('y', st.geo.T);
          rect.setAttribute('width', st.geo.step.toFixed(1));
          rect.setAttribute('height', (st.geo.H - st.geo.T - 24).toFixed(1));
        }
      }
    }
    startAuto();
  }

  async function fetchKline() {
    const st = klineState();
    return await api('/api/kline?code=' + st.code + '&period=' + st.period + '&days=' + st.days);
  }

  function startAuto() {
    const st = klineState();
    stopAuto();
    if (!st.auto) return;
    const intraday = ['trend', 'min1', 'min5', 'min15', 'min30', 'min60'].indexOf(st.period) >= 0;
    const open = S.ov && S.ov.in_session;
    if (!open && !intraday) return;          // 收盘后日K不必刷
    st.timer = setInterval(async () => {
      if (S.tab !== 'kline') return;
      try {
        const k = await fetchKline();
        if (k.error) return;
        st.data = k;
        const box = $('kchart');
        if (box) { box.innerHTML = drawChart(k).svg + '<div class="k-tip hidden" id="ktip"></div>'; bindChart(k); }
        const head = document.querySelector('#k-periods .tag, #k-periods b');
        const priceEl = document.querySelector('#k-periods .mono');
        if (priceEl) { priceEl.textContent = n2(k.price); priceEl.className = 'mono ' + cls(k.pct); }
      } catch (e) { /* 忽略 */ }
    }, 15000);
  }
  function stopAuto() { const st = klineState(); if (st.timer) { clearInterval(st.timer); st.timer = null; } }

  function openKline(code, period, days) {
    const st = klineState();
    if (code) st.code = String(code).trim();
    if (period) st.period = period;
    if (days) st.days = parseInt(days, 10);
    st.sel = null;
    S.tab = 'kline';
    if (location.hash !== '#kline') location.hash = '#kline';
    route('kline', true);
  }
  function setKlinePeriod(p) { const st = klineState(); st.period = p; st.sel = null; route('kline', true); }
  function setKlineDays(d) { const st = klineState(); st.days = parseInt(d, 10); st.sel = null; route('kline', true); }
  function toggleAuto(on) { const st = klineState(); st.auto = !!on; if (on) startAuto(); else stopAuto(); toast(on ? '已开启自动刷新（15秒）' : '已关闭自动刷新'); }
  function refreshKline() { route('kline', true); }

  /* ---------------- 页面：AI 决策 ---------------- */
  async function viewDecisions() {
    const [ds, rps, prs] = await Promise.all([
      api('/api/decisions?days=5'), api('/api/reports'), api('/api/prompts')]);
    const items = ds.items || [];
    const v = $('view');
    v.innerHTML = `
      <div class="two-col">
        <div class="card"><h3>决策流水（最近 5 天）</h3>
          <div class="list">${items.length ? items.map((d, i) => `
            <div class="item" onclick="App.showDecision(${i})">
              <div class="row"><b>${esc(d.kind || '')}</b>
                <span class="tag">${esc(d.ts || '')}</span></div>
              <div class="meta">${esc((d.market_view || '').slice(0, 70))}</div>
              <div class="meta">动作 ${(d.actions || []).length} 条 · 裁决 ${(d.results || []).length} 条</div>
            </div>`).join('') : '<div class="dim">还没有决策记录，点右上「立即决策」跑一次。</div>'}</div>
        </div>
        <div class="card"><h3>决策详情</h3>
          <div id="decision-detail" class="dim">← 选择左侧一条记录查看模型原始输出与风控裁决</div></div>
      </div>

      <div class="grid g2" style="margin-top:14px">
        <div class="card"><h3>审计：发给模型的提示词</h3>
          <div class="list">${(prs.items || []).slice(0, 40).map(p => `
            <div class="item" onclick="App.showPrompt('${esc(p.name)}')">
              <div class="row"><span class="mono" style="font-size:12px">${esc(p.name)}</span>
              <span class="spacer"></span><span class="meta">${(p.size / 1024).toFixed(1)} KB</span></div>
            </div>`).join('') || '<div class="dim">暂无</div>'}</div>
        </div>
        <div class="card"><h3>解析失败诊断（data/ai/failed）</h3>
          <div class="list" id="failed-list"><div class="dim">加载中…</div></div>
          <div class="dim" style="margin-top:8px;font-size:12px">
            模型输出不可解析时会把原文与提示词留档；点开可看 finish_reason 与是否被截断。
          </div>
        </div>
        <div class="card"><h3>完整报告文件</h3>
          <div class="list">${(rps.items || []).slice(0, 40).map(p => `
            <div class="item" onclick="App.showReport('${esc(p.name)}')">
              <div class="row"><span class="mono" style="font-size:12px">${esc(p.name)}</span>
              <span class="spacer"></span><span class="meta">${esc(p.mtime)}</span></div>
            </div>`).join('') || '<div class="dim">暂无</div>'}</div>
        </div>
      </div>
      <div class="card hidden" id="doc-view" style="margin-top:14px"><h3 id="doc-title">文件</h3><pre id="doc-body"></pre></div>`;
    S.decisions = items;
    loadFailed();
  }

  async function loadFailed() {
    const box = $('failed-list');
    if (!box) return;
    try {
      const f = await api('/api/failed');
      box.innerHTML = (f.items || []).length
        ? f.items.map(x => `<div class="item" onclick="App.showFailed('${esc(x.name)}')">
             <div class="row"><span class="mono" style="font-size:12px">${esc(x.name)}</span>
             <span class="spacer"></span><span class="meta">${esc(x.mtime)}</span></div></div>`).join('')
        : '<div class="dim">没有失败记录 —— 说明模型输出都能正常解析。</div>';
    } catch (e) { box.innerHTML = '<div class="dim">读取失败：' + esc(e.message) + '</div>'; }
  }

  async function showDecision(i) {
    const d = (S.decisions || [])[i];
    if (!d) return;
    const box = $('decision-detail');
    box.className = '';
    box.innerHTML = `
      <div class="row" style="margin-bottom:8px">
        <span class="tag hot">${esc(d.kind)}</span><span class="tag">${esc(d.ts)}</span></div>
      <div style="margin-bottom:10px"><b>市场判断：</b>${esc(d.market_view || '')}</div>
      ${(d.actions || []).length ? `<table><thead><tr><th>标的</th><th>动作</th><th>股数</th><th>理由</th></tr></thead><tbody>
        ${(d.actions || []).map(a => `<tr><td class="l mono">${esc(a.code)}</td>
          <td>${esc(a.action)}</td><td>${a.shares ?? '--'}</td>
          <td class="l dim">${esc(a.reason || '')}</td></tr>`).join('')}</tbody></table>` : '<div class="dim">本次无买卖动作</div>'}
      <h3 style="margin-top:14px">风控裁决</h3>
      <pre>${esc(((d.results || []).join('\n')) || '无')}</pre>`;
  }

  async function showPrompt(name) {
    const d = await api('/api/prompt?f=' + encodeURIComponent(name));
    showDoc('提示词 · ' + name, d.content || '(空)');
  }
  async function showReport(name) {
    const d = await api('/api/decision?f=' + encodeURIComponent(name));
    showDoc('报告 · ' + name, d.content || '(空)');
  }
  async function showFailed(name) {
    const d = await api('/api/failed?f=' + encodeURIComponent(name));
    showDoc('失败原文 · ' + name, d.content || '(空)');
  }
  function showDoc(title, body) {
    $('doc-title').textContent = title;
    $('doc-body').textContent = body;
    const el = $('doc-view');
    el.classList.remove('hidden');
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* ---------------- 页面：选股池 ---------------- */
  async function viewPool() {
    const [c, w] = await Promise.all([api('/api/candidates'), api('/api/watchlist')]);
    const items = c.items || [];
    const codes = w.codes || [];
    const v = $('view');
    v.innerHTML = `
      <div class="card">
        <div class="row"><h3 style="margin:0">AI 观察池</h3><span class="spacer"></span>
          <input id="watch-input" placeholder="输入 6 位代码，逗号分隔" style="width:280px"
                 value="${esc(codes.join(','))}">
          <button class="btn ok" onclick="App.saveWatch()">保存</button></div>
        <div class="dim" style="margin-top:8px;font-size:12px">
          AI 只会在「观察池 ∪ 持仓 ∪ 候选池」里交易，杜绝编造代码。
          ${w.updated ? '上次更新：' + esc(w.updated) : ''}
          ${(w.meta && w.meta.reason) ? '<br>AI 理由：' + esc(w.meta.reason) : ''}
        </div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="row"><h3 style="margin:0">规则初筛候选池</h3>
          <span class="spacer"></span>
          <span class="dim" style="font-size:12px">${c.age_sec !== null ? '更新于 ' + c.age_sec + ' 秒前' : '尚未扫描'}</span>
          <button class="btn primary" onclick="App.run('scan')" ${c.running ? 'disabled' : ''}>
            ${c.running ? '扫描中…' : '重新扫描全市场'}</button></div>
        <div class="dim" style="margin-top:8px;font-size:12px">
          规则把全市场 5000 只缩到 ~300 只高流动性股票 → 逐只读 90 根日线 → 打形态标签 → 交给 AI 挑选。
        </div>
        ${items.length ? `<table style="margin-top:10px"><thead><tr>
            <th>标的</th><th>现价</th><th>涨跌</th><th>形态</th><th>距MA20</th>
            <th>60日分位</th><th>RSI6</th><th>量能</th><th>成交额</th><th>候选分</th><th></th></tr></thead>
          <tbody>${items.map(f => `<tr>
            <td class="l">${esc(f.name)} <span class="dim mono">${f.code}</span></td>
            <td>${n2(f.price)}</td><td class="${cls(f.pct)}">${pct(f.pct)}</td>
            <td class="l"><span class="tag ${f.category.includes('突破') || f.category.includes('放量') ? 'hot' : ''}">${esc(f.category)}</span></td>
            <td class="${cls(f.dev_ma20)}">${pct(f.dev_ma20)}</td>
            <td>${n2(f.pct_pos60, 0)}%</td><td>${n2(f.rsi6, 1)}</td>
            <td>${n2(f.vol_ratio5)}x</td><td>${n2(f.amount_yi, 1)}亿</td>
            <td class="mono">${n2(f.screen_score, 1)}</td>
            <td><button class="btn tiny" onclick="App.openKline('${f.code}')">K</button> <button class="btn tiny" onclick="App.addWatch('${f.code}')">+池</button></td></tr>`).join('')}
          </tbody></table>` : '<div class="dim" style="margin-top:12px">点「重新扫描全市场」生成候选（约 20~40 秒）。</div>'}
      </div>`;
  }

  async function addWatch(code) {
    const w = await api('/api/watchlist');
    const codes = Array.from(new Set([...(w.codes || []), code])).slice(0, 10);
    await post('/api/watchlist', { codes });
    toast('已加入观察池：' + code);
    viewPool();
  }
  async function saveWatch() {
    const codes = $('watch-input').value.split(/[,，\s]+/).filter(x => x);
    await post('/api/watchlist', { codes });
    toast('观察池已保存');
    viewPool();
  }

  /* ---------------- 页面：我的持仓（真实账户，手工录入 + 分析） ---------------- */
  function holdState() {
    if (!S.hold) S.hold = { capital: 15000, items: [], data: null, loaded: false };
    return S.hold;
  }

  async function viewHoldings() {
    const st = holdState();
    let d;
    try { d = await api('/api/holdings'); }
    catch (e) { $('view').innerHTML = `<div class="card"><h3>读取失败</h3><pre>${esc(e.message)}</pre></div>`; return; }
    st.data = d;
    st.capital = d.capital || 15000;
    st.items = (d.raw || []).map(x => ({ ...x }));
    st.loaded = true;
    $('view').innerHTML = `
      <div class="card">
        <div class="row"><h3 style="margin:0">我的持仓</h3>
          <span class="spacer"></span>
          <span class="dim" style="font-size:12px">这是你自己的真实账户，AI 不会动它；AI 模拟盘在仪表盘</span>
        </div>
        <div class="row" style="margin-top:10px">
          <label style="font-size:12px">资金量（本金）
            <input id="hold-capital" type="number" step="100" value="${st.capital}" style="width:130px;margin-left:6px">
          </label>
          <button class="btn tiny" onclick="App.addHoldRow()">+ 加一只</button>
          <button class="btn primary" onclick="App.saveHoldings()">保存并分析</button>
          <span class="spacer"></span>
          <span class="dim" style="font-size:12px">${d.updated ? '上次保存 ' + esc(d.updated) : '尚未保存过'}</span>
        </div>
        <div id="hold-edit" style="margin-top:12px"></div>
      </div>
      <div class="card" style="margin-top:14px"><h3>AI 操作建议 <span class="dim" style="font-weight:400">（基于你的成本与当前盘面）</span></h3>
        <div id="hold-advice" class="dim">加载中…</div>
        <div class="row" style="margin-top:8px"><button class="btn tiny" onclick="App.run('decide')">让 AI 重新决策</button></div>
      </div>
      <div id="hold-analysis" style="margin-top:14px"></div>`;
    renderHoldEditor();
    renderHoldAnalysis(d);
    renderHoldAdvice();
  }

  function renderHoldEditor() {
    const st = holdState();
    const box = $('hold-edit');
    if (!box) return;
    if (!st.items.length) {
      box.innerHTML = '<div class="dim">还没有录入。点「+ 加一只」填代码、股数、成本，然后点「保存并分析」。</div>';
      return;
    }
    box.innerHTML = `<table><thead><tr>
        <th style="width:110px">代码</th><th style="width:100px">股数</th><th style="width:100px">成本价</th>
        <th style="width:130px">买入日期</th><th>备注</th><th style="width:60px"></th></tr></thead><tbody>
      ${st.items.map((it, i) => `<tr>
        <td><input class="h-code" data-i="${i}" value="${esc(it.code || '')}" placeholder="6位" style="width:96px"></td>
        <td><input class="h-shares" data-i="${i}" type="number" step="100" value="${it.shares || ''}" style="width:88px"></td>
        <td><input class="h-cost" data-i="${i}" type="number" step="0.001" value="${it.cost || ''}" style="width:88px"></td>
        <td><input class="h-date" data-i="${i}" type="date" value="${esc(it.date || '')}" style="width:120px"></td>
        <td><input class="h-note" data-i="${i}" value="${esc(it.note || '')}" placeholder="比如：突破买入/补仓" style="width:100%"></td>
        <td><button class="btn tiny" onclick="App.delHoldRow(${i})">删</button></td></tr>`).join('')}
      </tbody></table>`;
  }

  function collectHoldRows() {
    const st = holdState();
    const items = [];
    $('view').querySelectorAll('.h-code').forEach(inp => {
      const i = inp.dataset.i;
      const q = s => { const el = $('view').querySelector('.' + s + '[data-i="' + i + '"]'); return el ? el.value.trim() : ''; };
      items.push({ code: inp.value.trim(), shares: q('h-shares'), cost: q('h-cost'),
                   date: q('h-date'), note: q('h-note') });
    });
    return items;
  }

  function addHoldRow() {
    const st = holdState();
    st.items = collectHoldRows();
    st.items.push({ code: '', shares: '', cost: '', date: '', note: '' });
    renderHoldEditor();
  }

  function delHoldRow(i) {
    const st = holdState();
    st.items = collectHoldRows();
    st.items.splice(i, 1);
    renderHoldEditor();
  }

  async function saveHoldings() {
    const st = holdState();
    const capital = parseFloat(($('hold-capital') || {}).value || st.capital || 0);
    const items = collectHoldRows().filter(x => x.code && x.shares && x.cost);
    if (!items.length) { toast('至少填一只股票（代码/股数/成本）', true); return; }
    try {
      toast('保存中…');
      const r = await post('/api/holdings', { capital, items });
      st.data = r; st.capital = r.capital; st.items = (r.raw || []).map(x => ({ ...x }));
      renderHoldEditor();
      renderHoldAnalysis(r);
      toast('已保存并完成分析');
    } catch (e) { toast('保存失败：' + e.message, true); }
  }

  async function renderHoldAdvice() {
    const box = $('hold-advice');
    if (!box) return;
    try {
      const a = await api('/api/holdings_advice');
      const items = a.items || [];
      if (!items.length) {
        box.innerHTML = '<div class="dim">' + esc(a.hint || '还没有 AI 建议') + '</div>';
        return;
      }
      box.innerHTML = `<div class="dim" style="font-size:12px;margin-bottom:6px">更新时间 ${esc(a.ts || '')}
          ${a.market_view ? '｜' + esc(a.market_view) : ''}</div>
        <table><thead><tr><th>标的</th><th>建议</th><th>触发位</th><th>理由</th></tr></thead><tbody>
        ${items.map(x => `<tr><td class="l">${esc(x.name || '')} <span class="dim mono">${esc(x.code)}</span></td>
          <td class="${['加仓','买入'].includes(x['建议']) ? 'up' : (['减仓','清仓','止损','卖出'].includes(x['建议']) ? 'down' : 'dim')}">${esc(x['建议'])}</td>
          <td class="dim">${esc(x['触发位'] || '-')}</td>
          <td class="l dim" style="font-size:12px">${esc(x['理由'] || '')}</td></tr>`).join('')}
        </tbody></table>`;
    } catch (e) { box.innerHTML = '<div class="dim">读取失败：' + esc(e.message) + '</div>'; }
  }

  function renderHoldAnalysis(d) {
    const box = $('hold-analysis');
    if (!box) return;
    if (!d || !d.items || !d.items.length) {
      box.innerHTML = '<div class="card"><div class="dim">录入持仓后这里会显示：总市值、总盈亏、仓位占比，以及每只票的评分、止损、目标和建议。</div></div>';
      return;
    }
    const s = d.summary || {};
    const kpi = `<div class="grid g4">
      <div class="card"><h3>总市值</h3><div class="big ${cls(s.pnl)}">${money(s.total_mv)}</div>
        <div class="${cls(s.pnl)}">${s.pnl >= 0 ? '+' : ''}${money(s.pnl)} （${pct(s.pnl_pct)}）</div></div>
      <div class="card"><h3>持仓成本</h3><div class="big">${money(s.total_cost)}</div>
        <div class="dim">${s.count} 只 · 仓位 ${n2(s.position_pct, 1)}%</div></div>
      <div class="card"><h3>可用资金</h3><div class="big">${money(s.cash)}</div>
        <div class="dim">本金 ${money(s.capital)}</div></div>
      <div class="card"><h3>风险提示</h3>
        <div class="dim" style="font-size:12px">${holdRiskText(d)}</div></div>
    </div>`;
    const rows = d.items.map(r => {
      if (!r.ok) {
        return `<div class="card" style="margin-top:12px"><b>${esc(r.code)}</b>
          <span class="warn">${esc(r.error || '获取失败')}</span></div>`;
      }
      return `<div class="card" style="margin-top:12px">
        <div class="row">
          <b style="cursor:pointer" onclick="App.openKline('${r.code}')">${esc(r.name)} <span class="dim mono">${r.code}</span></b>
          <span class="mono ${cls(r.pnl_pct)}" style="font-size:17px">${n2(r.price)}</span>
          <span class="${cls(r.pct)}">${pct(r.pct)}</span>
          <span class="tag ${r.tone === 'danger' ? 'warn' : (r.tone === 'ok' ? 'ok' : '')}">${esc(r.level)}</span>
          <span class="spacer"></span>
          <span class="${cls(r.pnl_pct)}">${r.pnl >= 0 ? '+' : ''}${money(r.pnl)}（${pct(r.pnl_pct)}）</span>
        </div>
        <div class="grid g3" style="margin-top:8px">
          <div><div class="dim" style="font-size:12px">持仓</div>
            <div>${r.shares}股 · 成本 ${n2(r.cost, 3)} · 市值 ${money(r.mv)}</div>
            <div class="dim" style="font-size:12px">占持仓 ${n2(r.weight_pct, 1)}%</div></div>
          <div><div class="dim" style="font-size:12px">技术面</div>
            <div>评分 <b>${r.score}</b>／右侧 ${r.right} · 左侧 ${r.left} · 确认 ${r.checklist}/4</div>
            <div class="dim" style="font-size:12px">止损 ${n2(r.stop)} · 目标 ${(r.targets || []).map(x => n2(x)).join(' → ') || '--'}</div></div>
          <div><div class="dim" style="font-size:12px">建议</div>
            <div class="${r.tone === 'danger' ? 'down' : (r.tone === 'warn' ? 'warn' : 'up')}">${esc(r.advice)}</div>
            <div class="dim" style="font-size:12px">距止损 ${r.dist_stop_pct === null ? '--' : n2(r.dist_stop_pct) + '%'}</div></div>
        </div>
        ${r.note ? `<div class="dim" style="margin-top:6px;font-size:12px">备注：${esc(r.note)}</div>` : ''}
      </div>`;
    }).join('');
    box.innerHTML = kpi + rows;
  }

  function holdRiskText(d) {
    const s = d.summary || {};
    const tips = [];
    if (s.position_pct >= 90) tips.push('仓位已接近满仓，注意单票与总仓风险');
    else if (s.position_pct < 30) tips.push('仓位较轻，等右侧确认再加');
    const bad = (d.items || []).filter(r => r.ok && (r.tone === 'danger'));
    if (bad.length) tips.push(bad.length + ' 只已跌破止损：' + bad.map(x => x.name).join('、'));
    const weak = (d.items || []).filter(r => r.ok && r.score < 40);
    if (weak.length) tips.push(weak.length + ' 只处于弱势筑底区，别加仓');
    if (s.pnl_pct <= -8) tips.push('组合浮亏已超 8%，建议先减仓再谈机会');
    return tips.length ? tips.join('；') : '结构正常：无破止损、无满仓，按各自区间操作。';
  }

  /* ---------------- 页面：龙虎榜（全市场） ---------------- */
  async function viewLhb() {
    const st = S.lhb || (S.lhb = { days: 30, code: '' });
    const v = $('view');
    v.innerHTML = `
      <div class="card">
        <div class="row"><h3 style="margin:0">龙虎榜</h3>
          <span class="spacer"></span>
          <select id="lhb-days" style="width:110px" onchange="App.loadLhb(this.value, document.getElementById('lhb-code').value)">
            ${[7, 15, 30, 60].map(d => `<option value="${d}" ${d === st.days ? 'selected' : ''}>近 ${d} 日</option>`).join('')}
          </select>
          <input id="lhb-code" placeholder="可选：6位代码" value="${esc(st.code)}" style="width:130px"
                 onkeydown="if(event.key==='Enter')App.loadLhb(document.getElementById('lhb-days').value, this.value)">
          <button class="btn primary" onclick="App.loadLhb(document.getElementById('lhb-days').value, document.getElementById('lhb-code').value)">查询</button>
        </div>
        <div class="dim" style="margin-top:8px;font-size:12px">
          数据源：东方财富龙虎榜（akshare）｜按上榜日倒序｜点任意一行可直接看该股 K 线与 AI 买卖点
        </div>
        <div id="lhb-body" style="margin-top:10px"><div class="dim">加载中…</div></div>
      </div>`;
    loadLhb(st.days, st.code);
  }

  async function loadLhb(days, code) {
    const st = S.lhb || (S.lhb = { days: 30, code: '' });
    st.days = parseInt(days || st.days, 10);
    st.code = (code || '').trim();
    const box = $('lhb-body');
    if (!box) return;
    box.innerHTML = '<div class="dim">加载中…（首次约 1~3 秒）</div>';
    try {
      const url = '/api/lhb?days=' + st.days + '&limit=120' + (st.code ? '&code=' + st.code : '');
      const r = await api(url);
      if (r.error) { box.innerHTML = '<div class="dim">' + esc(r.error) + '</div>'; return; }
      const items = r.items || [];
      if (!items.length) {
        box.innerHTML = '<div class="dim">该区间没有龙虎榜记录' + (st.code ? '（' + esc(st.code) + '）' : '') + '</div>';
        return;
      }
      const dates = Array.from(new Set(items.map(i => i.date)));
      box.innerHTML = `<div class="row" style="margin-bottom:6px">
          <span class="tag">${items.length} 条</span>
          <span class="tag">上榜日 ${dates.length} 天</span>
          <span class="tag">${esc(r.status && r.status.version || '')}</span></div>
        <table><thead><tr><th>上榜日</th><th>标的</th><th>涨跌幅</th><th>净买额(万)</th>
          <th>买入(万)</th><th>卖出(万)</th><th>上榜原因</th><th>解读</th></tr></thead><tbody>
        ${items.map(i => `<tr title="点击查看K线" style="cursor:pointer" onclick="App.openKline('${i.code}')">
          <td class="l mono" style="font-size:12px">${esc(i.date)}</td>
          <td class="l">${esc(i.name)} <span class="dim mono">${i.code}</span></td>
          <td class="${cls(i.pct)}">${i.pct > 0 ? '+' : ''}${n2(i.pct)}%</td>
          <td class="${cls(i.net_wan)}">${n2(i.net_wan, 0)}</td>
          <td class="dim">${n2(i.buy_wan, 0)}</td>
          <td class="dim">${n2(i.sell_wan, 0)}</td>
          <td class="l dim" style="font-size:12px">${esc(i.reason)}</td>
          <td class="l dim" style="font-size:12px">${esc(i.read)}</td></tr>`).join('')}
        </tbody></table>`;
    } catch (e) {
      box.innerHTML = '<div class="dim">读取失败：' + esc(e.message).replace(/\n/g, '<br>') + '</div>';
    }
  }

  /* ---------------- 基本面卡（K线页用） ---------------- */
  function altCardHtml() {
    return `<div class="card" style="margin-top:14px"><h3>基本面 · 股东 · 龙虎榜</h3>
      <div id="alt-body" class="dim">加载中…</div>
      <div class="row" style="margin-top:8px">
        <button class="btn tiny" onclick="App.loadStockAlt(S.kline.code, true)">强制刷新</button>
        <span class="dim" style="font-size:12px">数据来自 akshare，缓存 6 小时</span>
      </div></div>`;
  }

  async function loadStockAlt(code, force) {
    const box = $('alt-body');
    if (!box) return;
    try {
      const r = await api('/api/altdata?code=' + encodeURIComponent(code) + (force ? '&force=1' : ''));
      if (!r.available) {
        box.innerHTML = `<div class="dim">${esc((r.status && r.status.note) || '扩展数据不可用')}</div>`;
        return;
      }
      const fin = (r.financial || []).map(x =>
        `<tr><td class="l">${esc(x['项'])}</td>${(x['期'] || []).map(p =>
          `<td class="mono">${esc(p['值'])}<span class="dim" style="font-size:11px"> ${esc(p['报告期'])}</span></td>`).join('')}</tr>`).join('');
      const hold = (r.holders || []).map(x =>
        `<tr><td class="l mono">${esc(x['截止日'])}</td><td>${esc(x['股东户数'])}</td>
         <td class="${cls(parseFloat(x['较上期%']))}">${esc(x['较上期%'])}%</td>
         <td class="dim">${esc(x['户均持股市值万'])} 万</td></tr>`).join('');
      const lhb = (r.lhb || []).map(x =>
        `<tr><td class="l mono">${esc(x['日期'])}</td>
         <td class="${cls(x['净买额万'])}">${n2(x['净买额万'], 1)} 万</td>
         <td class="l dim" style="font-size:12px">${esc((x['解读'] || '') + ' ' + (x['上榜原因'] || ''))}</td></tr>`).join('');
      box.innerHTML = `
        <div class="grid g2">
          <div><h3 style="margin:0 0 6px">财务摘要</h3>
            ${fin ? `<table><tbody>${fin}</tbody></table>` : '<div class="dim">无数据</div>'}</div>
          <div><h3 style="margin:0 0 6px">股东户数</h3>
            ${hold ? `<table><thead><tr><th>截止日</th><th>户数</th><th>环比</th><th>户均市值</th></tr></thead><tbody>${hold}</tbody></table>` : '<div class="dim">无数据</div>'}</div>
        </div>
        <h3 style="margin:12px 0 6px">近 30 日龙虎榜</h3>
        ${lhb ? `<table><thead><tr><th>日期</th><th>净买额</th><th>解读</th></tr></thead><tbody>${lhb}</tbody></table>`
          : '<div class="dim">近 30 日未上榜</div>'}
        <div class="dim" style="margin-top:8px;font-size:12px">${esc(r.compact || '')}</div>`;
    } catch (e) {
      box.innerHTML = '<div class="dim">读取失败：' + esc(e.message) + '</div>';
    }
  }

  /* ---------------- 页面：复盘 ---------------- */
  async function viewReview() {
    const [ov, eq, tr, jr, ls] = await Promise.all([
      api('/api/overview'), api('/api/equity'), api('/api/trades'),
      api('/api/journal'), api('/api/lessons')]);
    renderHeader(ov);
    const st = tr.stats || {};
    const v = $('view');
    v.innerHTML = `
      <div class="grid g3">
        <div class="card"><h3>绩效</h3>
          <div class="big ${cls(st.total_return_pct)}">${pct(st.total_return_pct)}</div>
          <div class="dim">${money(st.equity)} / 本金 ${money(st.start_capital)}</div></div>
        <div class="card"><h3>胜率与盈亏比</h3>
          <div class="big">${n2(st.win_rate_pct, 1)}%</div>
          <div class="dim">均盈 ${money(st.avg_win)} / 均亏 ${money(st.avg_loss)} · 盈亏比 ${st.profit_factor || '--'}</div></div>
        <div class="card"><h3>最大回撤 / 费用</h3>
          <div class="big down">${n2(st.max_drawdown_pct)}%</div>
          <div class="dim">累计手续费 ${money(st.total_fee)} 元</div></div>
      </div>
      <div class="card" style="margin-top:14px"><h3>权益曲线</h3>${chart(eq.equity)}</div>
      <div class="card" style="margin-top:14px"><h3>每日报告归档</h3>
        <div id="archive-body" class="dim">加载中…</div>
        <div class="row" style="margin-top:8px">
          <button class="btn" onclick="App.run('daily')">立即生成今日日报</button>
          <span class="dim" style="font-size:12px">每天 15:20 自动生成并归档到 data/reports/archive/&lt;日期&gt;/</span>
        </div>
      </div>

      <div class="two-col" style="margin-top:14px">
        <div class="card"><h3>交易日记</h3>
          <div class="list">${(jr.items || []).map(j => `
            <div class="item" onclick="App.showJournal('${esc(j.name.replace('.md', ''))}')">
              <div class="row"><span class="mono">${esc(j.name)}</span>
              <span class="spacer"></span><span class="meta">${(j.size / 1024).toFixed(1)} KB</span></div>
            </div>`).join('') || '<div class="dim">还没有日记。收盘复盘后 AI 会写。</div>'}</div>
          <h3 style="margin-top:14px">AI 积累的教训</h3>
          <div class="list" style="max-height:280px">${(ls.items || []).map(x => `
            <div class="item"><div class="meta">${esc(x.ts)}</div>${esc(x.lesson)}</div>`).join('') || '<div class="dim">暂无</div>'}</div>
        </div>
        <div class="card"><h3>成交明细</h3>
          ${(tr.items || []).length ? `<table><thead><tr><th>时间</th><th>方向</th><th>标的</th>
            <th>价格</th><th>股数</th><th>盈亏</th><th>备注</th></tr></thead><tbody>
            ${(tr.items || []).map(t => `<tr>
              <td class="l mono" style="font-size:12px">${esc(t.ts)}</td>
              <td class="${t.side === 'BUY' ? 'up' : 'down'}">${t.side === 'BUY' ? '买入' : '卖出'}</td>
              <td class="l">${esc(t.name || '')} <span class="dim mono">${t.code}</span></td>
              <td>${n2(t.price, 3)}</td><td>${t.shares}</td>
              <td class="${cls(t.pnl)}">${t.pnl === null ? '--' : money(t.pnl)}</td>
              <td class="l dim" style="font-size:12px">${esc((t.note || '').slice(0, 40))}</td></tr>`).join('')}
            </tbody></table>` : '<div class="dim">还没有成交记录。</div>'}
        </div>
      </div>
      <div class="card hidden" id="doc-view" style="margin-top:14px"><h3 id="doc-title"></h3><pre id="doc-body"></pre></div>`;
    loadArchive, loadLhb, loadStockAlt, saveHoldings, addHoldRow, delHoldRow,
    go, loadMail, saveMail, testMail, renderHoldAdvice, loadMyHoldDashboard();
  }

  async function loadArchive() {
    const box = $('archive-body');
    if (!box) return;
    try {
      const r = await api('/api/report_archive');
      const items = r.items || [];
      box.innerHTML = items.length
        ? `<table><thead><tr><th>日期</th><th>文件数</th><th>报告</th></tr></thead><tbody>
            ${items.map(i => `<tr><td class="l mono">${esc(i.date)}</td><td>${i.files}</td>
              <td class="l">${i.html ? `<a href="/reports/archive/${esc(i.date)}/daily_${esc(i.date)}.html" target="_blank">打开 HTML</a>` : '<span class="dim">-</span>'}</td></tr>`).join('')}
           </tbody></table>`
        : '<div class="dim">还没有日报。点下方按钮生成，或等 15:20 自动生成。</div>';
    } catch (e) { box.innerHTML = '<div class="dim">读取失败：' + esc(e.message) + '</div>'; }
  }

  async function showJournal(date) {
    const d = await api('/api/journal?d=' + encodeURIComponent(date));
    showDoc('交易日记 · ' + date, d.content || '(空)');
  }

  /* ---------------- 页面：风格手册 ---------------- */
  async function viewStyle() {
    const pb = await api('/api/playbook');
    const v = $('view');
    v.innerHTML = `
      <div class="two-col">
        <div class="card"><h3>手册版本历史</h3>
          <div class="dim" style="font-size:12px;margin-bottom:8px">
            每次收盘复盘后 AI 会重写这份手册，旧版自动归档 —— 这就是它的「风格演化」轨迹。</div>
          <div class="list">${(pb.history || []).map(h => `
            <div class="item" onclick="App.showPb('${esc(h.name)}')">
              <div class="mono" style="font-size:12px">${esc(h.name)}</div>
              <div class="meta">${esc(h.mtime)} · ${(h.size / 1024).toFixed(1)} KB</div>
            </div>`).join('') || '<div class="dim">暂无归档版本</div>'}</div>
        </div>
        <div class="card"><h3>当前手册（AI 自己维护）</h3>
          <pre style="max-height:640px">${esc(pb.text || '(空)')}</pre></div>
      </div>
      <div class="card hidden" id="doc-view" style="margin-top:14px"><h3 id="doc-title"></h3><pre id="doc-body"></pre></div>`;
  }
  async function showPb(name) {
    const d = await api('/api/playbook/file?f=' + encodeURIComponent(name));
    showDoc('历史版本 · ' + name, d.content || '(空)');
  }

  /* ---------------- 页面：设置 ---------------- */
  async function viewSettings() {
    const c = await api('/api/config');
    const ai = c.ai || {}, r = c.rails || {}, s = c.schedule || {};
    const f = (k, label, v, type = 'text', hint = '') => `
      <label class="f"><span>${label}${hint ? ' · ' + hint : ''}</span>
        <input id="cfg-${k}" type="${type}" value="${esc(v ?? '')}"></label>`;
    const rail = (k, label) => `
      <label class="f"><span>${label}</span>
        <input id="rail-${k}" type="number" step="0.1" value="${esc(r[k] ?? '')}"></label>`;
    const v = $('view');
    v.innerHTML = `
      <div class="grid g2">
        <div class="card"><h3>AI 大脑（OpenAI 兼容网关）</h3>
          <label class="f"><span>供应商</span>
            <select id="cfg-provider">
              ${['openai', 'custom', 'deepseek', 'ollama'].map(p =>
      `<option value="${p}" ${ai.provider === p ? 'selected' : ''}>${p}</option>`).join('')}
            </select></label>
          ${f('base_url', 'base_url', ai.base_url, 'text', '如 https://api.openai.com/v1 或你的中转地址')}
          ${f('model', 'model', ai.model, 'text', '如 gpt-4o-mini / deepseek-chat / qwen2.5:14b')}
          ${f('api_key', 'api_key', ai.api_key, 'password', '留空表示不修改')}
          <div class="row2">
            ${f('temperature', 'temperature', ai.temperature ?? 0.4, 'number')}
            ${f('max_tokens', 'max_tokens', ai.max_tokens ?? 2000, 'number', '默认输出额度')}
            ${f('max_tokens_decide', 'max_tokens_decide', ai.max_tokens_decide ?? 2500, 'number', '决策阶段')}
            ${f('max_tokens_review', 'max_tokens_review', ai.max_tokens_review ?? 3000, 'number', '复盘JSON')}
            ${f('max_tokens_playbook', 'max_tokens_playbook', ai.max_tokens_playbook ?? 5000, 'number', '手册重写')}
            ${f('max_tokens_pick', 'max_tokens_pick', ai.max_tokens_pick ?? 2000, 'number', '选股阶段')}
          </div>
          <div class="dim" style="margin-top:4px;font-size:12px">
            思考型模型会额外消耗 completion_thinking_tokens，输出额度不足会被截断并导致解析失败——不够就调大。
          </div>
          <div class="row" style="margin-top:6px">
            <button class="btn primary" onclick="App.saveConfig()">保存配置</button>
            <button class="btn" onclick="App.ping()">测试网关</button>
          </div>
          <div class="dim" style="margin-top:8px;font-size:12px">
            保存后即可点「立即决策」让真实模型操盘。key 只保存在本机 config.json。
          </div>
        </div>

        <div class="card"><h3>硬风控上限（AI 无法越界）</h3>
          <div class="row2">
            ${rail('max_position_pct', '单股仓位上限 %')}
            ${rail('max_total_position_pct', '总仓位上限 %')}
            ${rail('max_trades_per_day', '每日最大交易笔数')}
            ${rail('max_risk_per_trade_pct', '单笔风险上限 %')}
            ${rail('daily_loss_limit_pct', '当日亏损熔断 %')}
            ${rail('price_band_pct', '委托价偏离上限 %')}
            ${rail('forbid_buy_pct', '当日涨幅超此值禁买 %')}
            ${rail('forbid_sell_pct', '当日跌幅超此值禁卖 %')}
            ${rail('min_price', '最低可买价')}
            ${rail('max_price', '最高可买价')}
          </div>
          <div class="row" style="margin-top:8px"><button class="btn primary" onclick="App.saveConfig(true)">保存风控</button></div>
        </div>
      </div>

      <div class="card" style="margin-top:14px"><h3>自动调度</h3>
        <div class="row">
          <label class="row" style="gap:6px"><input type="checkbox" id="sch-enabled" ${s.enabled ? 'checked' : ''} style="width:auto"> 启用自动运行</label>
          <label class="row" style="gap:6px">盘中每 <input id="sch-decide" type="number" style="width:80px" value="${s.decide_every_min}"> 分钟决策</label>
          <label class="row" style="gap:6px">选股时间 <input id="sch-pick" style="width:90px" value="${s.pick_at}"></label>
          <label class="row" style="gap:6px">复盘时间 <input id="sch-review" style="width:90px" value="${s.review_at}"></label>
          <button class="btn primary" onclick="App.saveSchedule()">保存调度</button>
        </div>
        <div class="dim" style="margin-top:8px;font-size:12px">
          调度只在交易时段动作；每分钟记录一次权益。关闭后仍可用下方按钮手动触发。
        </div>
      </div>

      <div class="card" style="margin-top:14px"><h3>邮件通知（买卖提醒 / 复盘 / 次日计划）</h3>
        <div id="mail-body" class="dim">加载中…</div>
      </div>

      <div class="card" style="margin-top:14px"><h3>扩展数据源（可选：财报 / 股东 / 龙虎榜）</h3>
        <div id="altdata-body" class="dim">检查中…</div>
        <div class="dim" style="margin-top:8px;font-size:12px">
          安装后自动启用并带 6 小时缓存：<code>pip install akshare</code>（免费）或 <code>pip install tushare</code>
          （需在 config.json → ai.tushare_token 填 token）。不装也能跑，AI 只是少一份参考资料。
        </div>
      </div>

      <div class="card" style="margin-top:14px"><h3>数据备份（账户 / 成交 / AI 记忆）</h3>
        <div id="backup-body" class="dim">加载中…</div>
        <div class="row" style="margin-top:10px">
          <button class="btn primary" onclick="App.doBackup()">立即备份</button>
          <button class="btn" onclick="App.loadBackups()">刷新列表</button>
          <span class="dim" style="font-size:12px">备份含 paper_state（账户与成交）、playbook（手册）、journal（日记）、lessons（教训）、plans（计划单）</span>
        </div>
      </div>

      <div class="card" style="margin-top:14px"><h3>手动操作</h3>
        <div class="row">
          <button class="btn" onclick="App.run('scan')">全市场扫描</button>
          <button class="btn" onclick="App.run('pick')">AI 选股</button>
          <button class="btn primary" onclick="App.run('decide')">AI 决策</button>
          <button class="btn" onclick="App.run('review')">收盘复盘</button>
          <button class="btn" onclick="App.run('daily')">生成日报</button>
          <button class="btn" onclick="App.run('plan')">生成次日计划</button>
          <span class="spacer"></span>
          <button class="btn" onclick="App.reset()">重置模拟盘</button>
        </div>
        <div class="dim" style="margin-top:8px;font-size:12px">
          重置只清空模拟资金与持仓，AI 的策略手册、日记、教训会保留（记忆不丢）。
        </div>
      </div>`;
    S.style = null;
    loadBackups();
    loadAltdata();
    loadMail();
  }

  async function saveConfig(railsOnly) {
    const body = { ai: {}, rails: {}, schedule: {} };
    if (!railsOnly) {
      body.ai.provider = $('cfg-provider').value;
      body.ai.base_url = $('cfg-base_url').value.trim();
      body.ai.model = $('cfg-model').value.trim();
      const k = $('cfg-api_key').value.trim();
      if (k) body.ai.api_key = k;
      body.ai.temperature = parseFloat($('cfg-temperature').value || 0.4);
      ['max_tokens', 'max_tokens_decide', 'max_tokens_review',
        'max_tokens_playbook', 'max_tokens_pick'].forEach(k => {
          const el = $('cfg-' + k);
          if (el && el.value) body.ai[k] = parseInt(el.value, 10);
        });
    }
    ['max_position_pct', 'max_total_position_pct', 'max_trades_per_day',
      'max_risk_per_trade_pct', 'daily_loss_limit_pct', 'price_band_pct',
      'forbid_buy_pct', 'forbid_sell_pct', 'min_price', 'max_price'].forEach(k => {
        const el = $('rail-' + k);
        if (el) body.rails[k] = parseFloat(el.value);
      });
    try {
      const r = await post('/api/config', body);
      toast('已保存：' + (r.changed || []).join(', '));
      viewSettings();
    } catch (e) { toast('保存失败：' + e.message, true); }
  }

  async function saveSchedule() {
    try {
      await post('/api/config', {
        schedule: {
          enabled: $('sch-enabled').checked,
          decide_every_min: parseFloat($('sch-decide').value || 30),
          pick_at: $('sch-pick').value.trim(),
          review_at: $('sch-review').value.trim()
        }
      });
      toast('调度已保存');
    } catch (e) { toast('保存失败：' + e.message, true); }
  }

  async function ping() {
    toast('测试网关中…（最多 2 分钟）');
    try {
      const r = await post('/api/run', { kind: 'decide' });   // 先用真实任务验证
      watchJob(r.job);
    } catch (e) { toast('失败：' + e.message, true); }
  }

  /* ---------------- 任务与日志 ---------------- */
  async function run(kind) {
    try {
      const r = await post('/api/run', { kind });
      toast('已启动：' + kind + '（可同时跑多个任务）');
      if (kind === 'scan') { pollCandidates(); }
      watchJob(r.job);
    } catch (e) { toast('启动失败：' + e.message, true); }
  }
  function pollCandidates() {
    const t = setInterval(async () => {
      const c = await api('/api/candidates');
      if (!c.running && c.items && c.items.length) { clearInterval(t); if (S.tab === 'pool') viewPool(); }
    }, 3000);
  }

  /* ---------------- 多任务并发面板 ---------------- */
  function watchJob(id, silent) {
    if (!id) return;
    S.job = id;
    if (!S.jobTimer) S.jobTimer = setInterval(tickJobs, 1800);
    if (!silent) $('jobpanel').classList.remove('hidden');
    tickJobs();
  }
  async function tickJobs() {
    let list;
    try { list = await api('/api/jobs'); } catch (e) { return; }
    S.jobs = list;
    renderJobList(list);
    if (!S.job && list.running && list.running.length) S.job = list.running[0];
    if (!S.job) return;
    try {
      const j = await api('/api/job?id=' + S.job);
      const log = $('job-log');
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
      log.textContent = j.log || '（暂无输出）';
      if (atBottom) log.scrollTop = log.scrollHeight;
      $('job-title').textContent = '任务 ' + j.id + ' · ' + j.kind;
      const st = $('job-status');
      st.textContent = j.status === 'running'
        ? (j.waiting ? '排队中' : 'running') : j.status;
      st.className = 'pill ' + (j.status === 'running'
        ? (j.waiting ? 'off' : 'run') : (j.status === 'done' ? 'live' : 'off'));
      // 任务结束后刷新当前页
      if (j.status !== 'running' && !S._refreshed?.[j.id]) {
        S._refreshed = S._refreshed || {};
        S._refreshed[j.id] = true;
        toast(j.status === 'done' ? (j.id + ' 完成') : (j.id + ' 失败，看日志'),
          j.status !== 'done');
        setTimeout(() => (S.tab === 'dashboard' ? refreshDashboard() : route(S.tab, true)), 900);
      }
    } catch (e) { /* 忽略单次失败 */ }
  }
  function renderJobList(list) {
    const box = $('job-list');
    if (!box) return;
    const items = (list.items || []).slice(0, 8);
    box.innerHTML = items.map(j => {
      const cls = j.status === 'running' ? 'run' : (j.status === 'done' ? 'live' : 'off');
      const label = j.status === 'running'
        ? (j.waiting ? '排队' : '运行中') : (j.status === 'done' ? '完成' : '失败');
      const sel = j.id === S.job ? ' style="border-color:#33507a;background:#1a2436"' : '';
      return `<span class="tag ${cls}"${sel} onclick="App.selectJob('${j.id}')"
        title="点击查看日志">${j.id}:${j.kind}·${label}</span>`;
    }).join(' ');
    if (S.jobs.running && S.jobs.running.length > 1) {
      box.innerHTML += ` <span class="dim" style="font-size:11px">（并发 ${S.jobs.running.length}/${S.jobs.max_concurrent}）</span>`;
    }
  }
  function selectJob(id) {
    S.job = id;
    $('jobpanel').classList.remove('hidden');
    if (!S.jobTimer) S.jobTimer = setInterval(tickJobs, 1800);
    tickJobs();
  }
  function closeJob() { $('jobpanel').classList.add('hidden'); }

  async function reset() {
    if (!confirm('确认重置模拟盘？将清空资金与持仓（手册/日记/教训保留）')) return;
    try { await post('/api/reset', {}); toast('已重置'); route(S.tab, true); }
    catch (e) { toast('失败：' + e.message, true); }
  }

  /* ---------------- 扩展数据源状态 ---------------- */
  async function loadAltdata() {
    const box = $('altdata-body');
    if (!box) return;
    try {
      const r = await api('/api/altstatus');
      const tags = ['akshare', 'tushare'].map(k =>
        `<span class="tag ${r[k] ? 'ok' : ''}">${k} ${r[k] ? '已安装' : '未安装'}</span>`).join('');
      box.innerHTML = `<div class="row">${tags}</div>
        <div class="dim" style="font-size:12px;margin-top:6px">${esc(r.note || '')}</div>`;
    } catch (e) { box.innerHTML = '<div class="dim">状态获取失败</div>'; }
  }

  function go(tab) { location.hash = '#' + tab; route(tab, true); }

  /* ---------------- 数据备份 ---------------- */
  async function loadBackups() {
    const box = $('backup-body');
    if (!box) return;
    try {
      const b = await api('/api/backups');
      const items = (b.items || []).filter(x => x.exists);
      const missing = (b.items || []).filter(x => !x.exists);
      const list = (b.backups || []).map(x => `<div class="item" onclick="App.doRestore('${esc(x.name)}')">
          <div class="row"><span class="mono" style="font-size:12px">${esc(x.name)}</span>
          <span class="spacer"></span><span class="meta">${(x.size / 1024 / 1024).toFixed(2)} MB · ${esc(x.mtime)}</span></div>
        </div>`).join('');
      box.innerHTML = `<div class="row" style="margin-bottom:8px">
          <span class="tag ok">核心数据 ${items.length} 项齐全</span>
          ${missing.length ? `<span class="tag warn">${missing.length} 项待生成</span>` : ''}
          <span class="tag">备份 ${b.backup_count || 0} 份</span></div>
        <div class="list" style="max-height:200px">${list || '<div class="dim">还没有备份，点「立即备份」</div>'}</div>
        <div class="dim" style="font-size:12px;margin-top:6px">点备份条目可恢复到该时间点（恢复前会自动先备份当前数据）。</div>`;
    } catch (e) { box.innerHTML = '<div class="dim">读取失败：' + esc(e.message) + '</div>'; }
  }

  async function doBackup() {
    try {
      const r = await post('/api/backup', { reason: 'web' });
      toast('已备份：' + r.file + (r.verified ? '（校验通过）' : ''));
      loadBackups();
    } catch (e) { toast('备份失败：' + e.message, true); }
  }

  async function doRestore(name) {
    if (!confirm('恢复到这个备份？当前数据会先自动备份一份。\n' + name)) return;
    try {
      const r = await post('/api/restore', { name });
      toast('已恢复，准备刷新');
      setTimeout(() => route(S.tab, true), 600);
    } catch (e) { toast('恢复失败：' + e.message, true); }
  }

  /* ---------------- 路由 ---------------- */
  const VIEWS = {
    dashboard: viewDashboard, kline: viewKline, decisions: viewDecisions,
    pool: viewPool, holdings: viewHoldings, lhb: viewLhb, review: viewReview,
    style: viewStyle, settings: viewSettings
  };
  async function route(tab, force) {
    tab = tab || (location.hash || '#dashboard').slice(1);
    if (!VIEWS[tab]) tab = 'dashboard';
    if (!force && S.tab === tab && S[tab + 'Loaded']) return;
    if (tab !== 'kline') { try { stopAuto(); } catch (e) { } }
    S.tab = tab;
    document.querySelectorAll('.tabs a').forEach(a =>
      a.classList.toggle('active', a.dataset.tab === tab));
    if (!force || !$('view').firstElementChild) {
      $('view').innerHTML = '<div class="loading">加载中…</div>';
    }
    try { await VIEWS[tab](); }
    catch (e) {
      $('view').innerHTML = `<div class="card"><h3>加载失败</h3><pre>${esc(e.message)}</pre>
        <div class="dim">检查后端服务是否在运行：python main.py serve</div></div>`;
    }
  }

  /* ---------------- 初始化 ---------------- */
  function init() {
    window.addEventListener('hashchange', () => route());
    $('btn-refresh').onclick = () => route(S.tab, true);
    route();
    setInterval(() => {
      if (S.tab === 'dashboard') refreshDashboard();
    }, 15000);
  }
  document.addEventListener('DOMContentLoaded', init);

  return {
    route, run, watchJob, closeJob, reset, saveConfig, saveSchedule, ping,
    showDecision, showPrompt, showReport, showJournal, showPb, showFailed,
    addWatch, saveWatch, openKline, selectJob, setKlinePeriod, setKlineDays,
    toggleAuto, refreshKline, drawChart, refreshDashboard, loadBackups, doBackup, doRestore,
    loadArchive, loadLhb, loadStockAlt, saveHoldings, addHoldRow, delHoldRow,
    go, loadMail, saveMail, testMail, renderHoldAdvice, loadMyHoldDashboard
  };
})();
