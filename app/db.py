"""样本数据：订单明细 / 退款明细 / 每日投放。

三张表粒度不同，是本示例的核心：
- order_lines     订单商品明细：一单多条商品行，维度 日期+门店+渠道
- refunds         退款明细：一单可多次退款，维度 退款日期+门店+渠道
- ad_spend_daily  每日投放：按 日期+渠道 记录，【没有门店维度】

金额一律以“分”存储（INTEGER），避免浮点误差；展示时 /100.0。
"""
import sqlite3

BLANK_LABEL = '（空白）'

SCHEMA = """
CREATE TABLE order_lines (
  line_id      INTEGER PRIMARY KEY,
  order_id     TEXT NOT NULL,
  order_date   TEXT NOT NULL,
  store        TEXT NOT NULL,
  channel      TEXT NOT NULL,   -- '' 表示空白渠道（未填写）
  product      TEXT NOT NULL,
  qty          INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL
);
CREATE TABLE refunds (
  refund_id    TEXT PRIMARY KEY,
  order_id     TEXT NOT NULL,
  refund_date  TEXT NOT NULL,   -- 退款发生日，可能与原订单日期不同
  store        TEXT NOT NULL,
  channel      TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  reason       TEXT NOT NULL
);
CREATE TABLE ad_spend_daily (
  id          INTEGER PRIMARY KEY,
  spend_date  TEXT NOT NULL,
  channel     TEXT NOT NULL,
  spend_cents INTEGER NOT NULL,
  note        TEXT NOT NULL DEFAULT ''
);
"""

ORDER_LINES = [
    # line_id, order_id, date, store, channel, product, qty, amount_cents
    (1, 'O1001', '2026-11-10', '人民广场店', '美团',   '招牌奶茶', 2, 3000),
    (2, 'O1001', '2026-11-10', '人民广场店', '美团',   '加料珍珠', 1,  500),   # O1001 第二条明细
    (3, 'O1002', '2026-11-10', '人民广场店', '美团',   '美式咖啡', 1, 2000),
    (4, 'O1003', '2026-11-10', '大学城店',   '饿了么', '三明治',   1, 1500),
    (5, 'O1004', '2026-11-10', '人民广场店', '',       '芝士蛋糕', 1, 4000),   # 空白渠道
    (6, 'O1004', '2026-11-10', '人民广场店', '',       '手工饼干', 1, 1000),   # 空白渠道，同单第二条
    (7, 'O1005', '2026-11-11', '人民广场店', '美团',   '招牌奶茶', 2, 3000),   # 与 line 1 同金额 30.00
    (8, 'O1006', '2026-11-11', '大学城店',   '饿了么', '赠品杯垫', 1,    0),   # 实际为零（有记录，金额 0）
]

REFUNDS = [
    # refund_id, order_id, refund_date, store, channel, amount_cents, reason
    ('R9001', 'O1001', '2026-11-10', '人民广场店', '美团', 1000, '洒漏部分退款'),
    ('R9002', 'O1001', '2026-11-11', '人民广场店', '美团', 2500, '二次投诉补退'),  # 同一单第二次退款
    ('R9003', 'O1002', '2026-11-12', '人民广场店', '美团', 2000, '全额退款'),      # 该日没有任何订单
]

AD_SPEND = [
    # id, date, channel, spend_cents, note
    (1, '2026-11-10', '美团',   20000, ''),
    (2, '2026-11-10', '饿了么', 15000, ''),
    (3, '2026-11-11', '美团',   18000, ''),
    (4, '2026-11-11', '饿了么',     0, '当日未投放，实际为零'),
    # 2026-11-12 饿了么 无记录 → 缺少数据（与“实际为零”区分展示）
    (5, '2026-11-12', '美团',   22000, ''),
]


def build_db():
    """构建内存 SQLite 并载入样本，返回连接。"""
    con = sqlite3.connect(':memory:', check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.executemany(
        'INSERT INTO order_lines VALUES (?,?,?,?,?,?,?,?)', ORDER_LINES)
    con.executemany(
        'INSERT INTO refunds VALUES (?,?,?,?,?,?,?)', REFUNDS)
    con.executemany(
        'INSERT INTO ad_spend_daily VALUES (?,?,?,?,?)', AD_SPEND)
    return con


def meta(con):
    """页面筛选器需要的元数据。"""
    stores = [r[0] for r in con.execute(
        'SELECT DISTINCT store FROM order_lines ORDER BY store')]
    channels = [r[0] for r in con.execute(
        "SELECT DISTINCT channel FROM order_lines WHERE channel <> '' ORDER BY channel")]
    lo, hi = con.execute(
        'SELECT MIN(d), MAX(d) FROM ('
        '  SELECT order_date d FROM order_lines'
        '  UNION SELECT refund_date FROM refunds'
        '  UNION SELECT spend_date FROM ad_spend_daily)').fetchone()
    return {
        'stores': stores,
        'channels': channels + [BLANK_LABEL],
        'date_min': lo,
        'date_max': hi,
    }
