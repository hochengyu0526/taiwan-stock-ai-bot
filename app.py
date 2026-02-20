import os
import re
import sqlite3
import threading
from flask import Flask, request, abort
from google import genai
from google.genai import types
import yfinance as yf

# LINE Bot SDK v3
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, 
    ReplyMessageRequest, TextMessage, PushMessageRequest
)
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# ==========================================
# 1. 初始化環境
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'stock_robot.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS user_states (user_id TEXT PRIMARY KEY, last_mode TEXT)')
    conn.commit()
    conn.close()

init_db()
app = Flask(__name__)

# --- API 設定 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

genai_client = genai.Client(api_key=GEMINI_API_KEY)
line_config = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ==========================================
# 2. 核心分析邏輯 (使用你清單中的準確名稱)
# ==========================================

def get_custom_report(stock_id, mode):
    company_name, price, ai_analysis = "", "N/A", "AI 分析暫時無法生成"
    
    # yfinance 抓取邏輯 (增加上市上櫃判斷)
    try:
        stock = yf.Ticker(f"{stock_id}.TW")
        info = stock.info
        if not info or 'longName' not in info:
            stock = yf.Ticker(f"{stock_id}.TWO")
            info = stock.info
        if info:
            company_name = info.get('longName') or info.get('shortName') or ""
            price = info.get('currentPrice', 'N/A')
    except Exception as e:
        print(f"⚠️ yfinance 抓取失敗: {e}")

    # 🚀 根據你清單結果修正的模型順序
    # 優先順序：3.0 Flash (最新) -> 2.0 Flash (穩定) -> 1.5 Flash (保底)
    models_to_try = [
        "gemini-3-flash-preview", 
        "gemini-2.0-flash", 
        "gemini-flash-latest" 
    ]

    target_name = f"{company_name}({stock_id})" if company_name else stock_id
    prompt = f"你是專業台股分析師，請搜尋並分析 {target_name} 的{mode}。重點放在 2026 年展望與產業地位。"

    for model_name in models_to_try:
        try:
            print(f"📡 正在調用模型: {model_name}")
            response = genai_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(tools=[{'google_search': {}}])
            )
            if response and response.text:
                ai_analysis = response.text
                print(f"✨ 模型 {model_name} 成功生成分析")
                break
        except Exception as e:
            # 這裡會捕捉 429 額度用盡的錯誤
            print(f"❌ 模型 {model_name} 失敗: {str(e)}")
            continue

    name_display = f" {company_name}" if company_name else ""
    return f"【{mode}】\n📊 {stock_id}{name_display}\n💰 現價: {price}\n\n{ai_analysis}"

# ==========================================
# 3. 非同步與 Webhook 處理
# ==========================================

def async_task(user_id, stock_id, mode):
    """背景執行 AI 分析並主動 Push 結果"""
    try:
        report = get_custom_report(stock_id, mode)
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=report)]
            ))
    except Exception as e:
        print(f"🚨 背景任務崩潰: {e}")

@app.route("/")
def health_check(): return "Bot is live!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK' # 立刻回傳 OK，防止 LINE 自動重試

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()
    
    # 模式切換邏輯
    modes = ["基本面分析", "估值分析", "技術面分析", "籌碼面分析"]
    if user_msg in modes:
        save_user_mode(user_id, user_msg)
        send_reply(event, f"✅ 已切換至【{user_msg}】模式")
        return

    # 股票代碼邏輯
    if re.match(r'^\d{4}$', user_msg):
        mode = get_user_mode(user_id)
        # 1. 立即回覆，防止 LINE Timeout
        send_reply(event, f"【{mode}】\n📊 {user_msg}\n正在搜尋 2026 最新資料與 AI 分析中，請稍候約 15 秒...")
        # 2. 啟動背景執行緒
        threading.Thread(target=async_task, args=(user_id, user_msg, mode)).start()

def save_user_mode(uid, m):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO user_states (user_id, last_mode) VALUES (?, ?)', (uid, m))
    conn.commit(); conn.close()

def get_user_mode(uid):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT last_mode FROM user_states WHERE user_id = ?', (uid,))
    res = cursor.fetchone(); conn.close()
    return res[0] if res else "基本面分析"

def send_reply(event, text):
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=event.reply_token, messages=[TextMessage(text=text)]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
