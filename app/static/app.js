/* 经营分析前端：筛选 → 汇总/对照 → 下钻 → 导出 */
'use strict';

const $ = (id) => document.getElementById(id);
const BLANK = '（空白）';
const DIM_LABEL = { date: '日期', store: '门店', channel: '渠道' };
const METRIC_LABEL = { orders: '订单金额', refunds: '退款金额', spend: '投放花费' };
const METRIC_FIELD = { orders: 'order_amount', refunds: 'refund_amount', spend: 'spend' };
const SERIES = [
  { key: 'order_amount', label: '订单金额', color: 'var(--series-1)' },
  { key: 'refund_amount', label: '退款金额', color: 'var(--series-2)' },
  { key: 'spend', label: '投放花费', color: 'var(--series-3)' },
];

const state = {
  mode: 'correct',
  meta: null,
  lastSummary: null,
  lastCompare: null,
};

/* ---------------- 工具 ---------------- */

function fmt(v) {
  if (v === null || v === undefined) return null;
  return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c) n.append(c);
  return n;
}
function currentGroupBy() {
  const gb = [];
  if ($('gb-date').checked) gb.push('date');
  if ($('gb-store').checked) gb.push('store');
  if ($('gb-channel').checked) gb.push('channel');
  if (!gb.length) { $('gb-date').checked = true; gb.push('date'); }
  return gb;
}
function queryString(extra = {}) {
  const p = new URLSearchParams({
    mode: state.mode,
    date_from: $('f-date-from').value,
    date_to: $('f-date-to').value,
    store: $('f-store').value,
    channel: $('f-channel').value,
    group_by: currentGroupBy().join(','),
    refund_date_basis: $('c-refund-basis').value,
    blank_channel: $('c-blank-channel').value,
  });
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p.toString();
}
async function api(path) {
  const r = await fetch(path);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

/* ---------------- 指标卡片 ---------------- */

function renderCards(cards, spendSplittable) {
  const wrap = $('cards');
  wrap.replaceChildren();
  const defs = [
    { m: 'orders', sub: (c) => `订单数 ${c.cnt} · 点击下钻明细` },
    { m: 'refunds', sub: (c) => `退款笔数 ${c.cnt} · 点击下钻明细` },
    { m: 'spend', sub: () => '按日期+渠道记录 · 点击下钻明细' },
  ];
  defs.forEach(({ m, sub }, i) => {
    const c = cards[m];
    const card = el('div', { class: 'card', onclick: () => openDrawer(m, null) },
      el('span', { class: 'drill', text: '明细 ›' }),
      el('div', { class: 'label' },
        el('span', { class: 'swatch', style: `background:${SERIES[i].color}` }),
        document.createTextNode(METRIC_LABEL[m])),
      el('div', { class: 'value', text: c.amount === null ? '—' : '¥ ' + fmt(c.amount) }),
      el('div', { class: 'sub', text: sub(c) }));
    if (m === 'spend') {
      if (c.store_filter_ignored)
        card.append(el('div', { class: 'badge warn', text: '投放无门店维度：门店筛选未作用于本卡片' }));
      if (!spendSplittable)
        card.append(el('div', { class: 'badge warn', text: '已按门店分组：行内投放显示「不可拆分」' }));
    }
    wrap.append(card);
  });
}

/* ---------------- 趋势图（SVG 分组柱状图） ---------------- */

const SVGNS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs = {}) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}
function topRounded(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, h, w / 2));
  return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} `
       + `L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
}
function niceScale(maxVal) {
  if (!maxVal || maxVal <= 0) return { max: 1, step: 0.5 };
  const pow = Math.pow(10, Math.floor(Math.log10(maxVal)));
  for (const m of [1, 2, 2.5, 5, 10]) {
    const step = pow * m / 2;
    if (maxVal / step <= 5) return { max: Math.ceil(maxVal / step) * step, step };
  }
  return { max: pow * 10, step: pow * 2 };
}

function renderChart(daily) {
  const box = $('chart');
  box.replaceChildren();
  $('chart-legend').replaceChildren(...SERIES.map(s =>
    el('span', {}, el('span', { class: 'swatch', style: `background:${s.color}` }),
      document.createTextNode(s.label))));

  if (!daily.length) { box.append(el('p', { class: 'hint', text: '当前筛选下无数据' })); return; }

  const BAR_W = 22, GAP = 2, GROUP_PAD = 18, TOP = 14, BOTTOM = 26, LEFT = 56, H = 220;
  const groupW = SERIES.length * BAR_W + (SERIES.length - 1) * GAP + GROUP_PAD * 2;
  const W = LEFT + daily.length * groupW + 8;
  const maxV = Math.max(...daily.flatMap(d => SERIES.map(s => d[s.key] ?? 0)));
  const { max, step } = niceScale(maxV);
  const y = (v) => TOP + (H - TOP - BOTTOM) * (1 - v / max);

  const svg = svgEl('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}` });
  const css = getComputedStyle(document.documentElement);
  const ink3 = css.getPropertyValue('--ink-3').trim();
  const grid = css.getPropertyValue('--gridline').trim();
  const base = css.getPropertyValue('--baseline').trim();

  for (let v = 0; v <= max + 1e-9; v += step) {          // 网格线 + 刻度（弱化）
    const yy = y(v);
    svg.append(svgEl('line', { x1: LEFT, x2: W - 8, y1: yy, y2: yy, stroke: grid, 'stroke-width': 1 }));
    const t = svgEl('text', { x: LEFT - 8, y: yy + 4, 'text-anchor': 'end', 'font-size': 11, fill: ink3 });
    t.style.fontVariantNumeric = 'tabular-nums';
    t.textContent = v.toLocaleString('zh-CN');
    svg.append(t);
  }
  svg.append(svgEl('line', { x1: LEFT, x2: W - 8, y1: y(0), y2: y(0), stroke: base, 'stroke-width': 1 }));

  const tooltip = $('tooltip');
  daily.forEach((d, i) => {
    const gx = LEFT + i * groupW + GROUP_PAD;
    SERIES.forEach((s, j) => {
      const v = d[s.key];
      const bx = gx + j * (BAR_W + GAP);
      let mark = null;
      if (v !== null && v !== undefined && v > 0) {
        const h = y(0) - y(v);
        mark = svgEl('path', { d: topRounded(bx, y(v), BAR_W, h, 4), fill: s.color });
        svg.append(mark);
      }
      const hit = svgEl('rect', {   // 命中区域大于柱体本身
        x: bx - GAP / 2, y: TOP, width: BAR_W + GAP, height: H - TOP - BOTTOM,
        fill: 'transparent',
      });
      hit.addEventListener('mouseenter', () => { if (mark) mark.setAttribute('fill-opacity', '0.65'); });
      hit.addEventListener('mousemove', (e) => {
        tooltip.replaceChildren(
          el('div', { class: 't-row', text: d.date }),
          ...SERIES.map((ss) => {
            const val = d[ss.key];
            const row = el('div', { class: 't-row' },
              el('span', { class: 'swatch', style: `background:${ss.color}` }),
              document.createTextNode(`${ss.label}：`));
            row.append(el('b', { class: 't-val', text: val === null || val === undefined ? '无记录' : '¥ ' + fmt(val) }));
            if (ss === s) row.style.fontWeight = '600';
            return row;
          }));
        tooltip.classList.remove('hidden');
        tooltip.style.left = Math.min(e.clientX + 14, innerWidth - 240) + 'px';
        tooltip.style.top = (e.clientY + 14) + 'px';
      });
      hit.addEventListener('mouseleave', () => {
        if (mark) mark.setAttribute('fill-opacity', '1');
        tooltip.classList.add('hidden');
      });
      svg.append(hit);
    });
    const xl = svgEl('text', {
      x: gx + (SERIES.length * BAR_W + (SERIES.length - 1) * GAP) / 2,
      y: H - 8, 'text-anchor': 'middle', 'font-size': 11, fill: ink3,
    });
    xl.textContent = d.date.slice(5);
    svg.append(xl);
  });
  box.append(svg);
}

/* ---------------- 正确模式：结果表 ---------------- */

function metricCell(metric, value, row, splittable) {
  const td = el('td', { class: 'num' });
  if (metric === 'spend' && !splittable) {
    td.className = 'num unsplit';
    td.textContent = '不可拆分';
    td.title = '投放花费没有门店维度，不能把整天花费复制到每家门店';
    return td;
  }
  if (value === null || value === undefined) {
    td.className = 'num missing';
    td.textContent = '—';
    td.title = '无记录（缺少数据，非实际为零）';
    return td;
  }
  td.className = 'num clickable';
  td.textContent = fmt(value);
  td.title = '点击查看参与计算的明细与 SQL';
  td.addEventListener('click', () => openDrawer(metric, row));
  return td;
}

function renderCorrectTable(data) {
  $('result-title').textContent = '汇总结果（点击金额单元格下钻）';
  const gb = data.group_by;
  const t = $('result-table');
  t.replaceChildren();
  const head = el('tr', {},
    ...gb.map((d) => el('th', { text: DIM_LABEL[d] })),
    el('th', { class: 'num', text: '订单金额' }), el('th', { class: 'num', text: '订单数' }),
    el('th', { class: 'num', text: '退款金额' }), el('th', { class: 'num', text: '退款笔数' }),
    el('th', { class: 'num', text: '投放花费' }));
  t.append(el('thead', {}, head));
  const body = el('tbody');
  for (const r of data.rows) {
    body.append(el('tr', {},
      ...gb.map((d) => el('td', { text: r[d] ?? '—' })),
      metricCell('orders', r.order_amount, r, true),
      el('td', { class: 'num', text: r.order_cnt ?? '—' }),
      metricCell('refunds', r.refund_amount, r, true),
      el('td', { class: 'num', text: r.refund_cnt ?? '—' }),
      metricCell('spend', r.spend, r, data.spend_splittable)));
  }
  const c = data.cards;
  body.append(el('tr', { class: 'total-row' },
    el('td', { text: '合计', colspan: gb.length }),
    el('td', { class: 'num', text: fmt(c.orders.amount) }),
    el('td', { class: 'num', text: c.orders.cnt }),
    el('td', { class: 'num', text: fmt(c.refunds.amount) }),
    el('td', { class: 'num', text: c.refunds.cnt }),
    el('td', { class: 'num', text: fmt(c.spend.amount) })));
  t.append(body);

  const note = $('spend-note');
  if (!data.spend_splittable) {
    note.textContent = '⚠ 当前按门店分组：投放花费没有门店维度，行内显示「不可拆分」，'
      + '不会把整天花费复制到每家门店；合计行为全量投放。';
    note.classList.remove('hidden');
  } else if (c.spend.store_filter_ignored) {
    note.textContent = '⚠ 已筛选门店：投放花费没有门店维度，投放列与合计未按门店过滤（保持全量）。';
    note.classList.remove('hidden');
  } else {
    note.classList.add('hidden');
  }
}

/* ---------------- 错误模式：对照表 ---------------- */

function renderCompare(data) {
  $('result-title').textContent = '对照：正确口径 vs 错误直接关联';
  const gb = data.group_by ?? state.lastSummary?.group_by ?? currentGroupBy();
  const keyOf = (r) => gb.map((d) => r[d]).join('');
  const cMap = new Map(data.correct_rows.map((r) => [keyOf(r), r]));
  const wMap = new Map(data.wrong_rows.map((r) => [keyOf(r), r]));
  const keys = [...new Set([...cMap.keys(), ...wMap.keys()])].sort();

  const t = $('result-table');
  t.replaceChildren();
  const metrics = ['orders', 'refunds', 'spend'];
  t.append(el('thead', {},
    el('tr', {},
      ...gb.map((d) => el('th', { rowspan: 2, text: DIM_LABEL[d] })),
      ...metrics.map((m) => el('th', { class: 'num', colspan: 3, text: METRIC_LABEL[m] }))),
    el('tr', {}, ...metrics.flatMap(() => ['正确', '错误', '差异'].map((x) => el('th', { class: 'num', text: x }))))));

  const body = el('tbody');
  const totals = { c: { orders: 0, refunds: 0, spend: 0 }, w: { orders: 0, refunds: 0, spend: 0 } };
  for (const k of keys) {
    const c = cMap.get(k), w = wMap.get(k);
    const tr = el('tr', {}, ...gb.map((d, i) => el('td', { text: k.split('')[i] })));
    for (const m of metrics) {
      const f = METRIC_FIELD[m];
      const cv = c ? c[f] : null;
      const wv = w ? w[f] : null;
      tr.append(el('td', { class: 'num' + (cv === null || cv === undefined ? ' missing' : ''), text: cv === null || cv === undefined ? '—' : fmt(cv) }));
      if (!w) {
        const td = el('td', { class: 'num lost', colspan: 2 });
        td.append(el('span', { class: 'pill', text: '整行丢失' }));
        td.title = '该分组只有退款/投放、没有订单，直接关联后整行丢失';
        tr.append(td);
      } else {
        tr.append(el('td', { class: 'num' + (wv === null || wv === undefined ? ' missing' : ''), text: wv === null || wv === undefined ? '—' : fmt(wv) }));
        const diff = (wv ?? 0) - (cv ?? 0);
        const dt = el('td', { class: 'num' });
        if (Math.abs(diff) > 1e-9) {
          dt.textContent = (diff > 0 ? '+' : '−') + fmt(Math.abs(diff));
          dt.classList.add(diff > 0 ? 'diff-pos' : 'diff-neg');
          dt.title = diff > 0 ? '错误口径多计' : '错误口径少计';
        } else dt.textContent = '0.00';
        tr.append(dt);
      }
      totals.c[m] += cv ?? 0;
      totals.w[m] += wv ?? 0;
    }
    body.append(tr);
  }
  const trT = el('tr', { class: 'total-row' }, el('td', { text: '合计', colspan: gb.length }));
  for (const m of metrics) {
    const diff = totals.w[m] - totals.c[m];
    trT.append(el('td', { class: 'num', text: fmt(totals.c[m]) }),
      el('td', { class: 'num', text: fmt(totals.w[m]) }),
      el('td', { class: 'num ' + (Math.abs(diff) > 1e-9 ? (diff > 0 ? 'diff-pos' : 'diff-neg') : ''), text: (diff > 0 ? '+' : diff < 0 ? '−' : '') + fmt(Math.abs(diff)) }));
  }
  body.append(trT);
  t.append(body);

  const note = $('spend-note');
  note.textContent = '⚠ 错误示例：订单明细 × 退款 × 投放 直接关联，'
    + '一单多次退款 × 多条商品明细互相扇出，金额被重复计算；只有退款/投放、没有订单的分组整行丢失。';
  note.classList.remove('hidden');

  renderDupPanel(data);
}

function renderDupPanel(data) {
  const p = $('dup-panel');
  p.classList.remove('hidden');
  p.replaceChildren();
  p.append(el('h3', { text: '哪些原始记录被重复计算了？' }),
    el('p', { class: 'lede', text: '错误关联展开到行级后，统计每条原始记录参与求和的次数；被计次数 > 1 即重复计算。' }));
  const t = el('table');
  t.append(el('thead', {}, el('tr', {},
    ...['来源', '原始记录', '原始金额', '被计次数', '多计金额'].map((h, i) => el('th', { class: i >= 2 ? 'num' : '', text: h })))));
  const tb = el('tbody');
  if (!data.duplicated.length)
    tb.append(el('tr', {}, el('td', { colspan: 5, class: 'missing', text: '当前筛选下没有记录被重复计算' })));
  for (const d of data.duplicated) {
    tb.append(el('tr', {},
      el('td', { text: d.kind }),
      el('td', { text: d.label }),
      el('td', { class: 'num', text: fmt(d.amount) }),
      el('td', { class: 'num' }, el('span', { class: 'pill times', text: `× ${d.times}` })),
      el('td', { class: 'num diff-pos', text: '+' + fmt(d.amount * (d.times - 1)) })));
  }
  t.append(tb);
  p.append(t);
  if (data.dropped.length) {
    p.append(el('h3', { text: '被直接关联丢弃的记录' }),
      el('p', { class: 'lede', text: '以订单明细为主表 LEFT JOIN，没有订单承接的投放记录整列丢失：' }));
    const ul = el('ul');
    for (const d of data.dropped)
      ul.append(el('li', { text: `${d.kind} ${d.label}：¥ ${fmt(d.amount)}` }));
    p.append(ul);
  }
}

/* ---------------- SQL 步骤 ---------------- */

function renderSteps(steps) {
  const box = $('sql-steps');
  box.replaceChildren();
  steps.forEach((s, i) => {
    const det = el('details', { class: 'sql-step' });
    if (s.title.includes('实际执行')) det.open = true;
    det.append(el('summary', { text: s.title }), el('pre', { class: 'sql', text: s.sql }));
    box.append(det);
  });
}

/* ---------------- 下钻抽屉 ---------------- */

const COL_LABEL = {
  line_id: '明细行', order_id: '订单号', order_date: '下单日期', store: '门店',
  channel: '渠道', product: '商品', qty: '数量', amount: '金额',
  refund_id: '退款号', refund_date: '退款日期', reason: '原因',
  order_date_of_original: '原单日期', id: '编号', spend_date: '日期', spend: '花费',
  note: '备注', times: '被计次数', counted: '计入金额',
};
const COL_ORDER = ['line_id', 'refund_id', 'id', 'order_id', 'order_date', 'order_date_of_original',
  'refund_date', 'spend_date', 'store', 'channel', 'product', 'qty', 'reason', 'note',
  'amount', 'spend', 'times', 'counted'];

async function openDrawer(metric, row) {
  const extra = { metric };
  const bucket = {};
  if (row) {
    for (const d of currentGroupBy()) {
      extra['b_' + d] = row[d];
      bucket[d] = row[d];
    }
  }
  let data;
  try {
    data = await api('/api/detail?' + queryString(extra));
  } catch (e) {
    alert('明细查询失败：' + e.message);
    return;
  }
  $('drawer-title').textContent =
    METRIC_LABEL[metric] + (state.mode === 'wrong' ? '（错误口径下的明细）' : ' · 参与计算的明细');
  $('drawer-bucket').replaceChildren(...Object.entries(bucket).map(([d, v]) =>
    el('span', { class: 'chip', text: `${DIM_LABEL[d]}：${v}` })));
  const noteEl = $('drawer-note');
  if (data.note) { noteEl.textContent = '⚠ ' + data.note; noteEl.classList.remove('hidden'); }
  else noteEl.classList.add('hidden');

  const t = $('drawer-table');
  t.replaceChildren();
  const recs = data.records || [];
  if (!recs.length) {
    t.append(el('tbody', {}, el('tr', {}, el('td', { class: 'missing', text: data.splittable === false ? '' : '无记录' }))));
  } else {
    const cols = [...new Set(recs.flatMap((r) => Object.keys(r)))]
      .sort((a, b) => (COL_ORDER.indexOf(a) + 99) - (COL_ORDER.indexOf(b) + 99));
    t.append(el('thead', {}, el('tr', {}, ...cols.map((c) =>
      el('th', { class: ['amount', 'spend', 'counted', 'times', 'qty'].includes(c) ? 'num' : '', text: COL_LABEL[c] || c })))));
    const tb = el('tbody');
    for (const r of recs) {
      tb.append(el('tr', {}, ...cols.map((c) => {
        const v = r[c];
        if (c === 'times') return el('td', { class: 'num' }, el('span', { class: 'pill times', text: `× ${v}` }));
        if (['amount', 'spend', 'counted'].includes(c)) return el('td', { class: 'num', text: fmt(v) });
        return el('td', { text: v === '' ? BLANK : (v ?? '—') });
      })));
    }
    t.append(tb);
  }
  $('drawer-sql').textContent = data.sql || '（无）';
  $('drawer').classList.remove('hidden');
  $('drawer-mask').classList.remove('hidden');
}
function closeDrawer() {
  $('drawer').classList.add('hidden');
  $('drawer-mask').classList.add('hidden');
}

/* ---------------- 主流程 ---------------- */

async function run() {
  const qs = queryString();
  const main = document.querySelector('main');
  main.classList.add('loading');   // 刷新期间保留旧渲染，仅降低不透明度
  try {
    if (state.mode === 'correct') {
      const data = await api('/api/summary?' + qs);
      state.lastSummary = data;
      $('dup-panel').classList.add('hidden');
      renderCards(data.cards, data.spend_splittable);
      renderChart(data.daily);
      renderCorrectTable(data);
      renderSteps(data.sql_steps);
      $('chart-note').textContent = data.cards.spend.store_filter_ignored
        ? '（投放花费无门店维度，图中为未按门店过滤的全量）' : '';
    } else {
      const [sum, cmp] = await Promise.all([
        api('/api/summary?' + queryString({ mode: 'correct' })), api('/api/compare?' + qs)]);
      state.lastSummary = sum;
      state.lastCompare = cmp;
      cmp.group_by = sum.group_by;
      renderCards(sum.cards, sum.spend_splittable);
      renderChart(sum.daily);
      renderCompare(cmp);
      renderSteps([
        { title: '错误示例：直接关联（实际执行的 SQL）', sql: cmp.wrong_sql },
        { title: '错误关联展开到行级（用于统计每条原始记录被计次数）', sql: cmp.expanded_sql },
        ...cmp.correct_steps,
      ]);
      $('chart-note').textContent = '（趋势图始终按正确口径绘制）';
    }
  } catch (e) {
    alert('查询失败：' + e.message);
  } finally {
    main.classList.remove('loading');
  }
}

function setMode(mode) {
  state.mode = mode;
  $('mode-correct').classList.toggle('active', mode === 'correct');
  $('mode-wrong').classList.toggle('active', mode === 'wrong');
  $('mode-correct').setAttribute('aria-selected', mode === 'correct');
  $('mode-wrong').setAttribute('aria-selected', mode === 'wrong');
  run();
}

async function init() {
  state.meta = await api('/api/meta');
  $('f-date-from').value = state.meta.date_min;
  $('f-date-to').value = state.meta.date_max;
  for (const s of state.meta.stores) $('f-store').append(el('option', { text: s, value: s }));
  for (const c of state.meta.channels) $('f-channel').append(el('option', { text: c, value: c }));
  $('notes-list').replaceChildren(...state.meta.notes.map((n) => el('li', { text: n })));

  $('mode-correct').addEventListener('click', () => setMode('correct'));
  $('mode-wrong').addEventListener('click', () => setMode('wrong'));
  $('btn-run').addEventListener('click', run);
  $('btn-export').addEventListener('click', () => { location.href = '/api/export?' + queryString(); });
  $('drawer-close').addEventListener('click', closeDrawer);
  $('drawer-mask').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
  for (const id of ['f-date-from', 'f-date-to', 'f-store', 'f-channel',
    'gb-date', 'gb-store', 'gb-channel', 'c-refund-basis', 'c-blank-channel'])
    $(id).addEventListener('change', run);
  run();
}
init();
