"""
钉钉AI表格数据同步模块 — 拉取在途货物和总台账数据到SQLite
"""
import subprocess, json, sys, os, sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, 'supply_chain.db')
MCPORTER = r"C:\Users\86176\.workbuddy\binaries\node\versions\20.18.0\mcporter.cmd"
MCPORTER_CWD = PROJECT_DIR  # mcporter在CWD下找config/mcporter.json

# 钉钉AI表格配置
TRANSIT_BASE_ID = "dQPGYqjpJYmR9NM3soNvwvKqJakx1Z5N"  # 供应链-到港计划系统
TRANSIT_TABLE_ID = "IKyUzSM"   # 在途货物明细表
SOURCE_TABLE_ID = "hERWDMS"    # 简易到港表

FACTORY_BASE_ID = "14lgGw3P8vLw5gbpsRxKp15zV5daZ90D"  # 库存计划管理
FACTORY_TABLE_ID = "exvf4U0"   # 总台账表

# 市场映射
MARKET_MAP = {
    "德国市场": "DE", "美国市场": "US", "英国市场": "UK", "加拿大市场": "CA",
}

# 总台账字段映射
FACTORY_FIELDS = [
    ("ko0CeNr", "SKU"),
    ("fBlzwzt", "品名"),
    ("Jqmiagv", "在产数量"),
    ("dt4QIQw", "国内在库"),
    ("C4DIdnF", "待生产数量"),
    ("kC8snJ4", "库龄天数"),
    ("X1EcJOq", "工厂简写"),
    ("yJsG0I6", "产品线"),
    ("Kklzedh", "订单号"),
    ("cgXaqj4", "采购单价"),
    ("splpA9x", "订单状态"),
    ("fXHLoxV", "目的地"),
    ("4OZ57yX", "下单日期"),
    ("bMoJiWD", "未交货数"),
    ("Q305CyS", "订单数量"),
    ("woOn5zH", "出货总数"),
    ("QbcdNUR", "合同交期"),
    ("3y1TAyz", "实际交期"),
    ("2pWvV9A", "位置区域"),
    ("ChEnrLN", "SPU"),
]

# 仓库分类规则
def classify_warehouse(warehouse_name, market_code):
    """根据仓库名称和目的市场归类"""
    if not warehouse_name:
        defaults = {"US": "FBM美西", "UK": "FBM英国", "DE": "FBM德国"}
        return defaults.get(market_code, "FBM美西")

    if market_code == "US":
        for kws, label in [
            (["美西", "US-CA", "US-AZ", "US-OR", "US-WA"], "FBM美西"),
            (["美东", "US-NJ", "US-GA", "US-NY"], "FBM美东"),
            (["美南", "US-TX"], "FBM美南"),
            (["CG", "wayfair", "Wayfair"], "CG美国"),
            (["FBA"], "FBA美国"),
        ]:
            if any(kw in warehouse_name for kw in kws):
                return label
        return "FBM美西"
    elif market_code == "UK":
        if any(kw in warehouse_name for kw in ["CG", "wayfair", "Wayfair"]):
            return "CG英国"
        return "FBM英国"
    elif market_code == "DE":
        if any(kw in warehouse_name for kw in ["CG", "wayfair", "Wayfair"]):
            return "CG德国"
        return "FBM德国"
    return "FBM美西"


# ========== mcporter 调用 ==========

def mcporter_call(tool, **kwargs):
    """调用 mcporter CLI"""
    cmd_parts = [MCPORTER, "call", f"dingtalk-ai-table.{tool}"]
    for k, v in kwargs.items():
        cmd_parts.append(f"{k}={v}")
    cmd = " ".join(f'"{p}"' if " " in p else p for p in cmd_parts)
    try:
        r = subprocess.run(cmd, capture_output=True, shell=True, cwd=MCPORTER_CWD, timeout=180)
        out = r.stdout.decode('utf-8', errors='replace')
        start = out.find('{')
        if start == -1:
            return {"status": "error", "message": "No JSON in output", "raw": out[:500]}
        return json.loads(out[start:])
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "mcporter timeout"}
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"JSON parse error: {e}"}


def paginate_query(base_id, table_id, limit=100):
    """分页查询所有记录"""
    all_records = []
    cursor = None
    page = 0
    while True:
        page += 1
        params = {"baseId": base_id, "tableId": table_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        result = mcporter_call("query_records", **params)
        if result.get("status") != "success":
            break
        records = result.get("data", {}).get("records", [])
        all_records.extend(records)
        cursor = result.get("data", {}).get("nextCursor")
        if not cursor or not records:
            break
    return all_records


# ========== 数据提取 ==========

def get_cell_text(cells, field_id):
    v = cells.get(field_id)
    if v is None:
        return ""
    if isinstance(v, dict):
        return v.get("name", str(v))
    if isinstance(v, list):
        parts = []
        for item in v:
            if isinstance(item, dict):
                parts.append(item.get("name", str(item)))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return str(v).strip()


def get_cell_number(cells, field_id):
    v = cells.get(field_id)
    if v is None:
        return 0
    try:
        return int(float(str(v)))
    except (ValueError, TypeError):
        return 0


# ========== 在途数据同步 ==========

def sync_in_transit():
    """从钉钉拉取在途货物数据 → 写入 在途货物 表"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 1. 查询在途货物明细表
    transit_records = paginate_query(TRANSIT_BASE_ID, TRANSIT_TABLE_ID)

    # 2. 查询简易到港表（获取仓库、ETA、送仓时间）
    source_records = paginate_query(TRANSIT_BASE_ID, SOURCE_TABLE_ID)

    # 3. 构建映射
    wh_map = {}       # 货柜号 → 仓库名称
    eta_map = {}      # 货柜号 → 到港ETA
    send_map = {}     # 货柜号 → 送仓时间
    for r in source_records:
        cells = r.get("cells", {})
        container = get_cell_text(cells, "ZM649K3")
        if container:
            wh_map[container] = get_cell_text(cells, "p9PnYnN")
            eta_val = cells.get("WfrP1xs", "")
            if eta_val:
                eta_map[container] = eta_val if isinstance(eta_val, str) else eta_val.get("name", "")
            send_val = cells.get("vWMmmzG", "")
            if send_val:
                send_map[container] = send_val if isinstance(send_val, str) else send_val.get("name", "")

    # 4. 处理在途记录
    rows = []
    for r in transit_records:
        cells = r.get("cells", {})
        container = get_cell_text(cells, "JGqIje9")
        market_raw = get_cell_text(cells, "AQ282Bb")
        market_code = MARKET_MAP.get(market_raw, "")
        sku = get_cell_text(cells, "3HEklp3")
        qty = get_cell_number(cells, "WOynFQ6")
        order_no = get_cell_text(cells, "XbXDbOY")
        division = get_cell_text(cells, "d3E3SY4")

        warehouse_name = wh_map.get(container, "")
        eta = eta_map.get(container, "")
        send_time = send_map.get(container, "")
        warehouse_cat = classify_warehouse(warehouse_name, market_code)

        if qty <= 0:
            continue

        rows.append((
            market_code, market_raw, warehouse_cat, warehouse_name,
            sku, qty, order_no, division, eta, send_time, now
        ))

    # 5. 写入数据库
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('DELETE FROM 在途货物')
    c.executemany('''
        INSERT INTO 在途货物 (市场代码, 市场原文, 仓库分类, 仓库名称,
                                SKU, 数量, 订单号, 事业部, 到港ETA, 送仓时间, 同步时间)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', rows)
    conn.commit()

    # 更新同步记录
    c.execute('''INSERT OR REPLACE INTO 同步日志 (table_name, last_sync, record_count)
                 VALUES (?, ?, ?)''', ('在途货物', now, len(rows)))
    conn.commit()
    conn.close()

    return {"table": "在途货物", "count": len(rows), "time": now}


# ========== 工厂库存同步 ==========

def sync_factory_stock():
    """从钉钉拉取总台账数据 → 写入 工厂库存 表"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 1. 查询总台账表
    records = paginate_query(FACTORY_BASE_ID, FACTORY_TABLE_ID)

    # 2. 处理记录
    rows = []
    field_ids = [fid for fid, _ in FACTORY_FIELDS]
    for r in records:
        cells = r.get("cells", {})
        values = [get_cell_text(cells, fid) for fid in field_ids]
        # 数值字段转数字
        sku = values[0]
        if not sku:
            continue
        # 在产数量、国内在库等转整数
        for idx in [2, 3, 4, 5]:  # 在产数量, 国内在库, 待生产数量, 库龄天数
            try:
                values[idx] = int(float(values[idx])) if values[idx] else 0
            except (ValueError, TypeError):
                values[idx] = 0
        for idx in [13, 14, 15]:  # 未交货数, 订单数量, 出货总数
            try:
                values[idx] = int(float(values[idx])) if values[idx] else 0
            except (ValueError, TypeError):
                values[idx] = 0
        # 采购单价转浮点
        try:
            values[9] = float(values[9]) if values[9] else 0.0
        except (ValueError, TypeError):
            values[9] = 0.0

        rows.append(tuple(values) + (now,))

    # 3. 写入数据库
    col_names = [label for _, label in FACTORY_FIELDS] + ["同步时间"]
    placeholders = ','.join(['?'] * len(col_names))

    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('DELETE FROM 工厂库存')
    c.executemany(f'''
        INSERT INTO 工厂库存 ({','.join(col_names)})
        VALUES ({placeholders})
    ''', rows)
    conn.commit()

    # 更新同步记录
    c.execute('''INSERT OR REPLACE INTO 同步日志 (table_name, last_sync, record_count)
                 VALUES (?, ?, ?)''', ('工厂库存', now, len(rows)))
    conn.commit()
    conn.close()

    return {"table": "工厂库存", "count": len(rows), "time": now}


def get_sync_status():
    """获取各表同步状态"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute('SELECT table_name, last_sync, record_count FROM 同步日志')
    result = {}
    for row in c.fetchall():
        result[row[0]] = {"last_sync": row[1], "record_count": row[2]}
    conn.close()
    return result


if __name__ == "__main__":
    print("同步在途数据...")
    r1 = sync_in_transit()
    print(f"  在途: {r1['count']} 条, 时间: {r1['time']}")

    print("同步工厂库存...")
    r2 = sync_factory_stock()
    print(f"  工厂库存: {r2['count']} 条, 时间: {r2['time']}")

    print("完成！")
