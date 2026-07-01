# ============================================================
# api_server.py — API server đọc SQLite, trả JSON cho website
# GIAI ĐOẠN 3: AI Agent Chat (/chat, /prices)
# GIAI ĐOẠN 5: License EA System (/api/license/*)
# ============================================================
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import sqlite3
import os
import sys
import urllib.request
from datetime import datetime, date
from urllib.parse import urlparse, parse_qs
import risk_engine as risk
import learning_engine as learning
try:
    import forex_intelligence as fx
    FX_INTEL_AVAILABLE = True
except ImportError as e:
    FX_INTEL_AVAILABLE = False
    print(f"⚠️  Chưa cài pandas hoặc lỗi import forex_intelligence.py: {e}")
    print("    → chạy: pip install pandas --break-system-packages")

DB_PATH      = os.path.join(os.path.dirname(__file__), "signals.db")
LICENSE_DB   = os.path.join(os.path.dirname(__file__), "licenses.db")

# ── Import MT5 ───────────────────────────────────────────────
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = mt5.initialize()
    if MT5_AVAILABLE:
        print("✅ MT5 đã kết nối")
    else:
        print("⚠️  MT5 chưa kết nối — /prices sẽ trả về rỗng")
except ImportError:
    MT5_AVAILABLE = False
    print("⚠️  Chưa cài MetaTrader5 — /prices sẽ trả về rỗng")

# ── Import DeepSeek (OpenAI-compatible SDK) ───────────────────
try:
    from openai import OpenAI
    DEEPSEEK_AVAILABLE = True
    print("✅ OpenAI SDK sẵn sàng (dùng cho DeepSeek)")
except ImportError:
    DEEPSEEK_AVAILABLE = False
    print("⚠️  Chưa cài openai — chạy: pip install openai")

# ── Đọc config ───────────────────────────────────────────────
try:
    sys.path.insert(0, os.path.dirname(__file__))
    import config
    from config import (DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL_CHAT,
                         DEEPSEEK_MODEL_REASONING, BOT_TOKEN, CHAT_ID, ADMIN_PASSWORD)
    deepseek_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL) if DEEPSEEK_AVAILABLE else None
except Exception:
    BOT_TOKEN = ""
    CHAT_ID   = ""
    ADMIN_PASSWORD = "admin2026"
    deepseek_client = None

# ── Admin session tokens (in-memory) ──────────────────────────
# Token biến mất khi restart server — admin cần đăng nhập lại, chấp nhận được
# cho quy mô hiện tại (1 admin, chạy trên máy cá nhân).
import secrets as _secrets
ADMIN_TOKENS = set()


# ════════════════════════════════════════════════════════════
# SIGNALS DB HELPERS
# ════════════════════════════════════════════════════════════

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Tạo bảng confluence nếu chưa có
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_confluence (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id   INTEGER NOT NULL,
            score       INTEGER DEFAULT 0,
            label       TEXT,
            factors     TEXT,
            strengths   TEXT,
            warnings    TEXT,
            summary     TEXT,
            show_on_web INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        )
    """)
    conn.commit()
    return conn

def get_market_snapshot():
    if not MT5_AVAILABLE:
        return {}, "MT5 chưa kết nối."
    symbols = {
        "XAUUSDs": ("xauusd", "XAU/USD"),
        "BTCUSDs": ("btcusd", "BTC/USD"),
        "EURUSDs": ("eurusd", "EUR/USD"),
        "USOILs":  ("usoil",  "US OIL"),
    }
    prices = {}
    text_lines = []
    for sym_mt5, (key, label) in symbols.items():
        tick = mt5.symbol_info_tick(sym_mt5)
        if not tick:
            tick = mt5.symbol_info_tick(sym_mt5.rstrip("s"))
        if tick:
            prices[key] = round(tick.bid, 5)
            spread = round(tick.ask - tick.bid, 5)
            text_lines.append(
                f"• {label}: Bid={tick.bid:.5f}  Ask={tick.ask:.5f}  Spread={spread:.5f}"
            )
    market_text = "\n".join(text_lines) if text_lines else "Không lấy được giá từ MT5."
    return prices, market_text

def get_recent_signals_text(limit=5):
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT symbol, direction, entry_price, sl_price, tp_price, created_at "
            "FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        if not rows:
            return "Chưa có tín hiệu gần đây."
        lines = ["5 tín hiệu gần nhất:"]
        for r in rows:
            lines.append(
                f"• [{str(r['created_at'])[:16]}] {r['symbol']} {r['direction']} "
                f"Entry={r['entry_price']} SL={r['sl_price']} TP={r['tp_price']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Lỗi đọc database: {e}"


# ════════════════════════════════════════════════════════════
# LICENSE DB HELPERS
# ════════════════════════════════════════════════════════════

def get_license_conn():
    conn = sqlite3.connect(LICENSE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            telegram     TEXT,
            chat_id      INTEGER,
            account      INTEGER NOT NULL,
            broker       TEXT,
            plan         TEXT NOT NULL DEFAULT 'ib',
            license_key  TEXT,
            status       TEXT NOT NULL DEFAULT 'pending',
            ib_link      TEXT,
            payment_note TEXT,
            note         TEXT,
            expires_at   TEXT,
            created_at   TEXT DEFAULT (datetime('now','localtime')),
            approved_at  TEXT,
            updated_at   TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            telegram   TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id TEXT,
            amount         INTEGER,
            description    TEXT,
            status         TEXT DEFAULT 'paid',
            created_at     TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()
    return conn

def notify_admin_new_reg(name, account, plan, telegram):
    """Gửi TG notify cho admin khi có đơn mới."""
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        msg = (f"🔔 *Đơn đăng ký EA mới!*\n\n"
               f"👤 Tên: {name}\n"
               f"📊 Account: `{account}`\n"
               f"📦 Gói: {'⭐ IB Free' if plan == 'ib' else '💳 Rental $30/tháng'}\n"
               f"✈ Telegram: {telegram}\n\n"
               f"👉 Vào Admin Panel để duyệt!")
        payload = json.dumps({
            "chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload, headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[TG NOTIFY] {e}")

EA_DOWNLOAD_LINK = "https://drive.google.com/file/d/1ayTcI4wCl-6WGfz9dc_Phgfm6ubOk6WZ/view?usp=drive_link"

def send_key_to_customer(row, key):
    """Gửi license key cho khách qua Telegram."""
    if not BOT_TOKEN:
        return
    try:
        plan_name = '⭐ IB Free (vĩnh viễn)' if row['plan'] == 'ib' \
                    else f"💳 Rental (hết hạn: {row['expires_at']})"
        telegram = str(row['telegram']).replace('@', '')

        # Dùng plain text tránh lỗi Markdown 400
        msg = (f"✅ License Key SmartGold EA đã được cấp!\n\n"
               f"👤 Xin chào {row['name']}!\n\n"
               f"🔑 License Key:\n{key}\n\n"
               f"📊 Account MT5: {row['account']}\n"
               f"📦 Gói: {plan_name}\n\n"
               f"📥 Tải file EA tại đây:\n{EA_DOWNLOAD_LINK}\n\n"
               f"Cách cài đặt:\n"
               f"1. Tải file SmartGoldRecovery.ex5 từ link trên\n"
               f"2. Copy vào: MT5 → MQL5 → Experts\n"
               f"3. Restart MT5 → kéo EA vào chart XAUUSDs\n"
               f"4. Input LicenseKey → dán key vào\n"
               f"5. Tools → Options → Expert Advisors\n"
               f"   Thêm URL: trading-proxy.trannguyenthienminh.workers.dev\n"
               f"6. Bấm OK → EA sẽ tự kích hoạt ✅\n\n"
               f"💬 Hỗ trợ: t.me/TradingAI_Support")

        # Thử dùng chat_id số (lưu từ /start) trước
        chat_id_num = row.get('chat_id') if isinstance(row, dict) else None
        sent = False

        if chat_id_num:
            try:
                payload = json.dumps({
                    "chat_id": chat_id_num, "text": msg
                }).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    data=payload, headers={'Content-Type': 'application/json'}
                )
                urllib.request.urlopen(req, timeout=5)
                sent = True
                print(f"[TG SEND KEY] ✅ Gửi key qua chat_id {chat_id_num}")
            except Exception as e:
                print(f"[TG SEND KEY] chat_id thất bại: {e}")

        # Fallback: thử gửi qua @username
        if not sent and telegram:
            try:
                payload = json.dumps({
                    "chat_id": f"@{telegram}", "text": msg
                }).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    data=payload, headers={'Content-Type': 'application/json'}
                )
                urllib.request.urlopen(req, timeout=5)
                sent = True
                print(f"[TG SEND KEY] ✅ Gửi key qua @{telegram}")
            except Exception as e:
                print(f"[TG SEND KEY] @username thất bại: {e}")

        # Fallback cuối: notify admin gửi tay
        if not sent:
            try:
                admin_msg = (f"⚠️ *Cần gửi key tay cho khách!*\n\n"
                             f"👤 {row['name']} | @{telegram}\n"
                             f"📊 Account: `{row['account']}`\n"
                             f"🔑 Key: `{key}`\n\n"
                             f"Bot không DM được — khách chưa /start bot!")
                payload = json.dumps({
                    "chat_id": CHAT_ID, "text": admin_msg, "parse_mode": "Markdown"
                }).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    data=payload, headers={'Content-Type': 'application/json'}
                )
                urllib.request.urlopen(req, timeout=5)
                print(f"[TG SEND KEY] ⚠️ Đã notify admin gửi tay")
            except Exception as e:
                print(f"[TG SEND KEY] notify admin lỗi: {e}")

    except Exception as e:
        print(f"[TG SEND KEY] {e}")


# ════════════════════════════════════════════════════════════
# HTTP HANDLER
# ════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"  {args[0]} {args[1]} {args[2]}")

    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('ngrok-skip-browser-warning',   'true')

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def require_admin(self):
        """
        Kiểm tra header Authorization: Bearer <token> có hợp lệ không.
        Trả về True nếu hợp lệ. Nếu không hợp lệ, tự gửi lỗi 401 và trả về False
        — handler gọi hàm này chỉ cần `if not self.require_admin(): return`.
        """
        auth = self.headers.get('Authorization', '')
        token = auth.replace('Bearer ', '').strip()
        if not token or token not in ADMIN_TOKENS:
            self.send_json({"error": "Unauthorized — vui lòng đăng nhập lại"}, 401)
            return False
        return True

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    # ── GET ──────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path   = parsed.path
        try:
            if   path == '/api/summary':              self.handle_summary()
            elif path == '/api/signals':              self.handle_signals(params.get('status',[None])[0], params.get('symbol',[None])[0])
            elif path == '/api/signals/confluence':   self.handle_confluence_signals()
            elif path == '/api/analytics':            self.handle_analytics()
            elif path == '/api/journal':              self.handle_journal(params)
            elif path == '/api/decisions':            self.handle_decisions(params)
            elif path == '/api/risk-status':          self.handle_risk_status()
            elif path == '/api/learning':             self.handle_learning(params)
            elif path == '/prices':                   self.handle_prices()
            elif path == '/api/candles':              self.handle_candles(params)
            elif path == '/api/license/verify':       self.handle_license_verify(params)
            elif path == '/api/license/list':
                if self.require_admin(): self.handle_license_list()
            elif path == '/api/payments':
                if self.require_admin(): self.handle_payments_list()
            elif path == '/health':                   self.send_json({"status": "ok", "time": str(datetime.now())})
            else:                                     self.send_json({"error": "Not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    # ── POST ─────────────────────────────────────────────────
    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        try:
            if   path == '/chat':                         self.handle_chat()
            elif path == '/api/login/admin':               self.handle_admin_login()
            elif path == '/api/license/register':         self.handle_license_register()
            elif path == '/api/license/approve':
                if self.require_admin(): self.handle_license_approve()
            elif path == '/api/license/revoke':
                if self.require_admin(): self.handle_license_revoke()
            elif path == '/api/license/reactivate':
                if self.require_admin(): self.handle_license_reactivate()
            elif path == '/api/license/renew':
                if self.require_admin(): self.handle_license_renew()
            elif path == '/api/license/set_chat_id':      self.handle_set_chat_id()
            elif path == '/api/payment/sepay':            self.handle_sepay_webhook()
            elif path == '/api/telegram/send':
                if self.require_admin(): self.handle_telegram_send()
            elif path == '/api/member/login':              self.handle_member_login()
            elif path == '/api/member/renew-request':      self.handle_member_renew_request()
            else:                                         self.send_json({"error": "Not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    # ── /api/login/admin ─────────────────────────────────────
    def handle_admin_login(self):
        """POST /api/login/admin — { password } → { success, token }"""
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json({"success": False, "error": "JSON không hợp lệ"}, 400)
            return
        password = str(data.get('password', ''))
        if password and password == ADMIN_PASSWORD:
            token = _secrets.token_hex(32)
            ADMIN_TOKENS.add(token)
            print("[ADMIN LOGIN] ✅ Đăng nhập thành công")
            self.send_json({"success": True, "token": token})
        else:
            print("[ADMIN LOGIN] ❌ Sai mật khẩu")
            self.send_json({"success": False, "error": "Sai mật khẩu"}, 401)

    # ── /prices ──────────────────────────────────────────────
    def handle_prices(self):
        prices_dict, _ = get_market_snapshot()
        self.send_json(prices_dict)

    # ── /api/candles — Phase 3: dữ liệu nến cho Lightweight Charts ──
    _CANDLE_TF_MAP = None  # khởi tạo lazy bên dưới, cần mt5 đã import xong

    def handle_candles(self, params):
        """
        GET /api/candles?symbol=XAUUSDs&timeframe=M5&limit=300
        Trả về mảng nến OHLC theo đúng format Lightweight Charts cần:
        [{ time: <unix giây>, open, high, low, close }, ...] — cũ nhất trước.
        """
        if not MT5_AVAILABLE:
            self.send_json({"error": "MT5 chưa kết nối"}, 500)
            return

        symbol    = params.get('symbol', ['XAUUSDs'])[0]
        tf_str    = params.get('timeframe', ['M5'])[0].upper()
        limit     = int(params.get('limit', [300])[0])
        limit     = max(10, min(limit, 1000))  # chặn giá trị bất thường

        tf_map = {
            'M1': mt5.TIMEFRAME_M1, 'M5': mt5.TIMEFRAME_M5, 'M15': mt5.TIMEFRAME_M15,
            'M30': mt5.TIMEFRAME_M30, 'H1': mt5.TIMEFRAME_H1, 'H4': mt5.TIMEFRAME_H4,
            'D1': mt5.TIMEFRAME_D1,
        }
        if tf_str not in tf_map:
            self.send_json({"error": f"Timeframe không hợp lệ: {tf_str}"}, 400)
            return

        # Chỉ cho phép symbol có trong Watchlist — tránh bị dò quét symbol lạ
        allowed_symbols = {item['symbol'] for item in config.WATCHLIST}
        if symbol not in allowed_symbols:
            self.send_json({"error": f"Symbol không nằm trong watchlist: {symbol}"}, 400)
            return

        if not mt5.symbol_select(symbol, True):
            self.send_json({"error": f"Không thể chọn symbol {symbol} trên MT5"}, 500)
            return

        rates = mt5.copy_rates_from_pos(symbol, tf_map[tf_str], 0, limit)
        if rates is None or len(rates) == 0:
            self.send_json({"error": f"Không lấy được dữ liệu nến cho {symbol} {tf_str}"}, 500)
            return

        candles = [
            {
                "time":  int(r['time']),
                "open":  round(float(r['open']), 5),
                "high":  round(float(r['high']), 5),
                "low":   round(float(r['low']), 5),
                "close": round(float(r['close']), 5),
            }
            for r in rates
        ]
        self.send_json({"symbol": symbol, "timeframe": tf_str, "candles": candles})

    # ── /chat ────────────────────────────────────────────────
    def handle_chat(self):
        if not DEEPSEEK_AVAILABLE or deepseek_client is None:
            self.send_json({"error": "Chưa cài openai hoặc thiếu DEEPSEEK_API_KEY"}, 500)
            return
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json({"error": "JSON không hợp lệ"}, 400)
            return
        messages = data.get("messages")
        if not messages:
            self.send_json({"error": "Thiếu messages"}, 400)
            return
        prices_dict, market_text = get_market_snapshot()
        signals_text = get_recent_signals_text()
        system_prompt = f"""Bạn là AI Trading Agent chuyên về thị trường Vàng, Crypto và Forex.
Hỗ trợ cộng đồng Trading AI (trader Việt Nam).

GIÁ REALTIME ({datetime.now().strftime('%d/%m/%Y %H:%M')}):
{market_text}

{signals_text}

QUY TẮC TRẢ LỜI:
- Viết bằng tiếng Việt, ngắn gọn dưới 300 từ
- Dùng thuật ngữ Price Action & SMC khi phân tích
- Luôn đề cập risk management
- KHÔNG hứa hẹn lợi nhuận cụ thể
- Dùng emoji ⚡📊💡⚠️ để dễ đọc trên mobile"""
        try:
            response = deepseek_client.chat.completions.create(
                model=DEEPSEEK_MODEL_CHAT, max_tokens=1024,
                messages=[{"role": "system", "content": system_prompt}] + messages,
            )
            reply = response.choices[0].message.content
        except Exception as e:
            self.send_json({"error": f"Lỗi DeepSeek API: {str(e)}"}, 500)
            return
        self.send_json({"reply": reply, "market_snapshot": prices_dict,
                        "timestamp": datetime.now().strftime("%d/%m/%Y %H:%M:%S")})

    # ── /api/learning ─────────────────────────────────────────
    def handle_learning(self, params):
        """
        GET /api/learning — Module 3: Learning Patterns đã tích lũy đủ dữ liệu.
        Cho thấy AI 'đã học' được gì từ lịch sử thật (Symbol × Session × Type).
        """
        try:
            min_sample = int(params.get('min_sample', [5])[0])
            patterns = learning.get_all_patterns(min_sample=min_sample)
            self.send_json({
                'patterns': patterns,
                'total_patterns': len(patterns),
                'min_sample_size': min_sample,
            })
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    # ── /api/risk-status ─────────────────────────────────────
    def handle_risk_status(self):
        """
        GET /api/risk-status — Trạng thái Risk Intelligence hiện tại.
        Dùng cho widget "Trạng thái rủi ro" trên Dashboard.
        """
        try:
            session_ctx = risk.get_session_context()
            news_events = risk.fetch_upcoming_news_sync()
            news_ctx    = risk.get_news_risk_score(news_events)
            hint, msg   = risk.news_risk_decision_hint(news_ctx['risk_score'])

            # Lệnh đang mở để tính correlation tổng quan
            conn = get_conn()
            conn.row_factory = sqlite3.Row
            open_rows = conn.execute("SELECT symbol, direction FROM signals WHERE status='OPEN'").fetchall()
            conn.close()
            open_signals = [dict(r) for r in open_rows]

            # Module 4 v4.0 — Ma trận tương quan Pearson (real-time, cache 15 phút)
            if FX_INTEL_AVAILABLE:
                corr_ctx = fx.get_correlation_context()
            else:
                corr_ctx = {'note': 'Chưa cài pandas — chạy pip install pandas --break-system-packages',
                            'has_conflict': False, 'high_corr_pairs': [], 'matrix': {}}

            self.send_json({
                'session':      session_ctx,
                'news':         news_ctx,
                'news_hint':    hint,
                'news_message': msg,
                'open_positions_count': len(open_signals),
                'correlation':  {
                    'note':            corr_ctx['note'],
                    'has_conflict':    corr_ctx['has_conflict'],
                    'high_corr_pairs': corr_ctx['high_corr_pairs'],
                    'matrix':          corr_ctx['matrix'],
                },
                'checked_at':   str(datetime.now()),
            })
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    # ── /api/decisions ────────────────────────────────────────
    def handle_decisions(self, params):
        """GET /api/decisions — Lịch sử AI Decision Engine (Approve/Warn/Reject)."""
        conn = get_conn()
        conn.row_factory = sqlite3.Row
        try:
            decision_filter = params.get('decision', [None])[0]
            limit = int(params.get('limit', [100])[0])

            query = "SELECT * FROM decision_logs"
            args  = []
            if decision_filter:
                query += " WHERE decision = ?"
                args.append(decision_filter.upper())
            query += " ORDER BY id DESC LIMIT ?"
            args.append(limit)

            rows = conn.execute(query, args).fetchall()
            logs = [dict(r) for r in rows]

            # Thống kê tổng quan
            stats_rows = conn.execute(
                "SELECT decision, COUNT(*) as cnt FROM decision_logs GROUP BY decision"
            ).fetchall()
            stats = {'APPROVE': 0, 'WARN': 0, 'REJECT': 0}
            for row in stats_rows:
                stats[row['decision']] = row['cnt']

            total = sum(stats.values())
            reject_rate = round(stats['REJECT'] / total * 100, 1) if total else 0

            self.send_json({
                'logs':  logs,
                'stats': stats,
                'total': total,
                'reject_rate': reject_rate,
            })
        finally:
            conn.close()

    # ── /api/journal ──────────────────────────────────────────
    def handle_journal(self, params):
        """
        GET /api/journal — Trade Journal: danh sách tín hiệu đã đóng
        kèm đầy đủ: lý do vào lệnh (analysis_text), AI Confluence,
        và bài đánh giá hậu kiểm (post_mortem).
        Hỗ trợ filter: ?symbol=XAUUSDs&status=TP3_HIT&id=15
        """
        import json as _j
        conn = get_conn()
        conn.row_factory = sqlite3.Row
        try:
            single_id = params.get('id', [None])[0]
            if single_id:
                row = conn.execute("""
                    SELECT s.*, c.score, c.label, c.factors, c.strengths, c.warnings, c.summary
                    FROM signals s
                    LEFT JOIN signal_confluence c ON c.signal_id = s.id
                    WHERE s.id = ?
                """, (single_id,)).fetchone()
                if not row:
                    self.send_json({"error": "Không tìm thấy"}, 404)
                    return
                d = dict(row)
                for f in ('factors','strengths','warnings'):
                    try: d[f] = _j.loads(d[f]) if d[f] else {}
                    except Exception: d[f] = {}
                self.send_json({"trade": d})
                return

            symbol = params.get('symbol', [None])[0]
            status = params.get('status', [None])[0]

            query = """
                SELECT s.*, c.score, c.label, c.factors, c.strengths, c.warnings, c.summary
                FROM signals s
                LEFT JOIN signal_confluence c ON c.signal_id = s.id
                WHERE s.status != 'OPEN'
            """
            args = []
            if symbol:
                query += " AND s.symbol = ?"
                args.append(symbol)
            if status:
                query += " AND s.status = ?"
                args.append(status)
            query += " ORDER BY s.id DESC LIMIT 100"

            rows = conn.execute(query, args).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                for f in ('factors','strengths','warnings'):
                    try: d[f] = _j.loads(d[f]) if d[f] else {}
                    except Exception: d[f] = {}
                result.append(d)
            self.send_json({"trades": result, "total": len(result)})
        finally:
            conn.close()

    # ── /api/analytics ───────────────────────────────────────
    def handle_analytics(self):
        """GET /api/analytics — Performance Intelligence toàn diện."""
        conn = get_conn()
        conn.row_factory = sqlite3.Row
        try:
            # Lấy tất cả tín hiệu đã đóng
            closed = conn.execute("""
                SELECT s.*, c.score, c.label
                FROM signals s
                LEFT JOIN signal_confluence c ON c.signal_id = s.id
                WHERE s.status != 'OPEN'
                ORDER BY s.created_at ASC
            """).fetchall()
            closed = [dict(r) for r in closed]

            all_sigs = conn.execute("SELECT * FROM signals ORDER BY created_at ASC").fetchall()
            all_sigs = [dict(r) for r in all_sigs]

            if not closed:
                self.send_json({"error": "Chưa có tín hiệu đã đóng", "closed": 0})
                return

            import json as _j

            def is_win(s):
                return s['status'] in ('TP1_HIT','TP2_HIT','TP3_HIT',
                                       'SL_AFTER_TP1','SL_AFTER_TP2')

            def is_full_win(s):
                return s['status'] in ('TP2_HIT','TP3_HIT')

            def get_hour(s):
                try: return int(s['created_at'][11:13])
                except: return -1

            def get_session(hour):
                if 22 <= hour or hour < 7:  return 'Sydney/Tokyo'
                elif 7 <= hour < 12:         return 'London Open'
                elif 12 <= hour < 16:        return 'London/NY Overlap'
                elif 16 <= hour < 21:        return 'New York'
                else:                        return 'Off-hours'

            def get_weekday(s):
                try:
                    dt = datetime.strptime(s['created_at'][:10], '%Y-%m-%d')
                    return ['Thứ 2','Thứ 3','Thứ 4','Thứ 5','Thứ 6','Thứ 7','CN'][dt.weekday()]
                except: return '?'

            def get_month(s):
                try: return s['created_at'][:7]   # YYYY-MM
                except: return '?'

            def get_rr_bucket(rr):
                if rr is None: return '?'
                rr = float(rr)
                if rr < 1:    return '<1'
                elif rr < 1.5: return '1-1.5'
                elif rr < 2:   return '1.5-2'
                elif rr < 3:   return '2-3'
                else:           return '≥3'

            def get_score_bucket(score):
                if score is None: return 'No Score'
                score = int(score)
                if score >= 85:   return '85-100 (Excellent)'
                elif score >= 75: return '75-84 (Good)'
                elif score >= 60: return '60-74 (Fair)'
                else:              return '<60 (Weak)'

            def group_stats(items, key_fn):
                groups = {}
                for s in items:
                    k = key_fn(s)
                    if k not in groups:
                        groups[k] = {'total':0,'wins':0,'sl':0,'tp3':0,'pnl':0}
                    g = groups[k]
                    g['total'] += 1
                    if is_win(s):      g['wins'] += 1
                    if s['status'] == 'SL_HIT': g['sl'] += 1
                    if s['status'] == 'TP3_HIT': g['tp3'] += 1
                    if s['pnl_pips']:  g['pnl'] += float(s['pnl_pips'])
                # Tính winrate
                for k, g in groups.items():
                    t = g['total']
                    g['winrate'] = round(g['wins']/t*100, 1) if t else 0
                    g['pnl']     = round(g['pnl'], 2)
                return groups

            # ── Tổng quan ──
            total   = len(closed)
            wins    = sum(1 for s in closed if is_win(s))
            losses  = sum(1 for s in closed if s['status'] == 'SL_HIT')
            tp3_cnt = sum(1 for s in closed if s['status'] == 'TP3_HIT')
            winrate = round(wins/total*100, 1) if total else 0
            total_pnl = round(sum(float(s['pnl_pips'] or 0) for s in closed), 2)
            avg_rr  = round(sum(float(s['rr_ratio'] or 0) for s in closed)/total, 2) if total else 0

            # Streak tốt nhất
            best_streak = cur_streak = 0
            for s in closed:
                if is_win(s): cur_streak += 1; best_streak = max(best_streak, cur_streak)
                else: cur_streak = 0

            # ── Phân tích theo nhóm ──
            by_symbol    = group_stats(closed, lambda s: s['symbol'])
            by_timeframe = group_stats(closed, lambda s: s['timeframe'])
            by_direction = group_stats(closed, lambda s: s['direction'])
            by_type      = group_stats(closed, lambda s: s['signal_type'] or 'Unknown')
            by_session   = group_stats(closed, lambda s: get_session(get_hour(s)))
            by_weekday   = group_stats(closed, lambda s: get_weekday(s))
            by_month     = group_stats(closed, lambda s: get_month(s))
            by_rr        = group_stats(closed, lambda s: get_rr_bucket(s.get('rr_ratio')))
            by_score     = group_stats(closed, lambda s: get_score_bucket(s.get('score')))

            # ── Equity curve (tích lũy pnl theo thời gian) ──
            equity = []
            running = 0
            for s in closed:
                running += float(s['pnl_pips'] or 0)
                equity.append({
                    'date':   s['created_at'][:10],
                    'pnl':    round(float(s['pnl_pips'] or 0), 2),
                    'equity': round(running, 2),
                    'status': s['status'],
                    'symbol': s['symbol'],
                })

            # ── Tín hiệu gần nhất 20 lệnh ──
            recent = sorted(closed, key=lambda s: s['created_at'], reverse=True)[:20]

            self.send_json({
                'overview': {
                    'total':       total,
                    'wins':        wins,
                    'losses':      losses,
                    'tp3_hit':     tp3_cnt,
                    'open':        sum(1 for s in all_sigs if s['status']=='OPEN'),
                    'winrate':     winrate,
                    'total_pnl':   total_pnl,
                    'avg_rr':      avg_rr,
                    'best_streak': best_streak,
                },
                'by_symbol':    by_symbol,
                'by_timeframe': by_timeframe,
                'by_direction': by_direction,
                'by_type':      by_type,
                'by_session':   by_session,
                'by_weekday':   by_weekday,
                'by_month':     by_month,
                'by_rr':        by_rr,
                'by_score':     by_score,
                'equity_curve': equity,
                'recent':       recent,
            })
        finally:
            conn.close()

    # ── /api/signals/confluence ──────────────────────────────
    def handle_confluence_signals(self):
        """GET /api/signals/confluence — tín hiệu chất lượng cao cho Web Dashboard."""
        conn = get_conn()
        try:
            rows = conn.execute("""
                SELECT s.*, c.score, c.label, c.factors, c.strengths, c.warnings, c.summary
                FROM signals s
                INNER JOIN signal_confluence c ON c.signal_id = s.id
                WHERE c.show_on_web = 1
                ORDER BY s.id DESC
                LIMIT 50
            """).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                # Parse JSON fields
                for field in ('factors', 'strengths', 'warnings'):
                    try:
                        import json as _json
                        d[field] = _json.loads(d[field]) if d[field] else {}
                    except Exception:
                        d[field] = {}
                result.append(d)
            self.send_json({"signals": result, "total": len(result)})
        finally:
            conn.close()

    # ── /api/summary ─────────────────────────────────────────
    def handle_summary(self):
        conn = get_conn()
        try:
            today       = date.today().strftime('%Y-%m-%d')
            total       = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            today_count = conn.execute("SELECT COUNT(*) FROM signals WHERE created_at LIKE ?", (today+'%',)).fetchone()[0]
            open_count  = conn.execute("SELECT COUNT(*) FROM signals WHERE status='OPEN'").fetchone()[0]
            closed      = conn.execute("SELECT COUNT(*) FROM signals WHERE status != 'OPEN'").fetchone()[0]
            wins        = conn.execute("SELECT COUNT(*) FROM signals WHERE status LIKE 'TP%'").fetchone()[0]
            winrate     = round(wins / closed * 100) if closed > 0 else 0
            latest      = [dict(r) for r in conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 5").fetchall()]
            self.send_json({"total": total, "today": today_count, "open": open_count, "winrate": winrate, "latest": latest})
        finally:
            conn.close()

    # ── /api/signals ─────────────────────────────────────────
    def handle_signals(self, status=None, symbol=None):
        conn = get_conn()
        try:
            query  = "SELECT * FROM signals WHERE 1=1"
            params = []
            if status: query += " AND status=?";  params.append(status)
            if symbol: query += " AND symbol=?";  params.append(symbol)
            query += " ORDER BY id DESC LIMIT 100"
            rows = [dict(r) for r in conn.execute(query, params).fetchall()]
            self.send_json(rows)
        finally:
            conn.close()

    # ════════════════════════════════════════════════════════
    # CLIENT PORTAL 2.0 (Module 5)
    # ════════════════════════════════════════════════════════

    # ── /api/member/login ─────────────────────────────────────
    def handle_member_login(self):
        """
        POST /api/member/login — Đăng nhập Client Portal.
        Body: { "account": "12345678", "license_key": "SGR-XXXX-XXXX-XXXX" }
        """
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json({'success': False, 'error': 'JSON không hợp lệ'}, 400)
            return

        account = data.get('account', '').strip()
        key     = data.get('license_key', '').strip().upper()
        if not account or not key:
            self.send_json({'success': False, 'error': 'Thiếu Account ID hoặc License Key'}, 400)
            return

        conn = get_license_conn()
        try:
            row = conn.execute(
                "SELECT * FROM licenses WHERE account=? AND license_key=?",
                (account, key)
            ).fetchone()
            if not row:
                self.send_json({'success': False, 'error': 'Account ID hoặc License Key không đúng'})
                return

            r = dict(row)
            days_remaining = None
            if r.get('expires_at'):
                try:
                    exp = datetime.strptime(r['expires_at'], '%Y-%m-%d')
                    days_remaining = (exp - datetime.now()).days
                except Exception:
                    pass

            self.send_json({
                'success': True,
                'member': {
                    'id':             r['id'],
                    'name':           r['name'],
                    'telegram':       r.get('telegram'),
                    'account':        r['account'],
                    'broker':         r.get('broker'),
                    'plan':           r['plan'],
                    'license_key':    r['license_key'],
                    'status':         r['status'],
                    'expires_at':     r.get('expires_at'),
                    'created_at':     r.get('created_at'),
                    'days_remaining': days_remaining,
                }
            })
        finally:
            conn.close()

    # ── /api/member/renew-request ────────────────────────────
    def handle_member_renew_request(self):
        """
        POST /api/member/renew-request — Member yêu cầu gia hạn từ Portal.
        Gửi thông báo cho Admin qua Telegram, không tự động gia hạn
        (cần admin xác nhận đã nhận thanh toán trước).
        """
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json({'success': False, 'error': 'JSON không hợp lệ'}, 400)
            return

        account = data.get('account', '')
        name    = data.get('name', '')
        plan    = data.get('plan', '')
        tg      = data.get('telegram', '')

        if BOT_TOKEN and CHAT_ID:
            try:
                msg = (
                    f"🔄 *YÊU CẦU GIA HẠN TỪ CLIENT PORTAL*\n\n"
                    f"👤 Tên: {name}\n"
                    f"📊 Account: `{account}`\n"
                    f"💳 Gói: {plan}\n"
                    f"✈️ Telegram: @{tg}\n\n"
                    f"👉 Vào Admin Panel để xác nhận thanh toán và gia hạn."
                )
                payload = json.dumps({
                    "chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"
                }).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    data=payload, headers={'Content-Type': 'application/json'}
                )
                urllib.request.urlopen(req, timeout=8)
                print(f"[MEMBER PORTAL] ✅ Đã gửi yêu cầu gia hạn tới Admin — account {account}")
            except Exception as e:
                print(f"[MEMBER PORTAL] Lỗi gửi thông báo: {e}")

        self.send_json({'success': True})

    # ════════════════════════════════════════════════════════
    # LICENSE ENDPOINTS
    # ════════════════════════════════════════════════════════

    def handle_license_verify(self, params):
        """GET /api/license/verify?account=xxx&key=xxx"""
        account = params.get('account', [None])[0]
        key     = params.get('key',     [None])[0]
        if not account or not key:
            self.send_json({"valid": False, "reason": "Thiếu account hoặc key"}, 400)
            return
        conn = get_license_conn()
        try:
            row = conn.execute(
                "SELECT * FROM licenses WHERE account=? AND license_key=?",
                (int(account), key)
            ).fetchone()
            if not row:
                self.send_json({"valid": False, "reason": "Key không tồn tại hoặc sai account"})
                return
            if row['status'] == 'pending':
                self.send_json({"valid": False, "reason": "License chưa được duyệt"})
                return
            if row['status'] == 'revoked':
                self.send_json({"valid": False, "reason": "License đã bị thu hồi. Liên hệ admin."})
                return
            if row['status'] == 'expired':
                self.send_json({"valid": False, "reason": "License đã hết hạn. Vui lòng gia hạn."})
                return
            # Kiểm tra ngày hết hạn
            if row['expires_at']:
                exp = datetime.strptime(row['expires_at'], '%Y-%m-%d')
                if datetime.now() > exp:
                    conn.execute("UPDATE licenses SET status='expired' WHERE id=?", (row['id'],))
                    conn.commit()
                    self.send_json({"valid": False, "reason": "License đã hết hạn"})
                    return
            self.send_json({
                "valid":      True,
                "plan":       row['plan'],
                "name":       row['name'],
                "expires_at": row['expires_at'] or "unlimited",
            })
        finally:
            conn.close()

    def handle_license_register(self):
        """POST /api/license/register"""
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json({"success": False, "error": "JSON không hợp lệ"}, 400)
            return
        name    = data.get('name', '').strip()
        account = data.get('account')
        plan    = data.get('plan', 'ib')
        if not name or not account:
            self.send_json({"success": False, "error": "Thiếu thông tin bắt buộc"}, 400)
            return
        conn = get_license_conn()
        try:
            existing = conn.execute(
                "SELECT id, status FROM licenses WHERE account=?", (int(account),)
            ).fetchone()
            if existing:
                if existing['status'] == 'active':
                    self.send_json({"success": False, "error": "Tài khoản này đã có license đang hoạt động"})
                    return
                elif existing['status'] == 'pending':
                    self.send_json({"success": False, "error": "Đơn đăng ký của bạn đang chờ duyệt"})
                    return
            conn.execute("""
                INSERT INTO licenses (name, telegram, account, broker, plan, ib_link, payment_note, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (name, data.get('telegram',''), int(account), data.get('broker',''),
                  plan, data.get('ib_link',''), data.get('payment_note','')))
            conn.commit()
            try:
                notify_admin_new_reg(name, account, plan, data.get('telegram',''))
            except Exception as e:
                print(f"[LICENSE] TG notify lỗi: {e}")
            self.send_json({"success": True, "message": "Đăng ký thành công, chờ admin duyệt"})
        finally:
            conn.close()

    def handle_license_list(self):
        """GET /api/license/list"""
        conn = get_license_conn()
        try:
            rows = conn.execute("SELECT * FROM licenses ORDER BY id DESC").fetchall()
            self.send_json({"licenses": [dict(r) for r in rows]})
        finally:
            conn.close()

    def handle_license_approve(self):
        """POST /api/license/approve"""
        length = int(self.headers.get('Content-Length', 0))
        data   = json.loads(self.rfile.read(length))
        lid    = data.get('id')
        key    = data.get('license_key')
        exp    = data.get('expires_at') or None
        conn   = get_license_conn()
        try:
            conn.execute("""
                UPDATE licenses SET status='active', license_key=?, expires_at=?,
                note=?, approved_at=datetime('now','localtime'),
                updated_at=datetime('now','localtime') WHERE id=?
            """, (key, exp, data.get('note',''), lid))
            conn.commit()
            row = conn.execute("SELECT * FROM licenses WHERE id=?", (lid,)).fetchone()
            if row:
                try:
                    send_key_to_customer(row, key)
                except Exception as e:
                    print(f"[LICENSE] Gửi key TG lỗi: {e}")
            self.send_json({"success": True})
        finally:
            conn.close()

    def handle_license_revoke(self):
        """POST /api/license/revoke"""
        length = int(self.headers.get('Content-Length', 0))
        data   = json.loads(self.rfile.read(length))
        conn   = get_license_conn()
        try:
            conn.execute("UPDATE licenses SET status='revoked', updated_at=datetime('now','localtime') WHERE id=?",
                         (data.get('id'),))
            conn.commit()
            self.send_json({"success": True})
        finally:
            conn.close()

    def handle_license_reactivate(self):
        """POST /api/license/reactivate"""
        length = int(self.headers.get('Content-Length', 0))
        data   = json.loads(self.rfile.read(length))
        conn   = get_license_conn()
        try:
            conn.execute("UPDATE licenses SET status='active', updated_at=datetime('now','localtime') WHERE id=?",
                         (data.get('id'),))
            conn.commit()
            self.send_json({"success": True})
        finally:
            conn.close()


    def handle_set_chat_id(self):
        """POST /api/license/set_chat_id — signal_bot gọi khi khách /start"""
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json({'error': 'JSON không hợp lệ'}, 400)
            return
        telegram = str(data.get('telegram', '')).replace('@', '').lower().strip()
        chat_id  = data.get('chat_id')
        if not chat_id:
            self.send_json({'error': 'Thiếu chat_id'}, 400)
            return
        conn = get_license_conn()
        try:
            # Lưu vào chat_sessions (luôn lưu dù có username hay không)
            conn.execute(
                "INSERT INTO chat_sessions (chat_id, telegram) VALUES (?, ?)",
                (chat_id, telegram)
            )
            # Nếu có username → update licenses luôn
            updated = 0
            if telegram:
                cur = conn.execute(
                    "UPDATE licenses SET chat_id=? WHERE LOWER(REPLACE(telegram,'@',''))=?",
                    (chat_id, telegram)
                )
                updated = cur.rowcount
            conn.commit()
            print(f"[CHAT_ID] ✅ Lưu chat_id {chat_id} (@{telegram}) | licenses updated: {updated}")
            self.send_json({'success': True, 'updated': updated})
        finally:
            conn.close()

    # ── /api/payments ────────────────────────────────────────
    def handle_payments_list(self):
        """GET /api/payments — danh sách thanh toán từ SePay"""
        conn = get_license_conn()
        try:
            rows = conn.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 200").fetchall()
            self.send_json({"payments": [dict(r) for r in rows]})
        finally:
            conn.close()

    # ── /api/license/renew ───────────────────────────────────
    def handle_license_renew(self):
        """POST /api/license/renew — gia hạn license"""
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json({'error': 'JSON không hợp lệ'}, 400)
            return
        lid        = data.get('id')
        expires_at = data.get('expires_at')
        if not lid or not expires_at:
            self.send_json({'error': 'Thiếu id hoặc expires_at'}, 400)
            return
        conn = get_license_conn()
        try:
            conn.execute("""
                UPDATE licenses SET expires_at=?, status='active',
                updated_at=datetime('now','localtime') WHERE id=?
            """, (expires_at, lid))
            conn.commit()
            # Thông báo cho khách nếu có chat_id
            row = conn.execute("SELECT * FROM licenses WHERE id=?", (lid,)).fetchone()
            if row and row['chat_id'] and BOT_TOKEN:
                try:
                    msg = (f"🔄 License EA của bạn đã được gia hạn!\n\n"
                           f"👤 {row['name']}\n"
                           f"📊 Account: {row['account']}\n"
                           f"📅 Hết hạn mới: {expires_at}\n\n"
                           f"Cảm ơn bạn đã tin tưởng Trading AI! 🙏")
                    payload = json.dumps({
                        "chat_id": row['chat_id'], "text": msg
                    }).encode()
                    req = urllib.request.Request(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        data=payload, headers={'Content-Type': 'application/json'}
                    )
                    urllib.request.urlopen(req, timeout=5)
                    print(f"[RENEW] ✅ Đã notify gia hạn tới {row['name']}")
                except Exception as e:
                    print(f"[RENEW] TG notify lỗi: {e}")
            self.send_json({"success": True})
        finally:
            conn.close()

    # ── /api/telegram/send ───────────────────────────────────
    def handle_telegram_send(self):
        """POST /api/telegram/send — gửi DM tới 1 chat_id hoặc admin"""
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json({'error': 'JSON không hợp lệ'}, 400)
            return
        message  = data.get('message', '').strip()
        is_admin = data.get('admin', False)
        # Nếu admin=True → gửi cho CHAT_ID admin thay vì chat_id khách
        chat_id  = CHAT_ID if is_admin else data.get('chat_id')
        if not chat_id or not message:
            self.send_json({'error': 'Thiếu chat_id hoặc message'}, 400)
            return
        if not BOT_TOKEN:
            self.send_json({'error': 'BOT_TOKEN chưa cấu hình'}, 500)
            return
        try:
            payload = json.dumps({
                "chat_id": chat_id, "text": message, "parse_mode": "Markdown"
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data=payload, headers={'Content-Type': 'application/json'}
            )
            urllib.request.urlopen(req, timeout=8)
            tag = "ADMIN" if is_admin else f"chat_id {chat_id}"
            print(f"[TG SEND] ✅ Gửi tới {tag}")
            self.send_json({"success": True})
        except Exception as e:
            print(f"[TG SEND] Lỗi: {e}")
            self.send_json({"success": False, "error": str(e)})

    # ── /api/payment/sepay ──────────────────────────────────
    def handle_sepay_webhook(self):
        import re, secrets
        from datetime import timedelta
        auth = self.headers.get('Authorization', '')
        if auth != 'Apikey TradingAI-Sepay-2026-SecureKey':
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json({'error': 'JSON khong hop le'}, 400)
            return
        amount      = data.get('transferAmount', 0)
        content     = str(data.get('content', '')).upper()
        transaction = data.get('referenceCode', '')
        print(f'[SEPAY] Nhan: {amount} VND | ND: {content}')
        match = re.search(r'SGREA?\s*[-]?\s*(\d+)', content)
        if not match:
            self.send_json({'success': True, 'note': 'Khong khop don nao'})
            return
        account = int(match.group(1))
        if amount < 700000:
            self.send_json({'success': True, 'note': 'So tien khong du'})
            return
        conn = get_license_conn()
        try:
            # Lưu giao dịch vào bảng payments
            conn.execute(
                "INSERT INTO payments (transaction_id, amount, description, status) VALUES (?, ?, ?, 'paid')",
                (transaction, amount, content)
            )
            conn.commit()
            # Tránh cấp trùng nếu đã active
            existing = conn.execute(
                "SELECT * FROM licenses WHERE account=? AND status='active' AND plan='rental'",
                (account,)
            ).fetchone()
            if existing:
                print(f'[SEPAY] Account {account} da co key active, bo qua')
                self.send_json({'success': True, 'note': 'Da co key active'})
                return

            chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
            def seg(): return ''.join(secrets.choice(chars) for _ in range(4))
            key    = f'SGR-{seg()}-{seg()}-{seg()}'
            expire = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
            note   = f'Auto Sepay|{amount}VND|{transaction}'

            # Tìm chat_id gần nhất từ chat_sessions (trong 60 phút)
            session = conn.execute("""
                SELECT chat_id, telegram FROM chat_sessions
                WHERE id = (SELECT MAX(id) FROM chat_sessions)
            """).fetchone()
            recent_chat_id = session['chat_id'] if session else None
            recent_tg      = session['telegram'] if session else ''
            print(f'[SEPAY] chat_session gan nhat: chat_id={recent_chat_id} @{recent_tg}')

            # Tìm đơn pending
            row = conn.execute(
                "SELECT * FROM licenses WHERE account=? AND status='pending' AND plan='rental'",
                (account,)
            ).fetchone()

            if row:
                # Cập nhật đơn pending → active
                conn.execute("""
                    UPDATE licenses SET status='active', license_key=?, expires_at=?,
                    chat_id=COALESCE(chat_id, ?),
                    note=?, approved_at=datetime('now','localtime'),
                    updated_at=datetime('now','localtime') WHERE id=?
                """, (key, expire, recent_chat_id, note, row['id']))
                conn.commit()
                row_dict = dict(row)
                row_dict['chat_id'] = row_dict.get('chat_id') or recent_chat_id
                print(f'[SEPAY] Cap key {key} cho account {account} (pending→active)')
            else:
                # Tự tạo đơn mới + cấp key
                conn.execute("""
                    INSERT INTO licenses
                    (name, telegram, chat_id, account, broker, plan, status,
                     license_key, expires_at, note,
                     approved_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'rental', 'active', ?, ?, ?,
                            datetime('now','localtime'), datetime('now','localtime'))
                """, (f'Account {account}', recent_tg, recent_chat_id,
                      account, 'Unknown', key, expire, note))
                conn.commit()
                row_dict = {
                    'name': f'Account {account}',
                    'telegram': recent_tg,
                    'chat_id': recent_chat_id,
                    'account': account,
                    'plan': 'rental',
                    'expires_at': expire,
                }
                print(f'[SEPAY] Tu tao don + cap key {key} cho account {account}')

            row_dict['expires_at'] = expire
            try: send_key_to_customer(row_dict, key)
            except Exception as e: print(f'[SEPAY] TG err: {e}')
            try: notify_admin_new_reg(f'[PAID✅] {row_dict.get("name","?")}', account, 'rental', f'{amount:,} VND | {key}')
            except Exception as e: print(f'[SEPAY] notify err: {e}')
            self.send_json({'success': True, 'key': key, 'expires': expire})
        finally:
            conn.close()


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════
if __name__ == '__main__':
    # Khởi tạo license DB
    conn = get_license_conn()
    conn.close()
    print("✅ License DB sẵn sàng")

    port   = 8000
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f"\n✅ API server chạy tại http://localhost:{port}")
    print(f"   GET  /api/summary             — tổng quan tín hiệu")
    print(f"   GET  /api/signals             — danh sách tín hiệu")
    print(f"   GET  /prices                  — giá realtime MT5")
    print(f"   POST /chat                    — AI Agent Chat")
    print(f"   GET  /health                  — kiểm tra hoạt động")
    print(f"   GET  /api/license/verify      — EA kiểm tra license")
    print(f"   GET  /api/license/list        — admin: danh sách license")
    print(f"   GET  /api/payments            — admin: lịch sử thanh toán")
    print(f"   POST /api/license/register    — khách đăng ký")
    print(f"   POST /api/license/approve     — admin duyệt")
    print(f"   POST /api/license/renew       — admin gia hạn")
    print(f"   POST /api/license/revoke      — admin thu hồi")
    print(f"   POST /api/license/reactivate  — admin kích hoạt lại")
    print(f"   POST /api/telegram/send       — admin gửi DM telegram")
    print(f"   POST /api/payment/sepay       — SePay webhook\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⛔ API server đã dừng.")
