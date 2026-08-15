# -*- coding: utf-8 -*-
"""
====================================================================
 专业电商购物机器人（中文版）
====================================================================

版权所有 (c) 2026 培哥
频道: https://t.me/pgkj666      联系机器人: https://t.me/pgkj666_bot

功能：
  - 商品目录 + 多规格（颜色/尺码/型号）
  - 购物车 + 优惠券
  - 下单 + 卡转账付款（上传回执）
  - 邀请返佣系统
  - 客服工单（可回复）
  - 查看历史订单
  - 完整管理后台（商品、订单、支付、工单、用户、统计）

依赖：
    pip install "python-telegram-bot>=22.7" --upgrade

配置（见文件下方）：
    BOT_TOKEN   -> 从 @BotFather 获取
    ADMIN_IDS   -> 管理员数字ID列表（用 @userinfobot 获取）

运行：
    python shop_bot.py
====================================================================
"""

import logging
import sqlite3
import os
import random
import string
from datetime import datetime
from contextlib import closing

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ====================================================================
#  主配置 – 请务必修改
# ====================================================================

BOT_TOKEN = "BOT_TOKEN"          # 机器人 Token
ADMIN_IDS = [0]                  # 管理员数字ID列表
CURRENCY = "元"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop_advanced.db")
REFERRAL_BONUS_PERCENT = 5       # 邀请返佣百分比（5%）
PAYMENT_CARD_NUMBER = "6037-9975-1234-5678"  # 收款卡号（请修改）

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

#__DB_SCHEMA__
# ====================================================================
#  数据库 (SQLite)
# ====================================================================

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with closing(get_conn()) as conn, conn:
        conn.executescript("""
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            );

            -- 商品规格（颜色、尺码、型号）
            CREATE TABLE IF NOT EXISTS product_variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                variant_name TEXT NOT NULL,
                price INTEGER NOT NULL,
                stock INTEGER NOT NULL DEFAULT 0,
                photo_file_id TEXT DEFAULT NULL,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS cart_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                variant_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (variant_id) REFERENCES product_variants(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                phone TEXT,
                address TEXT,
                total_price INTEGER NOT NULL,
                discount_amount INTEGER DEFAULT 0,
                final_price INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                payment_status TEXT DEFAULT 'unpaid',
                created_at TEXT NOT NULL,
                paid_at TEXT,
                tracking_code TEXT,
                coupon_code TEXT,
                referrer_id INTEGER,
                referral_awarded INTEGER DEFAULT 0,
                stock_restored INTEGER DEFAULT 0,
                FOREIGN KEY (referrer_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                variant_id INTEGER,
                variant_name TEXT NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                referral_code TEXT UNIQUE,
                referrer_id INTEGER,
                wallet_balance INTEGER DEFAULT 0,
                created_at TEXT,
                FOREIGN KEY (referrer_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                description TEXT,
                created_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS coupons (
                code TEXT PRIMARY KEY,
                discount_type TEXT NOT NULL,
                discount_value INTEGER NOT NULL,
                min_order_amount INTEGER DEFAULT 0,
                expires_at TEXT,
                usage_limit INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                subject TEXT,
                message TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT,
                updated_at TEXT,
                admin_response TEXT,
                responded_at TEXT
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                card_number TEXT,
                receipt_photo_id TEXT,
                amount INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                verified_by INTEGER,
                created_at TEXT,
                verified_at TEXT,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id);
            CREATE INDEX IF NOT EXISTS idx_tickets_user_id ON support_tickets(user_id);
        """)
        # 轻量迁移：老库补 referral_awarded 列（防止返佣重复发放）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "referral_awarded" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN referral_awarded INTEGER DEFAULT 0")
        if "stock_restored" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN stock_restored INTEGER DEFAULT 0")
        # 轻量迁移：order_items 补 variant_id 列（取消订单时回滚库存需要）
        oi_cols = {r["name"] for r in conn.execute("PRAGMA table_info(order_items)").fetchall()}
        if "variant_id" not in oi_cols:
            conn.execute("ALTER TABLE order_items ADD COLUMN variant_id INTEGER")

# ====================================================================
#  数据库操作函数
# ====================================================================

# ---- 用户与邀请 ----
def get_or_create_user(user_id, username, first_name, last_name="", referrer_code=None):
    with closing(get_conn()) as conn, conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            referral_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            referrer_id = None
            if referrer_code:
                ref_user = conn.execute("SELECT id FROM users WHERE referral_code=?", (referrer_code,)).fetchone()
                if ref_user and ref_user["id"] != user_id:
                    referrer_id = ref_user["id"]
            conn.execute(
                """INSERT INTO users (id, username, first_name, last_name, referral_code, referrer_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, username, first_name, last_name, referral_code, referrer_id, datetime.now().isoformat())
            )
            user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return user

def get_user_by_id(user_id):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

def get_user_by_referral_code(code):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM users WHERE referral_code=?", (code,)).fetchone()

def add_wallet_transaction(user_id, amount, description):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO wallet_transactions (user_id, amount, description, created_at) VALUES (?, ?, ?, ?)",
            (user_id, amount, description, datetime.now().isoformat())
        )
        conn.execute("UPDATE users SET wallet_balance = wallet_balance + ? WHERE id=?", (amount, user_id))

# ---- 商品与规格 ----
def db_add_category(name):
    with closing(get_conn()) as conn, conn:
        cur = conn.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        return cur.lastrowid

def db_get_categories():
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM categories ORDER BY name").fetchall()

def db_add_product(category_id, name, description=""):
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO products (category_id, name, description) VALUES (?, ?, ?)",
            (category_id, name, description)
        )
        return cur.lastrowid

def db_add_variant(product_id, variant_name, price, stock, photo_file_id=None):
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            """INSERT INTO product_variants (product_id, variant_name, price, stock, photo_file_id)
               VALUES (?, ?, ?, ?, ?)""",
            (product_id, variant_name, price, stock, photo_file_id)
        )
        return cur.lastrowid

def db_get_variants_by_product(product_id):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM product_variants WHERE product_id=? ORDER BY variant_name",
            (product_id,)
        ).fetchall()

def db_get_variant(variant_id):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM product_variants WHERE id=?", (variant_id,)).fetchone()

def db_deactivate_product(product_id):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE products SET is_active=0 WHERE id=?", (product_id,))

def db_get_all_products_with_possible_variants():
    """返回所有上架商品及其规格（若无规格则规格字段为 NULL）。"""
    with closing(get_conn()) as conn:
        return conn.execute("""
            SELECT p.id AS product_id, p.name AS product_name, p.description,
                   c.name AS category_name,
                   v.id AS variant_id, v.variant_name, v.price, v.stock, v.photo_file_id
            FROM products p
            JOIN categories c ON p.category_id = c.id
            LEFT JOIN product_variants v ON p.id = v.product_id
            WHERE p.is_active = 1
            ORDER BY c.name, p.name, v.variant_name
        """).fetchall()

# ---- 购物车 ----
def db_add_to_cart(user_id, variant_id, quantity=1):
    with closing(get_conn()) as conn, conn:
        existing = conn.execute(
            "SELECT * FROM cart_items WHERE user_id=? AND variant_id=?",
            (user_id, variant_id)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE cart_items SET quantity = quantity + ? WHERE id=?",
                (quantity, existing["id"])
            )
        else:
            conn.execute(
                "INSERT INTO cart_items (user_id, variant_id, quantity) VALUES (?, ?, ?)",
                (user_id, variant_id, quantity)
            )

def db_get_cart(user_id):
    with closing(get_conn()) as conn:
        return conn.execute("""
            SELECT ci.id AS cart_id, ci.quantity,
                   v.id AS variant_id, v.variant_name, v.price, v.stock,
                   p.id AS product_id, p.name AS product_name
            FROM cart_items ci
            JOIN product_variants v ON ci.variant_id = v.id
            JOIN products p ON v.product_id = p.id
            WHERE ci.user_id = ?
        """, (user_id,)).fetchall()

def db_clear_cart(user_id):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))

def db_remove_cart_item(cart_id):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM cart_items WHERE id=?", (cart_id,))

# ---- 订单（下单在单事务内校验库存+扣减+写单，避免超卖）----
def db_create_order(user_id, username, full_name, phone, address, coupon_code=None, referrer_id=None):
    """
    在单个事务中：重新读取购物车、校验库存、计算优惠券、扣库存、写订单、清车。
    返回 (order_id, final_price) 或 ("error", 错误信息)。
    """
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cart = conn.execute("""
            SELECT ci.quantity, v.id AS variant_id, v.variant_name, v.price, v.stock,
                   p.name AS product_name
            FROM cart_items ci
            JOIN product_variants v ON ci.variant_id = v.id
            JOIN products p ON v.product_id = p.id
            WHERE ci.user_id = ?
        """, (user_id,)).fetchall()
        if not cart:
            return "error", "购物车为空"

        for item in cart:
            if item["quantity"] > item["stock"]:
                return "error", f"「{item['product_name']} - {item['variant_name']}」库存不足"

        total = sum(item["price"] * item["quantity"] for item in cart)

        discount = 0
        applied_coupon = None
        if coupon_code:
            c = conn.execute("SELECT * FROM coupons WHERE code=?", (coupon_code,)).fetchone()
            if (c and c["is_active"] and c["used_count"] < c["usage_limit"]
                    and (c["expires_at"] is None or datetime.now() < datetime.fromisoformat(c["expires_at"]))
                    and total >= c["min_order_amount"]):
                if c["discount_type"] == "percent":
                    discount = int(total * c["discount_value"] / 100)
                else:
                    discount = min(c["discount_value"], total)
                conn.execute("UPDATE coupons SET used_count = used_count + 1 WHERE code=?", (coupon_code,))
                applied_coupon = coupon_code

        final_price = max(0, total - discount)

        cur = conn.execute(
            """INSERT INTO orders
               (user_id, username, full_name, phone, address, total_price, discount_amount, final_price,
                status, payment_status, created_at, coupon_code, referrer_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'unpaid', ?, ?, ?)""",
            (user_id, username, full_name, phone, address, total, discount, final_price,
             datetime.now().isoformat(), applied_coupon, referrer_id)
        )
        order_id = cur.lastrowid
        for item in cart:
            conn.execute(
                """INSERT INTO order_items (order_id, variant_id, variant_name, product_name, quantity, unit_price)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (order_id, item["variant_id"], item["variant_name"], item["product_name"],
                 item["quantity"], item["price"])
            )
            conn.execute("UPDATE product_variants SET stock = stock - ? WHERE id=?",
                         (item["quantity"], item["variant_id"]))
        conn.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
        return order_id, final_price

def db_get_order(order_id):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

def db_get_order_items(order_id):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()

def db_update_order_status(order_id, status):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))

def cancel_order_and_restore(order_id, operator_id=0):
    """
    取消订单并做资金/库存回滚（单事务，幂等）：
      - 回滚库存（把订单内每个规格的数量加回，仅第一次）
      - 若该订单已付款，把 final_price 退回买家钱包并记流水
      - 若已发放过邀请返佣，扣回返佣
      - 订单状态置为 cancelled、支付状态置为 unpaid
    返回 (user_id, refunded_amount) 或错误字符串。
    """
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return "订单不存在"
        if order["status"] == "cancelled":
            return "该订单已取消"

        # 1) 回滚库存（幂等：stock_restored 标记）
        if order["stock_restored"] == 0:
            items = conn.execute("SELECT variant_id, quantity FROM order_items WHERE order_id=?",
                                 (order_id,)).fetchall()
            for it in items:
                if it["variant_id"]:
                    conn.execute("UPDATE product_variants SET stock = stock + ? WHERE id=?",
                                 (it["quantity"], it["variant_id"]))
            conn.execute("UPDATE orders SET stock_restored=1 WHERE id=?", (order_id,))

        # 2) 已付款则退款到钱包
        refunded = 0
        if order["payment_status"] == "paid" and order["final_price"] > 0:
            conn.execute("UPDATE users SET wallet_balance = wallet_balance + ? WHERE id=?",
                         (order["final_price"], order["user_id"]))
            conn.execute(
                "INSERT INTO wallet_transactions (user_id, amount, description, created_at) VALUES (?, ?, ?, ?)",
                (order["user_id"], order["final_price"], f"订单 #{order_id} 取消退款", datetime.now().isoformat())
            )
            refunded = order["final_price"]

        # 3) 已发放返佣则扣回
        if order["referral_awarded"] == 1 and order["referrer_id"]:
            bonus = int(order["final_price"] * REFERRAL_BONUS_PERCENT / 100)
            if bonus > 0:
                conn.execute("UPDATE users SET wallet_balance = wallet_balance - ? WHERE id=?",
                             (bonus, order["referrer_id"]))
                conn.execute(
                    "INSERT INTO wallet_transactions (user_id, amount, description, created_at) VALUES (?, ?, ?, ?)",
                    (order["referrer_id"], -bonus, f"订单 #{order_id} 取消，扣回返佣", datetime.now().isoformat())
                )
            conn.execute("UPDATE orders SET referral_awarded=0 WHERE id=?", (order_id,))

        # 4) 更新状态
        conn.execute("UPDATE orders SET status='cancelled', payment_status='unpaid' WHERE id=?", (order_id,))
        return order["user_id"], refunded

def db_update_order_payment_status(order_id, payment_status):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE orders SET payment_status=? WHERE id=?", (payment_status, order_id))

def db_get_user_orders(user_id, limit=20):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()

def db_get_all_orders(status=None, limit=20):
    with closing(get_conn()) as conn:
        if status:
            return conn.execute(
                "SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        return conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

def db_get_stats():
    with closing(get_conn()) as conn:
        total_orders = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        total_revenue = conn.execute(
            "SELECT COALESCE(SUM(final_price), 0) s FROM orders WHERE status != 'cancelled'"
        ).fetchone()["s"]
        pending_orders = conn.execute("SELECT COUNT(*) c FROM orders WHERE status='pending'").fetchone()["c"]
        paid_orders = conn.execute("SELECT COUNT(*) c FROM orders WHERE payment_status='paid'").fetchone()["c"]
        products_count = conn.execute("SELECT COUNT(*) c FROM products WHERE is_active=1").fetchone()["c"]
        users_count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        return {
            "total_orders": total_orders, "total_revenue": total_revenue,
            "pending_orders": pending_orders, "paid_orders": paid_orders,
            "products_count": products_count, "users_count": users_count,
        }

# ---- 优惠券 ----
def db_add_coupon(code, discount_type, discount_value, min_order_amount=0, expires_at=None, usage_limit=1):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            """INSERT INTO coupons (code, discount_type, discount_value, min_order_amount, expires_at, usage_limit)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (code, discount_type, discount_value, min_order_amount, expires_at, usage_limit)
        )

def db_get_coupon(code):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM coupons WHERE code=?", (code,)).fetchone()

def db_get_all_coupons():
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM coupons ORDER BY code").fetchall()

# ---- 支付 ----
def db_add_payment(order_id, user_id, card_number, receipt_photo_id, amount):
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            """INSERT INTO payments (order_id, user_id, card_number, receipt_photo_id, amount, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (order_id, user_id, card_number, receipt_photo_id, amount, datetime.now().isoformat())
        )
        return cur.lastrowid

def db_get_payment(payment_id):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()

def db_update_payment_status(payment_id, status, verified_by=None):
    with closing(get_conn()) as conn, conn:
        if status == "verified":
            conn.execute(
                "UPDATE payments SET status=?, verified_by=?, verified_at=? WHERE id=?",
                (status, verified_by, datetime.now().isoformat(), payment_id)
            )
        else:
            conn.execute("UPDATE payments SET status=? WHERE id=?", (status, payment_id))

def db_get_pending_payments():
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM payments WHERE status='pending' ORDER BY id DESC").fetchall()

def try_award_referral(order_id):
    """原子领取返佣：只有第一次成功把 referral_awarded 置 1 的调用才发放，杜绝重复返佣。"""
    with closing(get_conn()) as conn, conn:
        changed = conn.execute(
            "UPDATE orders SET referral_awarded=1 WHERE id=? AND referral_awarded=0 AND referrer_id IS NOT NULL",
            (order_id,)
        ).rowcount
        return changed > 0

# ---- 工单 ----
def db_create_ticket(user_id, subject, message):
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            """INSERT INTO support_tickets (user_id, subject, message, status, created_at, updated_at)
               VALUES (?, ?, ?, 'open', ?, ?)""",
            (user_id, subject, message, datetime.now().isoformat(), datetime.now().isoformat())
        )
        return cur.lastrowid

def db_get_tickets(status=None):
    with closing(get_conn()) as conn:
        if status:
            return conn.execute("SELECT * FROM support_tickets WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        return conn.execute("SELECT * FROM support_tickets ORDER BY id DESC").fetchall()

def db_get_ticket(ticket_id):
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()

def db_update_ticket_response(ticket_id, admin_response):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            """UPDATE support_tickets SET admin_response=?, status='in_progress', responded_at=?, updated_at=?
               WHERE id=?""",
            (admin_response, datetime.now().isoformat(), datetime.now().isoformat(), ticket_id)
        )

def db_close_ticket(ticket_id):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE support_tickets SET status='closed', updated_at=? WHERE id=?",
                     (datetime.now().isoformat(), ticket_id))

#__HELPERS__
# ====================================================================
#  通用辅助
# ====================================================================
STATUS_LABELS = {
    "pending": "⏳ 待处理",
    "confirmed": "✅ 已确认",
    "shipped": "📦 已发货",
    "cancelled": "❌ 已取消",
    "paid": "💰 已付款",
}
PAYMENT_STATUS_LABELS = {
    "unpaid": "未付款",
    "awaiting_verify": "等待确认回执",
    "paid": "已付款",
}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def format_price(amount: int) -> str:
    return f"{amount:,} {CURRENCY}"

def main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        ["🛍 浏览商品", "🛒 购物车"],
        ["🧾 我的订单", "📞 客服"],
    ]
    if is_admin(user_id):
        rows.append(["⚙️ 管理面板"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def admin_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        ["➕ 添加分类", "➕ 添加商品", "➕ 添加规格"],
        ["📋 商品列表", "🧾 订单管理"],
        ["💳 支付管理", "🎫 优惠券管理"],
        ["📩 客服工单", "📊 销售统计"],
        ["👥 用户", "🔙 返回主菜单"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

#__STATES__
# ====================================================================
#  会话状态
# ====================================================================
(
    ADD_PRODUCT_CATEGORY, ADD_PRODUCT_NAME, ADD_PRODUCT_DESCRIPTION,
    ADD_VARIANT_PRODUCT, ADD_VARIANT_NAME, ADD_VARIANT_PRICE, ADD_VARIANT_STOCK, ADD_VARIANT_PHOTO,
    CHECKOUT_NAME, CHECKOUT_PHONE, CHECKOUT_ADDRESS, CHECKOUT_COUPON, CHECKOUT_PAYMENT,
    PAYMENT_CARD_NUMBER_STATE, PAYMENT_RECEIPT,
    TICKET_SUBJECT, TICKET_MESSAGE, TICKET_RESPONSE,
    COUPON_CODE, COUPON_TYPE, COUPON_VALUE, COUPON_MIN_ORDER, COUPON_EXPIRY, COUPON_LIMIT,
) = range(24)

#__COMMON__
# ====================================================================
#  通用处理
# ====================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referrer_code = args[0] if args else None
    if referrer_code:
        context.user_data["referrer_code"] = referrer_code
    get_or_create_user(
        user_id=user.id, username=user.username, first_name=user.first_name,
        last_name=user.last_name or "", referrer_code=referrer_code,
    )
    msg = f"你好 {user.first_name} 👋\n欢迎光临本店铺！\n"
    if referrer_code:
        ref_user = get_user_by_referral_code(referrer_code)
        if ref_user and ref_user["id"] != user.id:
            msg += f"你是由 @{ref_user['username'] or ref_user['first_name']} 邀请来的！\n"
    msg += "请使用下方菜单。"
    await update.message.reply_text(msg, reply_markup=main_menu_keyboard(user.id))

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("操作已取消。", reply_markup=main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END

#__CATALOG__
# ====================================================================
#  顾客端：商品目录（含多规格）
# ====================================================================

async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    categories = db_get_categories()
    if not categories:
        await update.message.reply_text("暂时还没有商品分类。")
        return
    keyboard = [[InlineKeyboardButton(cat["name"], callback_data=f"cat:{cat['id']}")] for cat in categories]
    await update.message.reply_text("请选择分类：", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_products_in_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category_id = int(query.data.split(":")[1])
    with closing(get_conn()) as conn:
        products = conn.execute(
            "SELECT * FROM products WHERE category_id=? AND is_active=1", (category_id,)
        ).fetchall()
    if not products:
        await query.edit_message_text("该分类下暂无商品。")
        return
    for product in products:
        text = f"🛍 <b>{product['name']}</b>\n{product['description'] or ''}"
        variants = db_get_variants_by_product(product['id'])
        if variants:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔽 选择规格", callback_data=f"variants:{product['id']}")]
            ])
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("⛔ 无规格（缺货）", callback_data="noop")]
            ])
        photo_id = None
        for v in variants:
            if v['photo_file_id']:
                photo_id = v['photo_file_id']
                break
        if photo_id:
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=photo_id, caption=text,
                                         parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await context.bot.send_message(chat_id=query.message.chat_id, text=text,
                                           parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def show_variants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split(":")[1])
    variants = db_get_variants_by_product(product_id)
    if not variants:
        await query.edit_message_text("该商品没有任何规格。")
        return
    keyboard = []
    for v in variants:
        btn_text = f"{v['variant_name']} - {format_price(v['price'])}（库存: {v['stock']}）"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"add:{v['id']}")])
    await query.edit_message_text("请选择你想要的规格：", reply_markup=InlineKeyboardMarkup(keyboard))

async def add_to_cart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    variant_id = int(query.data.split(":")[1])
    variant = db_get_variant(variant_id)
    if not variant or variant["stock"] <= 0:
        await query.answer("该规格已缺货。", show_alert=True)
        return
    db_add_to_cart(query.from_user.id, variant_id, 1)
    await query.answer("已加入购物车 ✅")

async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

#__CART__
# ====================================================================
#  购物车与优惠券
# ====================================================================
async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    items = db_get_cart(user_id)
    if not items:
        await update.message.reply_text("购物车是空的。")
        return
    text_lines = ["🛒 <b>你的购物车：</b>\n"]
    total = 0
    keyboard_rows = []
    for item in items:
        subtotal = item["price"] * item["quantity"]
        total += subtotal
        text_lines.append(f"• {item['product_name']} - {item['variant_name']} × {item['quantity']} = {format_price(subtotal)}")
        keyboard_rows.append([InlineKeyboardButton(f"🗑 删除 {item['variant_name']}", callback_data=f"rmcart:{item['cart_id']}")])
    text_lines.append(f"\n💰 合计: {format_price(total)}")
    coupon_code = context.user_data.get("coupon_code")
    if coupon_code:
        coupon = db_get_coupon(coupon_code)
        if coupon:
            if coupon["discount_type"] == "percent":
                discount = int(total * coupon["discount_value"] / 100)
            else:
                discount = min(coupon["discount_value"], total)
            text_lines.append(f"🎫 优惠券（{coupon_code}）: -{format_price(discount)}")
            text_lines.append(f"💰 应付金额: {format_price(max(0, total - discount))}")
    keyboard_rows.append([InlineKeyboardButton("🎫 使用优惠券", callback_data="apply_coupon")])
    keyboard_rows.append([InlineKeyboardButton("✅ 提交订单", callback_data="checkout")])
    keyboard_rows.append([InlineKeyboardButton("❌ 清空购物车", callback_data="clearcart")])
    await update.message.reply_text("\n".join(text_lines), parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup(keyboard_rows))

async def remove_cart_item_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cart_id = int(query.data.split(":")[1])
    db_remove_cart_item(cart_id)
    await query.answer("已删除")
    await query.edit_message_text("已删除该商品。重新点击「🛒 购物车」可查看最新购物车。")

async def clear_cart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db_clear_cart(query.from_user.id)
    await query.answer("购物车已清空")
    await query.edit_message_text("你的购物车已清空。")

async def apply_coupon_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("请输入优惠券码：")
    return CHECKOUT_COUPON

async def apply_coupon_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    coupon = db_get_coupon(code)
    if not coupon or not coupon["is_active"] or coupon["used_count"] >= coupon["usage_limit"]:
        await update.message.reply_text("优惠券无效或已用完。",
                                        reply_markup=main_menu_keyboard(update.effective_user.id))
        return ConversationHandler.END
    if coupon["expires_at"] and datetime.now() > datetime.fromisoformat(coupon["expires_at"]):
        await update.message.reply_text("优惠券已过期。",
                                        reply_markup=main_menu_keyboard(update.effective_user.id))
        return ConversationHandler.END
    context.user_data["coupon_code"] = code
    await update.message.reply_text("优惠券已应用。请重新查看购物车。",
                                    reply_markup=main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END

coupon_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(apply_coupon_callback, pattern="^apply_coupon$")],
    states={CHECKOUT_COUPON: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_coupon_text)]},
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
)

#__CHECKOUT__
# ====================================================================
#  下单流程
# ====================================================================
async def checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    items = db_get_cart(query.from_user.id)
    if not items:
        await query.edit_message_text("购物车是空的。")
        return ConversationHandler.END
    await context.bot.send_message(chat_id=query.message.chat_id, text="请输入你的姓名：",
                                   reply_markup=ReplyKeyboardRemove())
    return CHECKOUT_NAME

async def checkout_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["full_name"] = update.message.text.strip()
    await update.message.reply_text("请输入你的联系电话：")
    return CHECKOUT_PHONE

async def checkout_get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["phone"] = update.message.text.strip()
    await update.message.reply_text("请输入你的完整收货地址：")
    return CHECKOUT_ADDRESS

async def checkout_get_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    user = update.effective_user
    user_row = get_user_by_id(user.id)
    referrer_id = user_row["referrer_id"] if user_row else None
    result, second = db_create_order(
        user_id=user.id, username=user.username or "",
        full_name=context.user_data.get("full_name", ""),
        phone=context.user_data.get("phone", ""),
        address=address,
        coupon_code=context.user_data.get("coupon_code"),
        referrer_id=referrer_id,
    )
    if result == "error":
        await update.message.reply_text(f"❌ 下单失败：{second}", reply_markup=main_menu_keyboard(user.id))
        return ConversationHandler.END
    order_id, final_price = result, second
    context.user_data.pop("coupon_code", None)
    await update.message.reply_text(
        f"✅ 订单 #{order_id} 已成功创建。\n"
        f"💰 应付金额: {format_price(final_price)}\n\n"
        "请选择卡转账方式完成付款。",
        reply_markup=main_menu_keyboard(user.id)
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 卡转账付款", callback_data=f"pay:{order_id}")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu")]
    ])
    await update.message.reply_text("请选择下面的一个选项：", reply_markup=keyboard)
    return ConversationHandler.END

checkout_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(checkout_start, pattern=r"^checkout$")],
    states={
        CHECKOUT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_get_name)],
        CHECKOUT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_get_phone)],
        CHECKOUT_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_get_address)],
    },
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
)

#__PAYMENT__
# ====================================================================
#  卡转账付款
# ====================================================================
async def payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":")[1])
    order = db_get_order(order_id)
    if not order:
        await query.edit_message_text("未找到订单。")
        return ConversationHandler.END
    await query.edit_message_text(
        f"💳 收款卡号：\n<code>{PAYMENT_CARD_NUMBER}</code>\n\n"
        f"应付金额: {format_price(order['final_price'])}\n\n"
        "转账后，请输入你的付款卡号（末位/备注均可）：",
        parse_mode=ParseMode.HTML,
    )
    context.user_data["order_id"] = order_id
    return PAYMENT_CARD_NUMBER_STATE

async def payment_get_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["card_number"] = update.message.text.strip()
    await update.message.reply_text("现在请发送付款回执的图片：")
    return PAYMENT_RECEIPT

async def payment_get_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    order_id = context.user_data.get("order_id")
    if not order_id:
        await update.message.reply_text("出错了，未找到订单。")
        return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text("请发送一张图片。")
        return PAYMENT_RECEIPT
    photo_id = update.message.photo[-1].file_id
    card_number = context.user_data.get("card_number", "")
    order = db_get_order(order_id)
    if not order:
        await update.message.reply_text("订单无效。")
        return ConversationHandler.END
    payment_id = db_add_payment(order_id, user.id, card_number, photo_id, order["final_price"])
    db_update_order_payment_status(order_id, "awaiting_verify")
    await update.message.reply_text(
        "✅ 已收到你的回执。管理员确认后，你的订单将开始处理。",
        reply_markup=main_menu_keyboard(user.id)
    )
    admin_text = (
        f"💰 <b>新付款</b>\n"
        f"订单 #{order_id}\n"
        f"金额: {format_price(order['final_price'])}\n"
        f"付款卡号: {card_number}\n"
        f"用户: {user.first_name} (@{user.username or 'no_username'})"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 确认付款", callback_data=f"verify_payment:{payment_id}"),
         InlineKeyboardButton("❌ 拒绝付款", callback_data=f"reject_payment:{payment_id}")]
    ])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(chat_id=admin_id, photo=photo_id, caption=admin_text,
                                         parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except Exception as e:
            logger.info("向管理员 %s 发送回执失败: %s", admin_id, e)
    context.user_data.clear()
    return ConversationHandler.END

payment_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(payment_start, pattern=r"^pay:\d+$")],
    states={
        PAYMENT_CARD_NUMBER_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_get_card)],
        PAYMENT_RECEIPT: [MessageHandler(filters.PHOTO, payment_get_receipt)],
    },
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
)

async def verify_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("你没有权限。", show_alert=True)
        return
    action, payment_id = query.data.split(":")
    payment_id = int(payment_id)
    payment = db_get_payment(payment_id)
    if not payment:
        await query.answer("未找到付款记录。")
        return
    if payment["status"] != "pending":
        await query.answer("该付款已处理过。", show_alert=True)
        return

    if action == "verify_payment":
        db_update_payment_status(payment_id, "verified", verified_by=query.from_user.id)
        db_update_order_payment_status(payment["order_id"], "paid")
        db_update_order_status(payment["order_id"], "confirmed")
        await query.answer("付款已确认。")
        try:
            await context.bot.send_message(
                chat_id=payment["user_id"],
                text=f"✅ 订单 #{payment['order_id']} 的付款已确认，正在处理中。"
            )
        except Exception:
            pass
        # 返佣（原子领取，杜绝重复发放）
        order = db_get_order(payment["order_id"])
        if order and order["referrer_id"] and try_award_referral(order["id"]):
            bonus = int(order["final_price"] * REFERRAL_BONUS_PERCENT / 100)
            if bonus > 0:
                add_wallet_transaction(order["referrer_id"], bonus, f"订单 #{order['id']} 邀请返佣")
                try:
                    await context.bot.send_message(
                        chat_id=order["referrer_id"],
                        text=f"🎉 邀请返佣 {format_price(bonus)} 已加入你的钱包。"
                    )
                except Exception:
                    pass
        await _edit_result(query, f"✅ 付款已确认，订单 #{payment['order_id']} 现已确认。")
    else:
        db_update_payment_status(payment_id, "rejected", verified_by=query.from_user.id)
        db_update_order_payment_status(payment["order_id"], "unpaid")
        await query.answer("付款已拒绝。")
        try:
            await context.bot.send_message(
                chat_id=payment["user_id"],
                text=f"❌ 订单 #{payment['order_id']} 的付款被拒绝，请联系客服。"
            )
        except Exception:
            pass
        await _edit_result(query, f"❌ 付款已拒绝，订单 #{payment['order_id']}。")


async def _edit_result(query, text):
    """兼容图片消息(caption)与文本消息(text)两种情况的编辑。"""
    try:
        if query.message.photo:
            await query.edit_message_caption(text)
        else:
            await query.edit_message_text(text)
    except Exception as e:
        logger.info("编辑结果消息失败: %s", e)

#__MYORDERS__
# ====================================================================
#  我的订单
# ====================================================================
async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    orders = db_get_user_orders(user_id)
    if not orders:
        await update.message.reply_text("你还没有任何订单。")
        return
    for order in orders:
        items = db_get_order_items(order["id"])
        items_text = "\n".join([f"• {it['product_name']} - {it['variant_name']} × {it['quantity']}" for it in items])
        text = (
            f"🧾 <b>订单 #{order['id']}</b>\n"
            f"📅 日期: {order['created_at'][:10]}\n"
            f"💰 金额: {format_price(order['final_price'])}\n"
            f"📌 状态: {STATUS_LABELS.get(order['status'], order['status'])}\n"
            f"💳 付款: {PAYMENT_STATUS_LABELS.get(order['payment_status'], order['payment_status'])}\n\n"
            f"🛍 商品:\n{items_text}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

#__SUPPORT__
# ====================================================================
#  客服工单
# ====================================================================
async def support_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("请输入你的问题主题：", reply_markup=ReplyKeyboardRemove())
    return TICKET_SUBJECT

async def support_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ticket_subject"] = update.message.text.strip()
    await update.message.reply_text("请写下你的具体问题：")
    return TICKET_MESSAGE

async def support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subject = context.user_data.get("ticket_subject", "无主题")
    message = update.message.text.strip()
    user = update.effective_user
    ticket_id = db_create_ticket(user.id, subject, message)
    await update.message.reply_text(
        f"✅ 你的工单 #{ticket_id} 已提交。\n我们会尽快回复你。",
        reply_markup=main_menu_keyboard(user.id)
    )
    admin_text = (
        f"📩 <b>新工单 #{ticket_id}</b>\n"
        f"来自: {user.first_name} (@{user.username or 'no_username'})\n"
        f"主题: {subject}\n"
        f"内容: {message}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ 回复工单", callback_data=f"reply_ticket:{ticket_id}")]
    ])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_text,
                                           parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except Exception:
            pass
    return ConversationHandler.END

async def admin_reply_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("你没有权限。", show_alert=True)
        return ConversationHandler.END
    ticket_id = int(query.data.split(":")[1])
    context.user_data["reply_ticket_id"] = ticket_id
    await query.answer()
    await query.edit_message_text("请输入你的回复内容：")
    return TICKET_RESPONSE

async def admin_reply_ticket_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ticket_id = context.user_data.get("reply_ticket_id")
    if not ticket_id:
        await update.message.reply_text("出错了。")
        return ConversationHandler.END
    response = update.message.text.strip()
    db_update_ticket_response(ticket_id, response)
    ticket = db_get_ticket(ticket_id)
    if ticket:
        try:
            await context.bot.send_message(
                chat_id=ticket["user_id"],
                text=f"📩 工单 #{ticket_id} 的回复：\n\n{response}\n\n如需关闭工单，请发送 /close_ticket {ticket_id}"
            )
        except Exception:
            pass
    await update.message.reply_text("你的回复已发送。", reply_markup=admin_menu_keyboard())
    return ConversationHandler.END

async def close_ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("用法： /close_ticket <工单号>")
        return
    try:
        ticket_id = int(context.args[0])
        ticket = db_get_ticket(ticket_id)
        # 允许工单所属用户或管理员关闭
        if not ticket or (ticket["user_id"] != update.effective_user.id and not is_admin(update.effective_user.id)):
            await update.message.reply_text("未找到工单，或该工单不属于你。")
            return
        db_close_ticket(ticket_id)
        await update.message.reply_text(f"工单 #{ticket_id} 已关闭。")
    except ValueError:
        await update.message.reply_text("工单号无效。")

async def admin_close_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("你没有权限。", show_alert=True)
        return
    ticket_id = int(query.data.split(":")[1])
    db_close_ticket(ticket_id)
    await query.answer("工单已关闭。")
    await query.edit_message_text(f"工单 #{ticket_id} 已关闭。")

#__ADMIN_PRODUCT__
# ====================================================================
#  管理后台：商品与规格
# ====================================================================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("权限受限。")
        return
    await update.message.reply_text("管理面板：", reply_markup=admin_menu_keyboard())

async def add_category_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text("请输入新分类名称（或 /cancel 取消）：", reply_markup=ReplyKeyboardRemove())
    return ADD_PRODUCT_CATEGORY

async def add_category_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    try:
        db_add_category(name)
        await update.message.reply_text(f"分类「{name}」已添加 ✅", reply_markup=admin_menu_keyboard())
    except sqlite3.IntegrityError:
        await update.message.reply_text("该分类已存在。", reply_markup=admin_menu_keyboard())
    return ConversationHandler.END

add_category_conversation = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^➕ 添加分类$"), add_category_start)],
    states={ADD_PRODUCT_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_category_save)]},
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
)

async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    categories = db_get_categories()
    if not categories:
        await update.message.reply_text("请先创建分类。")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(cat["name"], callback_data=f"addprod_cat:{cat['id']}")] for cat in categories]
    await update.message.reply_text("商品归属哪个分类？", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_PRODUCT_CATEGORY

async def add_product_category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_id = int(query.data.split(":")[1])
    context.user_data["new_product_cat"] = cat_id
    await query.edit_message_text("请输入商品名称：")
    return ADD_PRODUCT_NAME

async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product_name"] = update.message.text.strip()
    await update.message.reply_text("请输入商品描述（或输入 '-' 跳过）：")
    return ADD_PRODUCT_DESCRIPTION

async def add_product_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    if desc == "-":
        desc = ""
    db_add_product(
        category_id=context.user_data["new_product_cat"],
        name=context.user_data["new_product_name"],
        description=desc
    )
    await update.message.reply_text(
        f"商品「{context.user_data['new_product_name']}」已添加。现在可以为它添加规格了。\n"
        "请在管理菜单选择「➕ 添加规格」。",
        reply_markup=admin_menu_keyboard()
    )
    return ConversationHandler.END

add_product_conversation = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^➕ 添加商品$"), add_product_start)],
    states={
        ADD_PRODUCT_CATEGORY: [CallbackQueryHandler(add_product_category_chosen, pattern=r"^addprod_cat:")],
        ADD_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
        ADD_PRODUCT_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_description)],
    },
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
)

async def add_variant_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    products = db_get_all_products_with_possible_variants()
    unique_products = {}
    for row in products:
        if row["product_id"] not in unique_products:
            unique_products[row["product_id"]] = row["product_name"]
    if not unique_products:
        await update.message.reply_text("还没有任何商品，请先添加商品。")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(name, callback_data=f"variant_prod:{pid}")] for pid, name in unique_products.items()]
    await update.message.reply_text("请选择商品：", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_VARIANT_PRODUCT

async def add_variant_product_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split(":")[1])
    context.user_data["variant_product_id"] = product_id
    await query.edit_message_text("请输入规格名称（例如：'红色 - 40码'）：")
    return ADD_VARIANT_NAME

async def add_variant_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["variant_name"] = update.message.text.strip()
    await update.message.reply_text("请输入该规格的价格（单位：元，仅数字）：")
    return ADD_VARIANT_PRICE

async def add_variant_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = int(update.message.text.strip().replace(",", ""))
        if price < 0:
            raise ValueError
        context.user_data["variant_price"] = price
        await update.message.reply_text("请输入库存数量：")
        return ADD_VARIANT_STOCK
    except ValueError:
        await update.message.reply_text("请输入有效的数字。")
        return ADD_VARIANT_PRICE

async def add_variant_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stock = int(update.message.text.strip())
        if stock < 0:
            raise ValueError
        context.user_data["variant_stock"] = stock
        await update.message.reply_text("（可选）发送一张该规格的图片，或输入 '-' 跳过：")
        return ADD_VARIANT_PHOTO
    except ValueError:
        await update.message.reply_text("请输入有效的数字。")
        return ADD_VARIANT_STOCK

async def add_variant_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
    elif update.message.text and update.message.text.strip() != "-":
        await update.message.reply_text("请发送图片或输入 '-'。")
        return ADD_VARIANT_PHOTO
    db_add_variant(
        product_id=context.user_data["variant_product_id"],
        variant_name=context.user_data["variant_name"],
        price=context.user_data["variant_price"],
        stock=context.user_data["variant_stock"],
        photo_file_id=photo_id
    )
    await update.message.reply_text(
        f"✅ 规格「{context.user_data['variant_name']}」已添加。",
        reply_markup=admin_menu_keyboard()
    )
    return ConversationHandler.END

add_variant_conversation = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^➕ 添加规格$"), add_variant_start)],
    states={
        ADD_VARIANT_PRODUCT: [CallbackQueryHandler(add_variant_product_chosen, pattern=r"^variant_prod:")],
        ADD_VARIANT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_variant_name)],
        ADD_VARIANT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_variant_price)],
        ADD_VARIANT_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_variant_stock)],
        ADD_VARIANT_PHOTO: [MessageHandler((filters.PHOTO | filters.TEXT) & ~filters.COMMAND, add_variant_photo)],
    },
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
)

async def admin_list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    products = db_get_all_products_with_possible_variants()
    if not products:
        await update.message.reply_text("还没有任何商品。")
        return
    grouped = {}
    for row in products:
        pid = row["product_id"]
        if pid not in grouped:
            grouped[pid] = {"name": row["product_name"], "category": row["category_name"], "variants": []}
        if row["variant_id"] is not None:
            grouped[pid]["variants"].append({
                "variant_name": row["variant_name"], "price": row["price"], "stock": row["stock"]
            })
    for pid, data in grouped.items():
        text = f"🛍 <b>{data['name']}</b>（分类: {data['category']}）\n"
        if data["variants"]:
            for v in data["variants"]:
                text += f"   • {v['variant_name']} - {format_price(v['price'])}（库存: {v['stock']}）\n"
        else:
            text += "   ⚠️ 无规格（无法购买）\n"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 删除商品", callback_data=f"delprod:{pid}")]
        ])
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def admin_delete_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("你没有权限。", show_alert=True)
        return
    product_id = int(query.data.split(":")[1])
    db_deactivate_product(product_id)
    await query.answer("商品已下架")
    await query.edit_message_text("✅ 商品已下架。")

#__ADMIN_ORDER__
# ====================================================================
#  管理后台：订单管理
# ====================================================================
async def admin_show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    orders = db_get_all_orders(limit=20)
    if not orders:
        await update.message.reply_text("暂无订单。")
        return
    for order in orders:
        text = (
            f"🧾 订单 #{order['id']}\n"
            f"👤 {order['full_name']} | 📞 {order['phone']}\n"
            f"💰 {format_price(order['final_price'])}"
            f"（优惠: {format_price(order['discount_amount'])}）\n"
            f"📌 状态: {STATUS_LABELS.get(order['status'], order['status'])}\n"
            f"💳 付款: {PAYMENT_STATUS_LABELS.get(order['payment_status'], order['payment_status'])}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 确认订单", callback_data=f"ordstatus:{order['id']}:confirmed"),
                InlineKeyboardButton("📦 已发货", callback_data=f"ordstatus:{order['id']}:shipped"),
                InlineKeyboardButton("❌ 取消", callback_data=f"ordstatus:{order['id']}:cancelled")
            ]
        ])
        await update.message.reply_text(text, reply_markup=keyboard)

async def order_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("你没有权限。", show_alert=True)
        return
    _, order_id_str, new_status = query.data.split(":")
    order_id = int(order_id_str)

    # 取消订单：走回滚流程（回库存 + 已付款则退款 + 扣回返佣）
    if new_status == "cancelled":
        result = cancel_order_and_restore(order_id, operator_id=query.from_user.id)
        if isinstance(result, str):
            await query.answer(result, show_alert=True)
            return
        buyer_id, refunded = result
        await query.answer("订单已取消，库存已回滚。")
        try:
            msg = f"你的订单 #{order_id} 已被取消。"
            if refunded > 0:
                msg += f"\n已退款 {format_price(refunded)} 到你的钱包。"
            await context.bot.send_message(chat_id=buyer_id, text=msg)
        except Exception:
            pass
        note = f"订单 #{order_id} 已取消，库存已回滚。"
        if refunded > 0:
            note += f" 已退款 {format_price(refunded)}。"
        await query.edit_message_text(note)
        return

    db_update_order_status(order_id, new_status)
    await query.answer(f"状态已改为 {STATUS_LABELS.get(new_status, new_status)}。")
    order = db_get_order(order_id)
    if order:
        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=f"你的订单 #{order_id} 状态已变为 {STATUS_LABELS.get(new_status, new_status)}。"
            )
        except Exception:
            pass
    await query.edit_message_text(f"订单 #{order_id} 状态已更新。")

#__ADMIN_PAYMENT__
# ====================================================================
#  管理后台：支付管理
# ====================================================================
async def admin_show_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    payments = db_get_pending_payments()
    if not payments:
        await update.message.reply_text("没有待确认的付款。")
        return
    for payment in payments:
        order = db_get_order(payment["order_id"])
        if not order:
            continue
        text = (
            f"💰 付款 #{payment['id']} - 订单 #{payment['order_id']}\n"
            f"金额: {format_price(payment['amount'])}\n"
            f"用户: {order['full_name']}（ID: {payment['user_id']}）"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 确认", callback_data=f"verify_payment:{payment['id']}"),
             InlineKeyboardButton("❌ 拒绝", callback_data=f"reject_payment:{payment['id']}")]
        ])
        if payment["receipt_photo_id"]:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=payment["receipt_photo_id"],
                                         caption=text, reply_markup=keyboard)
        else:
            await update.message.reply_text(text, reply_markup=keyboard)

#__ADMIN_COUPON__
# ====================================================================
#  管理后台：优惠券管理
# ====================================================================
async def admin_coupon_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    coupons = db_get_all_coupons()
    if coupons:
        text = "🎫 优惠券列表:\n"
        for c in coupons:
            tlabel = "百分比" if c["discount_type"] == "percent" else "固定额"
            text += (f"• {c['code']} - {tlabel} {c['discount_value']}"
                     f"（最低订单: {c['min_order_amount']}）- 使用: {c['used_count']}/{c['usage_limit']}\n")
        await update.message.reply_text(text)
    else:
        await update.message.reply_text("暂无优惠券。")
    await update.message.reply_text("要添加新优惠券，请输入券码（或 /cancel 取消）：")
    return COUPON_CODE

async def coupon_get_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    if db_get_coupon(code):
        await update.message.reply_text("该券码已存在，请换一个。")
        return COUPON_CODE
    context.user_data["new_coupon_code"] = code
    await update.message.reply_text("请选择优惠类型（输入 percent 百分比 或 fixed 固定额）：")
    return COUPON_TYPE

async def coupon_get_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dtype = update.message.text.strip().lower()
    if dtype not in ["percent", "fixed"]:
        await update.message.reply_text("请输入 percent 或 fixed。")
        return COUPON_TYPE
    context.user_data["new_coupon_type"] = dtype
    await update.message.reply_text("请输入优惠数值（percent 填 1-100 的数字；fixed 填金额，单位元）：")
    return COUPON_VALUE

async def coupon_get_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = int(update.message.text.strip())
        if context.user_data["new_coupon_type"] == "percent" and (value < 1 or value > 100):
            await update.message.reply_text("百分比需在 1 到 100 之间。")
            return COUPON_VALUE
        context.user_data["new_coupon_value"] = value
        await update.message.reply_text("请输入使用该券的最低订单金额（0 表示不限）：")
        return COUPON_MIN_ORDER
    except ValueError:
        await update.message.reply_text("请输入有效的数字。")
        return COUPON_VALUE

async def coupon_get_min_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        min_order = int(update.message.text.strip())
        context.user_data["new_coupon_min_order"] = min_order
        await update.message.reply_text("请输入过期日期（格式 YYYY-MM-DD），或输入 '-' 表示不过期：")
        return COUPON_EXPIRY
    except ValueError:
        await update.message.reply_text("请输入有效的数字。")
        return COUPON_MIN_ORDER

async def coupon_get_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expiry = None
    txt = update.message.text.strip()
    if txt != "-":
        try:
            expiry = datetime.strptime(txt, "%Y-%m-%d").isoformat()
        except ValueError:
            await update.message.reply_text("日期格式错误，请重新输入（YYYY-MM-DD）或 '-'。")
            return COUPON_EXPIRY
    context.user_data["new_coupon_expiry"] = expiry
    await update.message.reply_text("请输入可使用次数（默认 1）：")
    return COUPON_LIMIT

async def coupon_get_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        limit = int(update.message.text.strip())
    except ValueError:
        limit = 1
    db_add_coupon(
        code=context.user_data["new_coupon_code"],
        discount_type=context.user_data["new_coupon_type"],
        discount_value=context.user_data["new_coupon_value"],
        min_order_amount=context.user_data["new_coupon_min_order"],
        expires_at=context.user_data["new_coupon_expiry"],
        usage_limit=limit
    )
    await update.message.reply_text(
        f"优惠券 {context.user_data['new_coupon_code']} 已添加。",
        reply_markup=admin_menu_keyboard()
    )
    context.user_data.clear()
    return ConversationHandler.END

coupon_conversation_admin = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^🎫 优惠券管理$"), admin_coupon_menu)],
    states={
        COUPON_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_get_code)],
        COUPON_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_get_type)],
        COUPON_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_get_value)],
        COUPON_MIN_ORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_get_min_order)],
        COUPON_EXPIRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_get_expiry)],
        COUPON_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_get_limit)],
    },
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
)

#__ADMIN_TICKET__
# ====================================================================
#  管理后台：工单管理
# ====================================================================
async def admin_show_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    tickets = db_get_tickets(status=None)
    if not tickets:
        await update.message.reply_text("暂无工单。")
        return
    for t in tickets:
        status_emoji = "🔴" if t["status"] == "open" else "🟡" if t["status"] == "in_progress" else "🟢"
        text = (
            f"{status_emoji} 工单 #{t['id']} - {t['subject']}\n"
            f"来自用户: {t['user_id']}\n"
            f"状态: {t['status']}\n"
            f"内容: {(t['message'] or '')[:100]}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ 回复", callback_data=f"reply_ticket:{t['id']}")],
            [InlineKeyboardButton("❌ 关闭工单", callback_data=f"close_ticket:{t['id']}")]
        ])
        await update.message.reply_text(text, reply_markup=keyboard)

#__ADMIN_STATS__
# ====================================================================
#  统计、用户、返回
# ====================================================================
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    stats = db_get_stats()
    text = (
        "📊 <b>店铺统计</b>\n\n"
        f"🧾 订单总数: {stats['total_orders']}\n"
        f"⏳ 待处理订单: {stats['pending_orders']}\n"
        f"💰 总销售额: {format_price(stats['total_revenue'])}\n"
        f"💳 已付款订单: {stats['paid_orders']}\n"
        f"🛍 上架商品: {stats['products_count']}\n"
        f"👥 注册用户: {stats['users_count']}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def admin_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with closing(get_conn()) as conn:
        users = conn.execute("SELECT * FROM users ORDER BY id LIMIT 50").fetchall()
    if not users:
        await update.message.reply_text("还没有注册用户。")
        return
    text = "👥 用户列表:\n"
    for u in users:
        text += (f"• {u['first_name']} (@{u['username'] or 'no_username'}) - "
                 f"邀请码: {u['referral_code']} - 钱包: {format_price(u['wallet_balance'])}\n")
    await update.message.reply_text(text)

async def admin_back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("已返回主菜单。", reply_markup=main_menu_keyboard(update.effective_user.id))

async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_text("已返回主菜单。")
    except Exception:
        pass

#__MAIN__
# ====================================================================
#  注册处理器并运行
# ====================================================================
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel_conversation))
    app.add_handler(CommandHandler("close_ticket", close_ticket_command))

    app.add_handler(coupon_conversation)
    app.add_handler(checkout_conversation)
    app.add_handler(payment_conversation)
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📞 客服$"), support_start)],
        states={
            TICKET_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, support_subject)],
            TICKET_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, support_message)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    ))

    app.add_handler(add_category_conversation)
    app.add_handler(add_product_conversation)
    app.add_handler(add_variant_conversation)
    app.add_handler(coupon_conversation_admin)

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_reply_ticket_callback, pattern=r"^reply_ticket:")],
        states={TICKET_RESPONSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reply_ticket_text)]},
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    ))

    app.add_handler(MessageHandler(filters.Regex("^🛍 浏览商品$"), show_categories))
    app.add_handler(MessageHandler(filters.Regex("^🛒 购物车$"), show_cart))
    app.add_handler(MessageHandler(filters.Regex("^🧾 我的订单$"), my_orders))

    app.add_handler(MessageHandler(filters.Regex("^⚙️ 管理面板$"), admin_panel))
    app.add_handler(MessageHandler(filters.Regex("^🔙 返回主菜单$"), admin_back_to_main))
    app.add_handler(MessageHandler(filters.Regex("^📋 商品列表$"), admin_list_products))
    app.add_handler(MessageHandler(filters.Regex("^🧾 订单管理$"), admin_show_orders))
    app.add_handler(MessageHandler(filters.Regex("^💳 支付管理$"), admin_show_payments))
    app.add_handler(MessageHandler(filters.Regex("^📩 客服工单$"), admin_show_tickets))
    app.add_handler(MessageHandler(filters.Regex("^📊 销售统计$"), admin_stats))
    app.add_handler(MessageHandler(filters.Regex("^👥 用户$"), admin_users_list))

    app.add_handler(CallbackQueryHandler(show_products_in_category, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(show_variants, pattern=r"^variants:"))
    app.add_handler(CallbackQueryHandler(add_to_cart_callback, pattern=r"^add:"))
    app.add_handler(CallbackQueryHandler(remove_cart_item_callback, pattern=r"^rmcart:"))
    app.add_handler(CallbackQueryHandler(clear_cart_callback, pattern=r"^clearcart$"))
    app.add_handler(CallbackQueryHandler(verify_payment_callback, pattern=r"^(verify_payment|reject_payment):"))
    app.add_handler(CallbackQueryHandler(order_status_callback, pattern=r"^ordstatus:"))
    app.add_handler(CallbackQueryHandler(admin_delete_product_callback, pattern=r"^delprod:"))
    app.add_handler(CallbackQueryHandler(admin_close_ticket_callback, pattern=r"^close_ticket:"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))

    logger.info("电商机器人已启动。")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
