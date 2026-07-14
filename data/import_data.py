"""
数据导入脚本 - 将CSV数据导入SQLite数据库
支持：销量表、库存表、SKU对应表、产品基础信息表
"""

import csv
import sqlite3
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, 'supply_chain.db')

# 数据源路径
DATA_SOURCES = {
    '销量表': r'C:\Users\86176\Desktop\库龄表\5月订单明细.xlsx',
    '库存表': r'C:\Users\86176\Desktop\库龄表.xlsx',
}

SKU_MAP_PATH = r'R:\8.数据库\SKU对应表.xlsx'
PRODUCT_INFO_PATH = r'R:\8.数据库\产品基础信息表.xlsx'


def create_tables(conn):
    """创建数据库表"""
    c = conn.cursor()

    # 销量表（来自易仓API订单数据）
    c.execute('''
        CREATE TABLE IF NOT EXISTS 销量 (
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
        )
    ''')

    # 库存表（易仓API数据）
    c.execute('''
        CREATE TABLE IF NOT EXISTS 海外仓库存 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            SKU TEXT,
            仓库名称 TEXT,
            可用数量 INTEGER DEFAULT 0
        )
    ''')

    # SKU对应表
    c.execute('''
        CREATE TABLE IF NOT EXISTS SKU映射 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            地区 TEXT,
            原始SKU TEXT,
            匹配后SKU TEXT
        )
    ''')

    # 产品基础信息表（事业部）
    c.execute('''
        CREATE TABLE IF NOT EXISTS 产品信息 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            地区 TEXT,
            SKU TEXT,
            重量 REAL,
            长度 REAL,
            宽度 REAL,
            高度 REAL,
            所属事业部 TEXT,
            采购价 REAL DEFAULT 0
        )
    ''')

    # 在途货物表
    c.execute('''
        CREATE TABLE IF NOT EXISTS 在途货物 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            市场代码 TEXT,
            市场原文 TEXT,
            仓库分类 TEXT,
            仓库名称 TEXT,
            SKU TEXT,
            数量 INTEGER,
            订单号 TEXT,
            事业部 TEXT,
            到港ETA TEXT,
            送仓时间 TEXT,
            同步时间 TEXT
        )
    ''')

    # 工厂库存（总台账）表
    c.execute('''
        CREATE TABLE IF NOT EXISTS 工厂库存 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            SKU TEXT,
            品名 TEXT,
            在产数量 INTEGER DEFAULT 0,
            国内在库 INTEGER DEFAULT 0,
            待生产数量 INTEGER DEFAULT 0,
            库龄天数 INTEGER DEFAULT 0,
            工厂简写 TEXT,
            产品线 TEXT,
            订单号 TEXT,
            采购单价 REAL DEFAULT 0,
            订单状态 TEXT,
            目的地 TEXT,
            未交货数 INTEGER DEFAULT 0,
            订单数量 INTEGER DEFAULT 0,
            出货总数 REAL DEFAULT 0,
            合同交期 TEXT,
            实际交期 TEXT,
            位置区域 TEXT,
            SPU TEXT,
            同步时间 TEXT
        )
    ''')

    # 同步日志表
    c.execute('''
        CREATE TABLE IF NOT EXISTS 同步日志 (
            table_name TEXT PRIMARY KEY,
            last_sync TEXT,
            record_count INTEGER DEFAULT 0
        )
    ''')

    conn.commit()


def normalize_datetime(val):
    """标准化日期时间格式：将 2026/5/5 9:59 格式统一为 2026/05/05 09:59
    确保字符串比较时排序正确"""
    val = val.strip()
    if not val:
        return val
    # 尝试解析常见格式
    for fmt in ('%Y/%m/%d %H:%M', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d',
                '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(val, fmt)
            if ' ' in val:
                return dt.strftime('%Y/%m/%d %H:%M')
            else:
                return dt.strftime('%Y/%m/%d')
        except ValueError:
            continue
    # 无法解析则原样返回
    return val


def import_sales(conn, xlsx_path):
    """导入销量表（从易仓API订单数据xlsx）"""
    print(f"  读取销量表: {xlsx_path}")
    c = conn.cursor()
    c.execute('DELETE FROM 销量')

    try:
        import pandas as pd
    except ImportError:
        print("  需要 pandas + openpyxl 来读取xlsx，跳过销量表")
        return 0

    df = pd.read_excel(xlsx_path, dtype=str, engine='openpyxl')
    count = 0
    for _, row in df.iterrows():
        # 仓库SKU处理：带"-"取第一个"-"之前
        wh_sku = str(row.get('仓库SKU', '')).strip()
        if not wh_sku:
            continue
        # 排除W开头
        if wh_sku.upper().startswith('W'):
            continue

        try:
            qty = int(float(row.get('SKU数量', 0) or 0))
        except (ValueError, TypeError):
            qty = 0

        try:
            unit_price = float(row.get('单价', 0) or 0)
        except (ValueError, TypeError):
            unit_price = 0.0

        try:
            total_amount = float(row.get('总金额', 0) or 0)
        except (ValueError, TypeError):
            total_amount = 0.0

        try:
            sale_amount = float(row.get('销售额', 0) or 0)
        except (ValueError, TypeError):
            sale_amount = 0.0

        try:
            ship_fee = float(row.get('运费', 0) or 0)
        except (ValueError, TypeError):
            ship_fee = 0.0

        try:
            fulfillment_type = int(float(row.get('发货类型', 0) or 0))
        except (ValueError, TypeError):
            fulfillment_type = 0

        # 标准化付款时间
        payment_time = normalize_datetime(str(row.get('付款时间', '')).strip())

        c.execute('''
            INSERT INTO 销量 (订单ID, 平台, 销售单号, 仓库代码, 账号, 国家或地区代码,
                               币种, 付款时间, 仓库SKU, 数量, 单价, 产品名称,
                               总金额, 销售额, 运费, 订单类型, 发货类型)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            str(row.get('订单ID', '')).strip(),
            str(row.get('平台', '')).strip(),
            str(row.get('销售单号', '')).strip(),
            str(row.get('仓库代码', '')).strip(),
            str(row.get('账号', '')).strip(),
            str(row.get('国家', '')).strip(),
            str(row.get('币种', '')).strip(),
            payment_time,
            wh_sku,
            qty,
            unit_price,
            str(row.get('产品名称', ''))[:200].strip(),
            total_amount,
            sale_amount,
            ship_fee,
            str(row.get('订单类型', '')).strip(),
            fulfillment_type,
        ))
        count += 1

    conn.commit()
    print(f"  导入销量表: {count} 行")
    return count


def import_inventory(conn, xlsx_path):
    """导入库存表（从易仓API导出的xlsx，含产品SKU、可用数量、仓库名称）"""
    print(f"  读取库存表: {xlsx_path}")
    c = conn.cursor()
    c.execute('DELETE FROM 海外仓库存')

    try:
        import pandas as pd
    except ImportError:
        print("  需要 pandas + openpyxl 来读取xlsx，跳过库存表")
        return 0

    df = pd.read_excel(xlsx_path, dtype=str)
    count = 0
    for _, row in df.iterrows():
        sku = str(row.get('产品SKU', '')).strip()
        wh = str(row.get('仓库名称', '')).strip()
        try:
            qty = int(float(row.get('可用数量', 0) or 0))
        except (ValueError, TypeError):
            qty = 0

        if sku and wh and qty > 0:
            c.execute('INSERT INTO 海外仓库存 (SKU, 仓库名称, 可用数量) VALUES (?, ?, ?)',
                      (sku, wh, qty))
            count += 1

    conn.commit()
    print(f"  导入库存表: {count} 行")
    return count


def import_sku_mapping(conn, xlsx_path):
    """导入SKU对应表"""
    if not os.path.exists(xlsx_path):
        print(f"  SKU对应表不存在: {xlsx_path}，跳过")
        return 0

    try:
        import pandas as pd
    except ImportError:
        print("  需要 pandas + openpyxl 来读取xlsx，跳过SKU对应表")
        return 0

    print(f"  读取SKU对应表: {xlsx_path}")
    c = conn.cursor()
    c.execute('DELETE FROM SKU映射')

    df = pd.read_excel(xlsx_path, dtype=str, header=None)
    count = 0
    for _, row in df.iterrows():
        region = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ''
        sku = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ''
        mapped = str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else ''
        if region and sku and mapped:
            c.execute('INSERT INTO SKU映射 (地区, 原始SKU, 匹配后SKU) VALUES (?, ?, ?)',
                       (region.upper(), sku, mapped))
            count += 1

    conn.commit()
    print(f"  导入SKU对应表: {count} 行")
    return count


def import_product_info(conn, xlsx_path):
    """导入产品基础信息表（含事业部）"""
    if not os.path.exists(xlsx_path):
        print(f"  产品基础信息表不存在: {xlsx_path}，跳过")
        return 0

    try:
        import pandas as pd
    except ImportError:
        print("  需要 pandas + openpyxl 来读取xlsx，跳过产品基础信息表")
        return 0

    print(f"  读取产品基础信息表: {xlsx_path}")
    c = conn.cursor()
    c.execute('DELETE FROM 产品信息')

    total = 0
    for sheet_name in ('US', 'DE', 'UK'):
        try:
            df = pd.read_excel(xlsx_path, sheet_name=sheet_name, dtype=str)
        except Exception as e:
            print(f"    Sheet {sheet_name} 读取失败: {e}")
            continue

        count = 0
        for _, row in df.iterrows():
            sku = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ''
            weight = 0.0
            length = 0.0
            width = 0.0
            height = 0.0
            try:
                weight = float(row.iloc[1]) if pd.notna(row.iloc[1]) else 0.0
            except (ValueError, TypeError):
                pass
            try:
                length = float(row.iloc[2]) if pd.notna(row.iloc[2]) else 0.0
            except (ValueError, TypeError):
                pass
            try:
                width = float(row.iloc[3]) if pd.notna(row.iloc[3]) else 0.0
            except (ValueError, TypeError):
                pass
            try:
                height = float(row.iloc[4]) if pd.notna(row.iloc[4]) else 0.0
            except (ValueError, TypeError):
                pass
            division = str(row.iloc[5]).strip() if pd.notna(row.iloc[5]) and str(row.iloc[5]).strip() != 'nan' else ''

            if sku:
                c.execute('''
                    INSERT INTO 产品信息 (地区, SKU, 重量, 长度, 宽度, 高度, 所属事业部, 采购价)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (sheet_name, sku, weight, length, width, height, division, 0.0))
                count += 1

        print(f"    Sheet {sheet_name}: {count} 行")
        total += count

    conn.commit()
    print(f"  导入产品基础信息表: {total} 行")
    return total


def main():
    print("=" * 50)
    print("  供应链数据导入工具")
    print("=" * 50)

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)

    # 导入销量表
    sales_path = DATA_SOURCES['销量表']
    if os.path.exists(sales_path):
        import_sales(conn, sales_path)
    else:
        print(f"  销量表不存在: {sales_path}")
        # 旧表如果存在则DROP重建
        c = conn.cursor()
        c.execute('DROP TABLE IF EXISTS 销量')
        conn.commit()
        create_tables(conn)

    # 导入库存表
    inv_path = DATA_SOURCES['库存表']
    if os.path.exists(inv_path):
        import_inventory(conn, inv_path)
    else:
        print(f"  库存表不存在: {inv_path}")
    # 导入SKU对应表
    import_sku_mapping(conn, SKU_MAP_PATH)

    # 导入产品基础信息表
    import_product_info(conn, PRODUCT_INFO_PATH)

    # 创建索引
    c = conn.cursor()
    c.execute('CREATE INDEX IF NOT EXISTS idx_sales_sku ON 销量(仓库SKU)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sales_time ON 销量(付款时间)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sales_country ON 销量(国家或地区代码)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sales_store ON 销量(账号)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sales_platform ON 销量(平台)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_inv_sku ON 海外仓库存(SKU)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sku_map ON SKU映射(地区, 原始SKU)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_prod_info ON 产品信息(地区, SKU)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_transit_sku ON 在途货物(SKU)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_transit_market ON 在途货物(市场代码)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_transit_wh ON 在途货物(仓库分类)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_transit_div ON 在途货物(事业部)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_factory_sku ON 工厂库存(SKU)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_factory_div ON 工厂库存(产品线)')
    conn.commit()

    # 统计
    print("\n--- 数据库统计 ---")
    for table in ['销量', '海外仓库存', 'SKU映射', '产品信息', '在途货物', '工厂库存', '同步日志']:
        c.execute(f'SELECT COUNT(*) FROM {table}')
        print(f"  {table}: {c.fetchone()[0]} 行")

    conn.close()
    print(f"\n数据库已保存: {DB_PATH}")
    print("完成！")


if __name__ == '__main__':
    main()
