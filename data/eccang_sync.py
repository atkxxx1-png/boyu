"""
易仓ERP API - 海外海外仓库存增量同步
从易仓OpenAPI拉取海外仓库存数据，增量更新到supply_chain.db
表结构：SKU, 仓库名称, 可用数量
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
    """生成AES/CBC/PKCS7Padding签名"""
    filtered = {k: v for k, v in params.items() if k != "sign" and v != "" and v is not None}
    sorted_keys = sorted(filtered.keys())
    sign_str = "&".join(f"{k}={filtered[k]}" for k in sorted_keys)

    key = secret_key.encode('utf-8')
    iv = AES_IV.encode('utf-8')
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(sign_str.encode('utf-8'), 16)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode('utf-8')


def get_inventory(page=1, page_size=100, is_out_stock=2, update_time_from=None):
    """查询海外仓库存"""
    biz_data = {"page": page, "page_size": page_size, "is_out_stock": is_out_stock}
    if update_time_from:
        biz_data["update_time_from"] = update_time_from

    biz_content = json.dumps(biz_data, ensure_ascii=False)

    params = {
        "app_key": APP_KEY,
        "service_id": SERVICE_ID,
        "interface_method": "getProductInventoryNew",
        "biz_content": biz_content,
        "sign_type": "AES",
        "charset": "UTF-8",
        "timestamp": str(int(time.time() * 1000)),
        "nonce_str": "req" + str(int(time.time() * 1000)),
        "version": "v1.0.0"
    }

    params["sign"] = generate_sign(params, SECRET_KEY)

    print(f"[eccang_sync] 请求第{page}页...", flush=True)
    response = requests.post(API_URL, json=params, timeout=30)
    return response.json()


def fetch_incremental(update_time_from=None):
    """分页拉取海外仓库存，返回(有库存列表, 零库存列表, 过滤数量)"""
    all_data = []
    zero_qty_data = []    # 增量模式下用于清理已卖光的旧记录
    page = 1
    page_size = 100
    total = None
    filtered_count = 0

    mode_desc = f"增量(从{update_time_from}起)" if update_time_from else "全量"
    print(f"[eccang_sync] 拉取模式: {mode_desc}")

    while True:
        retries = 5
        result = None

        for attempt in range(retries):
            try:
                result = get_inventory(page=page, page_size=page_size,
                                       is_out_stock=2, update_time_from=update_time_from)

                if result.get("code") != "200":
                    raise Exception(f"API错误: {result.get('message')}")

                biz_data = json.loads(result.get("biz_content", "{}"))
                items = biz_data.get("data", [])

                if total is None:
                    total = int(biz_data.get("total", 0))

                # 分离有库存和零库存
                before = len(items)
                items_with_qty = []
                items_zero = []
                for item in items:
                    if int(item.get("pi_in_used_qty", 0) or 0) > 0:
                        items_with_qty.append(item)
                    else:
                        items_zero.append(item)
                filtered_count += len(items_zero)

                print(f"[eccang_sync] 第{page}页: 获取{before}条, 有库存{len(items_with_qty)}条, 零库存{len(items_zero)}条 (累计{len(all_data)+len(items_with_qty)}/{total or '?'})")

                if not items and not biz_data.get("data", []):
                    return all_data, zero_qty_data, filtered_count

                all_data.extend(items_with_qty)
                zero_qty_data.extend(items_zero)
                break

            except Exception as e:
                if attempt < retries - 1:
                    wait = 5 * (attempt + 1)
                    print(f"[eccang_sync] 失败(尝试{attempt+1}/{retries}): {e}, 等待{wait}秒...")
                    time.sleep(wait)
                else:
                    print(f"[eccang_sync] 第{page}页重试{retries}次均失败")
                    return all_data, zero_qty_data, filtered_count

        if total and page * page_size >= total:
            print(f"[eccang_sync] 全部{total}条数据拉取完成!")
            return all_data, zero_qty_data, filtered_count

        page += 1
        time.sleep(3)


def _warehouse_to_market(name):
    """根据仓库名称自动匹配市场"""
    name_upper = name.upper() if name else ''
    if '美西' in name_upper or '美东' in name_upper or 'US' in name_upper or 'LA' in name_upper or 'NY' in name_upper or 'CG' in name_upper or 'NJ' in name_upper or 'CA' in name_upper:
        return 'US'
    if '英国' in name_upper or 'UK' in name_upper or 'GB' in name_upper or '英' in name_upper:
        return 'UK'
    if '德国' in name_upper or 'DE' in name_upper or 'EU' in name_upper or '法国' in name_upper or 'FR' in name_upper or '意大利' in name_upper or 'IT' in name_upper or '西班牙' in name_upper or 'ES' in name_upper:
        return 'DE'
    return 'US'


def sync_inventory(update_time_from=None):
    """同步海外仓库存数据到数据库，增量更新模式

    逻辑：
    - update_time_from=None → 全量拉取，清空表后全量写入
    - update_time_from有值 → 增量拉取，SKU+仓库名称匹配则替换海外仓库存，否则新增
    """
    data, zero_data, filtered_count = fetch_incremental(update_time_from)

    if not data:
        return {'count': 0, 'time': '', 'mode': '增量' if update_time_from else '全量', 'msg': '无数据'}

    conn = db_connect()
    c = conn.cursor()

    # 确保表存在
    c.execute('''CREATE TABLE IF NOT EXISTS 海外仓库存 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        SKU TEXT,
        仓库名称 TEXT,
        可用数量 INTEGER DEFAULT 0,
        市场 TEXT DEFAULT ''
    )''')
    # 兼容旧表迁移：如果缺少市场字段则自动追加
    try:
        c.execute('ALTER TABLE 海外仓库存 ADD COLUMN 市场 TEXT DEFAULT \"\"')
    except:
        pass

    if update_time_from:
        # 增量模式：upsert
        updated = 0
        inserted = 0

        for item in data:
            sku = item.get("product_sku", "").strip()
            warehouse = item.get("warehouse_name", "").strip()
            qty = int(item.get("pi_in_used_qty", 0) or 0)
            market = _warehouse_to_market(warehouse)

            if not sku or not warehouse:
                continue

            c.execute('SELECT id FROM 海外仓库存 WHERE SKU = ? AND 仓库名称 = ?', (sku, warehouse))
            row = c.fetchone()

            if row:
                c.execute('UPDATE 海外仓库存 SET 可用数量 = ?, 市场 = ? WHERE id = ?', (qty, market, row[0]))
                updated += 1
            else:
                c.execute('INSERT INTO 海外仓库存 (SKU, 仓库名称, 可用数量, 市场) VALUES (?, ?, ?, ?)',
                          (sku, warehouse, qty, market))
                inserted += 1

        print(f"[eccang_sync] 增量更新: 新增{inserted}, 更新{updated}")

        # 增量模式：清理已卖光的记录（API返回零库存 → 从DB中删除）
        deleted = 0
        for item in zero_data:
            sku = item.get("product_sku", "").strip()
            warehouse = item.get("warehouse_name", "").strip()
            if not sku or not warehouse:
                continue
            c.execute('DELETE FROM 海外仓库存 WHERE SKU = ? AND 仓库名称 = ?', (sku, warehouse))
            deleted += c.rowcount
        if deleted > 0:
            print(f"[eccang_sync] 增量清理: 删除{deleted}条已卖光记录")
    else:
        # 全量模式：清空后重写
        c.execute('DELETE FROM 海外仓库存')
        count = 0
        for item in data:
            sku = item.get("product_sku", "").strip()
            warehouse = item.get("warehouse_name", "").strip()
            qty = int(item.get("pi_in_used_qty", 0) or 0)
            market = _warehouse_to_market(warehouse)

            if not sku or not warehouse:
                continue

            c.execute('INSERT INTO 海外仓库存 (SKU, 仓库名称, 可用数量, 市场) VALUES (?, ?, ?, ?)',
                      (sku, warehouse, qty, market))
            count += 1

        print(f"[eccang_sync] 全量写入: {count}条记录")

    # 更新同步日志
    sync_time = time.strftime('%Y-%m-%d %H:%M:%S')
    c.execute('''INSERT OR REPLACE INTO 同步日志 (table_name, last_sync, record_count)
                 VALUES ('海外仓库存', ?, ?)''', (sync_time, len(data)))

    # 创建索引
    c.execute('CREATE INDEX IF NOT EXISTS idx_inv_sku ON 海外仓库存(SKU)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_inv_wh ON 海外仓库存(仓库名称)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_inv_sku_wh ON 海外仓库存(SKU, 仓库名称)')

    conn.commit()

    # 获取总记录数
    c.execute('SELECT COUNT(*) FROM 海外仓库存')
    total_count = c.fetchone()[0]
    conn.close()

    mode = '增量' if update_time_from else '全量'
    print(f"[eccang_sync] {mode}同步完成, 海外仓库存总记录{total_count}条")

    return {
        'count': total_count,
        'time': sync_time,
        'mode': mode,
        'fetched': len(data),
        'filtered_zero': filtered_count,
    }
