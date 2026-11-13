"""查询构建与执行。

两条路线：
1. correct_* —— 正确口径：每个指标先按自己可用的维度汇总，再对齐到查询粒度。
   投放花费没有门店维度，按门店分组/筛选时不拆分、不复制。
2. wrong_*   —— 错误示例：订单明细 × 退款 × 投放 直接关联，用于对照展示
   扇出（fan-out）如何把金额重复计算、把“只有退款的日期”弄丢。

所有 SQL 都是实际下发给 SQLite 执行的语句，页面原样展示。
"""
from db import BLANK_LABEL

DIMS = ('date', 'store', 'channel')
DIM_LABEL = {'date': '日期', 'store': '门店', 'channel': '渠道'}
METRIC_DIMS = {
    'orders': ('date', 'store', 'channel'),
    'refunds': ('date', 'store', 'channel'),
    'spend': ('date', 'channel'),          # 投放没有门店维度
}
METRIC_LABEL = {'orders': '订单金额', 'refunds': '退款金额', 'spend': '投放花费'}

CALIBER_NOTES = [
    '订单金额 = 订单明细行金额逐行求和（先按可用维度汇总，再对齐到查询粒度）。',
    '退款金额 = 退款明细逐笔求和；归属日期可选「退款发生日」或「原订单日期」。',
    '投放花费只有「日期+渠道」两个维度：按门店分组时显示「不可拆分」，绝不把整天花费复制到每家门店；筛选门店时投放总额保持全量并给出提示。',
    '不使用 SUM(DISTINCT …) 掩盖重复：样本里两笔奶茶都是 30.00 元，DISTINCT 会把合法记录合并丢掉。',
    '缺少记录（显示 —）与实际为零（显示 0.00）分别展示；只有退款没有订单的日期保留。',
]


def _chan(col):
    return f"CASE WHEN {col} = '' THEN '{BLANK_LABEL}' ELSE {col} END"


def _date_expr(metric, caliber, alias='t'):
    if metric == 'orders':
        return f'{alias}.order_date'
    if metric == 'spend':
        return f'{alias}.spend_date'
    # refunds：按口径选择退款发生日或原订单日期（原单日期来自 od 连接）
    return 'od.order_date' if caliber['refund_date_basis'] == 'order_date' else f'{alias}.refund_date'


def _refunds_from(caliber):
    src = 'refunds t'
    if caliber['refund_date_basis'] == 'order_date':
        src += (' JOIN (SELECT DISTINCT order_id, order_date FROM order_lines) od'
                ' ON od.order_id = t.order_id')
    return src


def _where(metric, filters, caliber, alias='t'):
    """指标各自可用的筛选条件。投放没有门店维度 → 忽略门店筛选。"""
    w, p = [], []
    d = _date_expr(metric, caliber, alias)
    if filters.get('date_from'):
        w.append(f'{d} >= ?')
        p.append(filters['date_from'])
    if filters.get('date_to'):
        w.append(f'{d} <= ?')
        p.append(filters['date_to'])
    if metric != 'spend' and filters.get('store'):
        w.append(f'{alias}.store = ?')
        p.append(filters['store'])
    if filters.get('channel'):
        if filters['channel'] == BLANK_LABEL:
            w.append(f"{alias}.channel = ''")
        else:
            w.append(f'{alias}.channel = ?')
            p.append(filters['channel'])
    if metric in ('orders', 'refunds') and caliber.get('blank_channel') == 'exclude':
        w.append(f"{alias}.channel <> ''")
    return ('WHERE ' + ' AND '.join(w)) if w else '', p


def _dim_select(metric, dims, caliber, alias='t'):
    out = []
    for d in dims:
        if d == 'date':
            out.append(f'{_date_expr(metric, caliber, alias)} AS date')
        elif d == 'store':
            out.append(f'{alias}.store AS store')
        else:
            out.append(f'{_chan(alias + ".channel")} AS channel')
    return out


# ---------------------------------------------------------------- 正确口径

def _metric_cte(metric, gb, filters, caliber):
    """单个指标在它自己可用维度 ∩ 查询粒度上的汇总子查询。"""
    dims = [d for d in gb if d in METRIC_DIMS[metric]]
    sel = _dim_select(metric, dims, caliber)
    where, params = _where(metric, filters, caliber)
    agg = {
        'orders': 'SUM(t.amount_cents) / 100.0 AS order_amount, '
                  'COUNT(DISTINCT t.order_id) AS order_cnt',
        'refunds': 'SUM(t.amount_cents) / 100.0 AS refund_amount, '
                   'COUNT(*) AS refund_cnt',
        'spend': 'SUM(t.spend_cents) / 100.0 AS spend',
    }[metric]
    src = {'orders': 'order_lines t',
           'refunds': _refunds_from(caliber),
           'spend': 'ad_spend_daily t'}[metric]
    cols = (', '.join(sel) + ',\n  ') if sel else ''
    grp = ('\nGROUP BY ' + ', '.join(str(i + 1) for i in range(len(sel)))) if sel else ''
    sql = f'SELECT {cols}{agg}\nFROM {src}\n{where}{grp}' if where else \
          f'SELECT {cols}{agg}\nFROM {src}{grp}'
    return sql, params


def _compose_correct(gb, filters, caliber):
    """把三个指标的子查询对齐到查询粒度：键集 UNION + 逐个 LEFT JOIN。

    返回 (完整 SQL, 参数, 各指标 CTE 片段, 投放是否可按键对齐)。
    """
    ctes, params = {}, []
    for m in ('orders', 'refunds', 'spend'):
        sql, p = _metric_cte(m, gb, filters, caliber)
        ctes[m] = sql
        params += p
    alias = {'orders': 'o', 'refunds': 'r', 'spend': 's'}
    spend_alignable = 'store' not in gb
    contrib = ['orders', 'refunds'] + (['spend'] if spend_alignable else [])
    keys_sql = '\n  UNION\n  '.join(
        f"SELECT {', '.join(gb)} FROM {alias[m]}" for m in contrib)
    joins = []
    for m in contrib:
        a = alias[m]
        on = ' AND '.join(f'k.{d} = {a}.{d}' for d in gb if d in METRIC_DIMS[m])
        joins.append(f'LEFT JOIN {a} ON {on}')
    metric_cols = ['o.order_amount', 'o.order_cnt', 'r.refund_amount', 'r.refund_cnt']
    if spend_alignable:
        metric_cols.append('s.spend')
    key_cols = ', '.join(f'k.{d}' for d in gb)
    full = (
        f"WITH o AS (\n  {ctes['orders']}\n),\n"
        f"r AS (\n  {ctes['refunds']}\n),\n"
        f"s AS (\n  {ctes['spend']}\n),\n"
        f"keys AS (\n  {keys_sql}\n)\n"
        f"SELECT {key_cols},\n       {', '.join(metric_cols)}\n"
        f"FROM keys k\n  " + '\n  '.join(joins) +
        f"\nORDER BY {key_cols}")
    return full, params, ctes, spend_alignable


def _rows(cur):
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def correct_summary(con, gb, filters, caliber):
    sql, params, ctes, spend_alignable = _compose_correct(gb, filters, caliber)
    rows = _rows(con.execute(sql, params))
    steps = [
        {'title': '① 订单金额：按自身可用维度（日期/门店/渠道）汇总',
         'sql': ctes['orders']},
        {'title': '② 退款金额：按自身可用维度（日期/门店/渠道）汇总',
         'sql': ctes['refunds']},
        {'title': '③ 投放花费：只有日期/渠道两个维度，无门店',
         'sql': ctes['spend']},
        {'title': '④ 对齐到查询粒度（键集 UNION 后逐个 LEFT JOIN）——实际执行的完整 SQL',
         'sql': sql},
    ]
    return {'rows': rows, 'sql': sql, 'steps': steps,
            'spend_splittable': spend_alignable}


def cards(con, filters, caliber):
    """顶部卡片：三个指标各自独立汇总（不过度连接）。"""
    out = {}
    specs = {
        'orders': ('SELECT SUM(t.amount_cents) / 100.0 AS amount, '
                   'COUNT(DISTINCT t.order_id) AS cnt\nFROM order_lines t'),
        'refunds': ('SELECT SUM(t.amount_cents) / 100.0 AS amount, '
                    'COUNT(*) AS cnt\nFROM ' + _refunds_from(caliber)),
        'spend': 'SELECT SUM(t.spend_cents) / 100.0 AS amount\nFROM ad_spend_daily t',
    }
    for m, head in specs.items():
        where, params = _where(m, filters, caliber)
        sql = head + ('\n' + where if where else '')
        row = con.execute(sql, params).fetchone()
        out[m] = {'amount': row['amount'], 'sql': sql}
        if m != 'spend':
            out[m]['cnt'] = row['cnt']
    out['spend']['store_filter_ignored'] = bool(filters.get('store'))
    return out


def daily_series(con, filters, caliber):
    """趋势图数据：同口径按日期汇总（投放同样忽略门店筛选）。"""
    res = correct_summary(con, ['date'], filters, caliber)
    return [{'date': r['date'], 'order_amount': r['order_amount'],
             'refund_amount': r['refund_amount'], 'spend': r.get('spend')}
            for r in res['rows']], res['sql']


# ---------------------------------------------------------------- 错误示例

def _wrong_where(filters, caliber):
    w, p = [], []
    if filters.get('date_from'):
        w.append('ol.order_date >= ?')
        p.append(filters['date_from'])
    if filters.get('date_to'):
        w.append('ol.order_date <= ?')
        p.append(filters['date_to'])
    if filters.get('store'):
        w.append('ol.store = ?')
        p.append(filters['store'])
    if filters.get('channel'):
        if filters['channel'] == BLANK_LABEL:
            w.append("ol.channel = ''")
        else:
            w.append('ol.channel = ?')
            p.append(filters['channel'])
    if caliber.get('blank_channel') == 'exclude':
        w.append("ol.channel <> ''")
    return ('WHERE ' + ' AND '.join(w)) if w else '', p


def _wrong_dim_select(gb):
    out = []
    for d in gb:
        if d == 'date':
            out.append('ol.order_date AS date')
        elif d == 'store':
            out.append('ol.store AS store')
        else:
            out.append(f"{_chan('ol.channel')} AS channel")
    return out


def wrong_summary(con, gb, filters, caliber):
    """错误写法：明细直接相连，退款/投放被订单行扇出重复计数。

    退款被关联到订单行 → 归属原单日期（退款发生日信息丢失）；
    只有退款没有订单的日期整行丢失；没有订单承接的投放记录被丢弃。
    """
    sel = _wrong_dim_select(gb)
    where, params = _wrong_where(filters, caliber)
    grp = ', '.join(str(i + 1) for i in range(len(sel)))
    sql = (
        f"SELECT {', '.join(sel)},\n"
        f"       SUM(ol.amount_cents) / 100.0 AS order_amount,\n"
        f"       SUM(rf.amount_cents) / 100.0 AS refund_amount,\n"
        f"       SUM(sp.spend_cents) / 100.0 AS spend\n"
        f"FROM order_lines ol\n"
        f"LEFT JOIN refunds rf ON rf.order_id = ol.order_id\n"
        f"LEFT JOIN ad_spend_daily sp\n"
        f"  ON sp.spend_date = ol.order_date AND sp.channel = ol.channel\n"
        f"{where + chr(10) if where else ''}"
        f"GROUP BY {grp}\nORDER BY {grp}")
    rows = _rows(con.execute(sql, params))
    return {'rows': rows, 'sql': sql}


def wrong_expanded(con, filters, caliber):
    """错误关联展开到行级：用于找出每条原始记录被重复计算了几次。"""
    where, params = _wrong_where(filters, caliber)
    sql = (
        "SELECT ol.line_id, ol.order_id, ol.order_date, ol.store, ol.channel,\n"
        "       ol.product, ol.amount_cents,\n"
        "       rf.refund_id, rf.amount_cents AS refund_cents,\n"
        "       sp.id AS spend_id, sp.spend_cents\n"
        "FROM order_lines ol\n"
        "LEFT JOIN refunds rf ON rf.order_id = ol.order_id\n"
        "LEFT JOIN ad_spend_daily sp\n"
        "  ON sp.spend_date = ol.order_date AND sp.channel = ol.channel\n"
        + (where + '\n' if where else '')
        + "ORDER BY ol.line_id, rf.refund_id")
    return sql, _rows(con.execute(sql, params))


def duplication(con, filters, caliber):
    """统计每条原始记录在错误关联中被计了几次、哪些被丢弃。"""
    sql, rows = wrong_expanded(con, filters, caliber)
    line_times, refund_times, spend_times = {}, {}, {}
    for r in rows:
        line_times[r['line_id']] = line_times.get(r['line_id'], 0) + 1
        if r['refund_id']:
            refund_times[r['refund_id']] = refund_times.get(r['refund_id'], 0) + 1
        if r['spend_id'] is not None:
            spend_times[r['spend_id']] = spend_times.get(r['spend_id'], 0) + 1

    duplicated, seen = [], set()
    for r in rows:  # 订单行：一单多次退款 → 每条明细被计 N 次
        if r['line_id'] in seen:
            continue
        seen.add(r['line_id'])
        t = line_times[r['line_id']]
        if t > 1:
            duplicated.append({
                'kind': '订单明细', 'id': r['line_id'],
                'label': f"{r['order_id']} · {r['product']}",
                'amount': r['amount_cents'] / 100.0, 'times': t})
    for rid, t in sorted(refund_times.items()):  # 退款：一单多条明细 → 每笔退款被计 N 次
        if t > 1:
            info = con.execute(
                'SELECT refund_id, order_id, amount_cents, reason FROM refunds '
                'WHERE refund_id = ?', (rid,)).fetchone()
            duplicated.append({
                'kind': '退款明细', 'id': rid,
                'label': f"{info['order_id']} · {info['reason']}",
                'amount': info['amount_cents'] / 100.0, 'times': t})
    for sid, t in sorted(spend_times.items()):  # 投放：被每个订单行各计一次
        if t > 1:
            info = con.execute(
                'SELECT spend_date, channel, spend_cents FROM ad_spend_daily '
                'WHERE id = ?', (sid,)).fetchone()
            duplicated.append({
                'kind': '每日投放', 'id': sid,
                'label': f"{info['spend_date']} · {info['channel']}",
                'amount': info['spend_cents'] / 100.0, 'times': t})

    dropped = []  # 没有订单行承接的投放记录，在 LEFT JOIN 方向下被整列丢掉
    for sp in con.execute('SELECT id, spend_date, channel, spend_cents '
                          'FROM ad_spend_daily').fetchall():
        if sp['id'] not in spend_times:
            dropped.append({
                'kind': '每日投放', 'id': sp['id'],
                'label': f"{sp['spend_date']} · {sp['channel'] or BLANK_LABEL}",
                'amount': sp['spend_cents'] / 100.0})
    return {'duplicated': duplicated, 'dropped': dropped, 'expanded_sql': sql}


def compare(con, gb, filters, caliber):
    """正确 vs 错误对照。"""
    correct = correct_summary(con, gb, filters, caliber)
    wrong = wrong_summary(con, gb, filters, caliber)
    key = lambda r: tuple(r.get(d) for d in gb)
    ckeys = {key(r) for r in correct['rows']}
    wkeys = {key(r) for r in wrong['rows']}
    dup = duplication(con, filters, caliber)
    return {
        'correct_rows': correct['rows'],
        'wrong_rows': wrong['rows'],
        'missing_keys': [list(k) for k in sorted(ckeys - wkeys)],
        'phantom_keys': [list(k) for k in sorted(wkeys - ckeys)],
        'duplicated': dup['duplicated'],
        'dropped': dup['dropped'],
        'wrong_sql': wrong['sql'],
        'expanded_sql': dup['expanded_sql'],
        'correct_steps': correct['steps'],
        'spend_splittable': correct['spend_splittable'],
    }


# ---------------------------------------------------------------- 明细下钻

def detail(con, metric, bucket, filters, caliber, mode):
    """某个指标在某个分组桶里的参与计算明细 + 实际执行的 SQL。"""
    if metric == 'spend' and bucket.get('store'):
        return {'splittable': False, 'records': [], 'sql': '',
                'note': '投放花费没有门店维度，不能按门店拆分；'
                        '请取消门店分组/筛选后查看投放明细。'}
    if mode == 'wrong':
        return _wrong_detail(con, metric, bucket, filters, caliber)

    where, params = _where(metric, filters, caliber)
    conds, extra = [], []
    for d, v in bucket.items():
        if v is None:
            continue
        if d == 'date':
            conds.append(f'{_date_expr(metric, caliber)} = ?')
        elif d == 'store':
            conds.append('t.store = ?')
        else:
            conds.append("t.channel = ''" if v == BLANK_LABEL else 't.channel = ?')
            v = '' if v == BLANK_LABEL else v
        extra.append(v)
    all_where = ' AND '.join(filter(None, [where[6:] if where else '', *conds]))
    all_where = 'WHERE ' + all_where if all_where else ''
    if metric == 'orders':
        sql = (f'SELECT t.line_id, t.order_id, t.order_date, t.store,\n'
               f"       {_chan('t.channel')} AS channel, t.product, t.qty,\n"
               f'       t.amount_cents / 100.0 AS amount\n'
               f'FROM order_lines t\n{all_where}\nORDER BY t.order_id, t.line_id')
    elif metric == 'refunds':
        sql = (f'SELECT t.refund_id, t.order_id, t.refund_date, t.store,\n'
               f"       {_chan('t.channel')} AS channel,\n"
               f'       t.amount_cents / 100.0 AS amount, t.reason\n'
               f'FROM {_refunds_from(caliber)}\n{all_where}\nORDER BY t.refund_id')
    else:
        sql = (f'SELECT t.id, t.spend_date, t.channel,\n'
               f'       t.spend_cents / 100.0 AS amount, t.note\n'
               f'FROM ad_spend_daily t\n{all_where}\nORDER BY t.spend_date, t.channel')
    records = _rows(con.execute(sql, params + extra))
    return {'splittable': True, 'records': records, 'sql': sql, 'note': ''}


def _wrong_detail(con, metric, bucket, filters, caliber):
    """错误模式下的明细：每条原始记录带「被计次数」。"""
    sql, rows = wrong_expanded(con, filters, caliber)
    def in_bucket(r):
        for d, v in bucket.items():
            if v is None:
                continue
            if d == 'date' and r['order_date'] != v:
                return False
            if d == 'store' and r['store'] != v:
                return False
            if d == 'channel' and (r['channel'] or BLANK_LABEL) != v:
                return False
        return True
    rows = [r for r in rows if in_bucket(r)]
    records = []
    if metric == 'orders':
        times = {}
        for r in rows:
            times[r['line_id']] = times.get(r['line_id'], 0) + 1
        seen = set()
        for r in rows:
            if r['line_id'] in seen:
                continue
            seen.add(r['line_id'])
            t = times[r['line_id']]
            records.append({'line_id': r['line_id'], 'order_id': r['order_id'],
                            'product': r['product'],
                            'amount': r['amount_cents'] / 100.0,
                            'times': t, 'counted': r['amount_cents'] / 100.0 * t})
    elif metric == 'refunds':
        times = {}
        for r in rows:
            if r['refund_id']:
                times[r['refund_id']] = times.get(r['refund_id'], 0) + 1
        for rid, t in sorted(times.items()):
            info = con.execute('SELECT order_id, amount_cents, reason FROM refunds '
                               'WHERE refund_id = ?', (rid,)).fetchone()
            records.append({'refund_id': rid, 'order_id': info['order_id'],
                            'reason': info['reason'],
                            'amount': info['amount_cents'] / 100.0,
                            'times': t, 'counted': info['amount_cents'] / 100.0 * t})
    else:
        times = {}
        for r in rows:
            if r['spend_id'] is not None:
                times[r['spend_id']] = times.get(r['spend_id'], 0) + 1
        for sid, t in sorted(times.items()):
            info = con.execute('SELECT spend_date, channel, spend_cents '
                               'FROM ad_spend_daily WHERE id = ?', (sid,)).fetchone()
            records.append({'id': sid, 'spend_date': info['spend_date'],
                            'channel': info['channel'],
                            'amount': info['spend_cents'] / 100.0,
                            'times': t, 'counted': info['spend_cents'] / 100.0 * t})
    note = ('错误关联下，原始记录被扇出重复计数：被计次数 > 1 的行即重复计算的来源；'
            '计入金额 = 原始金额 × 被计次数。')
    return {'splittable': True, 'records': records, 'sql': sql, 'note': note}
