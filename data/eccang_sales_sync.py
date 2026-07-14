"""
易仓ERP API - 销量（订单）增量同步
从易仓OpenAPI拉取订单数据，增量更新到supply_chain.db
表结构：订单ID, 平台, 销售单号, 仓库代码, 账号, 国家或地区代码, 币种,
        付款时间, 仓库SKU, 数量, 单价, 产品名称, 总金额, 销售额, 运费,
        订单类型, 发货类型
"""
import requests
import json
import time
import base64
import sqlite3
import os
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, 'supply_chain.db')

# API配置
def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


APP_KEY = required_env("ECCANG_APP_KEY")
SECRET_KEY = required_env("ECCANG_SECRET_KEY")
SERVICE_ID = required_env("ECCANG_SERVICE_ID")
API_URL = "http://openapi-web.eccang.com/openApi/api/unity"
AES_IV = "1234500000054321"


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def generate_sign(params, secret_key):
    filtered = {k: v for k, v in params.items() if k != "sign" and v != "" and v is not None}
    sorted_keys = sorted(filtered.keys())
    sign_str = "&".join(f"{k}={filtered[k]}" for k in sorted_keys)
    key = secret_key.encode('utf-8')
    iv = AES_IV.encode('utf-8')
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(sign_str.encode('utf-8'), 16)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode('utf-8')


def normalize_datetime(val):
    """标准化日期时间：2026/5/5 9:59 → 2026/05/05 09:59"""
    if not val:
        return val
    val = str(val).strip()
    from datetime import datetime
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M'):
        try:
            dt = datetime.strptime(val, fmt)
            return dt.strftime('%Y/%m/%d %H:%M')
        except ValueError:
            continue
    return val


def get_orders(page=1, page_size=100, condition=None):
    """查询订单列表"""
    biz_data = {
        "page": page,
        "page_size": page_size,
        "get_detail": 1,
        "get_address": 0,
    }
    if condition:
        biz_data["condition"] = condition

    biz_content = json.dumps(biz_data, ensure_ascii=False)

    params = {
        "app_key": APP_KEY,
        "service_id": SERVICE_ID,
        "interface_method": "getOrderList",
        "biz_content": biz_content,
        "sign_type": "AES",
        "charset": "UTF-8",
        "timestamp": str(int(time.time() * 1000)),
        "nonce_str": "req" + str(int(time.time() * 1000)),
        "version": "v1.0.0"
    }

    params["sign"] = generate_sign(params, SECRET_KEY)
    response = requests.post(API_URL, json=params, timeout=30)
    result = response.json()

    code = result.get("code")
    if str(code) != "200":
        raise Exception(f"API错误: code={code}, msg={result.get('message')}")

    biz = result.get("biz_content", "{}")
    if isinstance(biz, str):
        biz = json.loads(biz)
    return biz


def flatten_order(order):
    """将单个订单展平为sales行（一个订单可能有多条明细）"""
    base = {
        "订单ID": order.get("order_id", ""),
        "平台": order.get("platform", ""),
        "销售单号": order.get("order_code", ""),
        "仓库代码": order.get("warehouse_code", ""),
        "账号": order.get("user_account", ""),
        "国家或地区代码": order.get("country_code", ""),
        "币种": order.get("currency", ""),
        "付款时间": normalize_datetime(order.get("platform_paid_date", "")),
        "总金额": order.get("amountpaid", "0"),
        "销售额": order.get("subtotal", "0"),
        "运费": order.get("ship_fee", "0"),
        "订单类型": order.get("order_type", ""),
        "发货类型": order.get("fulfillment_type", 0),
    }

    rows = []
    details = order.get("order_details", [])

    if details:
        for d in details:
            wh_list = d.get("warehouse_sku_list", [])
            if wh_list:
                for w in wh_list:
                    row = base.copy()
                    row["仓库SKU"] = w.get("warehouse_sku", "")
                    try:
                        row["数量"] = int(w.get("warehouse_sku_qty", 0) or 0)
                    except (ValueError, TypeError):
                        row["数量"] = 0
                    row["单价"] = d.get("unit_price", "0")
                    row["产品名称"] = str(d.get("product_title", ""))[:200]
                    rows.append(row)
            else:
                row = base.copy()
                row["仓库SKU"] = d.get("product_sku_list", "")
                try:
                    row["数量"] = int(d.get("qty", 0) or 0)
                except (ValueError, TypeError):
                    row["数量"] = 0
                row["单价"] = d.get("unit_price", "0")
                row["产品名称"] = str(d.get("product_title", ""))[:200]
                rows.append(row)
    else:
        row = base.copy()
        row["仓库SKU"] = ""
        row["数量"] = 0
        row["单价"] = "0"
        row["产品名称"] = ""
        rows.append(row)

    return rows


def sync_sales(end_date=None):
    """增量同步销量数据

    逻辑：
    1. 查数据库中最晚付款时间 → 作为增量起点
    2. 拉取从起点到end_date（默认昨天）的订单
    3. 按订单ID匹配：已存在则DELETE旧记录再INSERT新记录（订单可能拆分/合并）
    4. 没有历史数据则全量拉取
    """
    from datetime import datetime, timedelta

    conn = db_connect()
    c = conn.cursor()

    # 确保表存在
    c.execute('''CREATE TABLE IF NOT EXISTS 销量 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        订单ID TEXT,
        平台 TEXT,
        销售单号 TEXT,
        仓库代码 TEXT,
        账号 TEXT,
        国家或地区代码 TEXT,
        币种 TEXT,
        付款时间 TEXT,
        仓库SKU TEXT,
        数量 INTEGER,
        单价 REAL,
        产品名称 TEXT,
        总金额 REAL,
        销售额 REAL,
        运费 REAL,
        订单类型 TEXT,
        发货类型 INTEGER
    )''')

    # 获取最晚付款时间
    c.execute("SELECT MAX(付款时间) FROM 销量 WHERE 付款时间 IS NOT NULL AND 付款时间 != ''")
    max_time_row = c.fetchone()
    max_time = max_time_row[0] if max_time_row else None

    if not end_date:
        # 默认截至昨天
        yesterday = datetime.now() - timedelta(days=1)
        end_date = yesterday.strftime('%Y-%m-%d')

    # 构建查询条件
    condition = {
        "platform_paid_date_end": f"{end_date} 23:59:59",
    }

    if max_time:
        # 增量：从最晚付款时间的日期开始（因为付款时间精确到分钟，按天重新拉取避免遗漏）
        start_str = str(max_time).split(' ')[0].replace('/', '-')
        condition["platform_paid_date_start"] = f"{start_str} 00:00:00"
        mode = "增量"
        print(f"[eccang_sales_sync] 增量同步: {start_str} ~ {end_date}")
    else:
        # 全量：不限开始时间
        mode = "全量"
        print(f"[eccang_sales_sync] 全量同步: ~ {end_date}")

    conn.close()

    # 分页拉取
    all_orders = []
    page = 1
    total = None

    while True:
        retries = 5
        biz = None

        for attempt in range(retries):
            try:
                print(f"[eccang_sales_sync] 请求第{page}页...", end=" ", flush=True)
                biz = get_orders(page=page, page_size=100, condition=condition)
                data = biz.get("data", [])

                if total is None:
                    total = int(biz.get("total", 0))
                    print(f"total={total}")
                else:
                    print(f"获取{len(data)}条 (累计{len(all_orders)+len(data)}/{total})")

                if not data:
                    # 拉取完成
                    all_orders.extend(data)
                    break

                all_orders.extend(data)
                break

            except Exception as e:
                if attempt < retries - 1:
                    wait = 5 * (attempt + 1)
                    print(f"\n[WARN] 失败(尝试{attempt+1}/{retries}): {e}, 等待{wait}秒...")
                    time.sleep(wait)
                else:
                    print(f"\n[ERROR] 第{page}页重试{retries}次均失败，已获取{len(all_orders)}条")
                    # 继续处理已获取的数据

        if total and len(all_orders) >= total:
            print(f"[OK] 全部{total}条订单拉取完成!")
            break

        if not data:
            break

        page += 1
        time.sleep(2)

    if not all_orders:
        return {'count': 0, 'time': '', 'mode': mode, 'msg': '无新订单'}

    # 写入数据库
    conn = db_connect()
    c = conn.cursor()

    # 收集所有订单ID
    order_ids = set()
    for order in all_orders:
        oid = order.get("order_id", "")
        if oid:
            order_ids.add(oid)

    # 增量模式：先删除这些订单的旧记录（因为订单明细可能变化）
    if order_ids:
        placeholders = ','.join(['?'] * len(order_ids))
        c.execute(f"DELETE FROM 销量 WHERE 订单ID IN ({placeholders})", list(order_ids))
        print(f"[eccang_sales_sync] 删除旧记录: {len(order_ids)}个订单")

    # 展平并插入
    total_rows = 0
    for order in all_orders:
        rows = flatten_order(order)
        for row in rows:
            wh_sku = row.get("仓库SKU", "").strip()

            try:
                qty = int(row.get("数量", 0) or 0)
            except (ValueError, TypeError):
                qty = 0

            try:
                unit_price = float(row.get("单价", 0) or 0)
            except (ValueError, TypeError):
                unit_price = 0.0

            try:
                total_amount = float(row.get("总金额", 0) or 0)
            except (ValueError, TypeError):
                total_amount = 0.0

            try:
                sale_amount = float(row.get("销售额", 0) or 0)
            except (ValueError, TypeError):
                sale_amount = 0.0

            try:
                ship_fee = float(row.get("运费", 0) or 0)
            except (ValueError, TypeError):
                ship_fee = 0.0

            try:
                fulfillment_type = int(row.get("发货类型", 0) or 0)
            except (ValueError, TypeError):
                fulfillment_type = 0

            c.execute('''
                INSERT INTO 销量 (订单ID, 平台, 销售单号, 仓库代码, 账号, 国家或地区代码,
                                   币种, 付款时间, 仓库SKU, 数量, 单价, 产品名称,
                                   总金额, 销售额, 运费, 订单类型, 发货类型)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                row.get("订单ID", ""),
                row.get("平台", ""),
                row.get("销售单号", ""),
                row.get("仓库代码", ""),
                row.get("账号", ""),
                row.get("国家或地区代码", ""),
                row.get("币种", ""),
                row.get("付款时间", ""),
                wh_sku,
                qty,
                unit_price,
                row.get("产品名称", ""),
                total_amount,
                sale_amount,
                ship_fee,
                row.get("订单类型", ""),
                fulfillment_type,
            ))
            total_rows += 1

    # 更新同步日志
    sync_time = time.strftime('%Y-%m-%d %H:%M:%S')
    c.execute('''INSERT OR REPLACE INTO 同步日志 (table_name, last_sync, record_count)
                 VALUES ('销量', ?, ?)''', (sync_time, total_rows))

    # 创建索引
    c.execute('CREATE INDEX IF NOT EXISTS idx_sales_sku ON 销量(仓库SKU)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sales_time ON 销量(付款时间)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sales_country ON 销量(国家或地区代码)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sales_store ON 销量(账号)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sales_platform ON 销量(平台)')

    conn.commit()

    c.execute('SELECT COUNT(*) FROM 销量')
    total_count = c.fetchone()[0]
    conn.close()

    print(f"[eccang_sales_sync] {mode}同步完成, 新增/更新{total_rows}条, 库存总记录{total_count}条")

    return {
        'count': total_count,
        'time': sync_time,
        'mode': mode,
        'fetched': len(all_orders),
        'rows_inserted': total_rows,
    }
