import threading
import os
import re
import sqlite3
import yfinance as yf
from flask import Flask, request, abort
from google import genai
from google.genai import types
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage, PushMessageRequest
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# --- 初始化與路徑 ---
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
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
genai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

line_config = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 新增：根目錄路由 (解決 Render 掃描與驗證問題) ---
@app.route("/")
def health_check():
    return "Bot is running!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK' # 立刻回傳 OK，避免 LINE 重試

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()
    
    # 模式切換
    modes = ["基本面分析", "估值分析", "技術面分析", "籌碼面分析"]
    if user_msg in modes:
        save_user_mode(user_id, user_msg)
        send_reply(event, f"✅ 已切換至【{user_msg}】模式")
        return

    # 股票分析：使用 Threading 避免逾時
    if re.match(r'^\d{4}$', user_msg):
        mode = get_user_mode(user_id)
        # 啟動背景執行緒
        thread = threading.Thread(target=async_process, args=(user_id, user_msg, mode))
        thread.start()
        return # 這裡不回傳任何東西，讓 callback 直接回傳 200 OK

def async_process(user_id, stock_id, mode):
    report = get_custom_report(stock_id, mode)
    # 使用 Push Message 回傳結果
    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=report)]))

def get_custom_report(stock_id, mode):
    company_name, price, ai_analysis = "", "N/A", "分析生成中..."
    
    # yfinance 抓取邏輯
    try:
        stock = yf.Ticker(f"{stock_id}.TW")
        info = stock.info
        if not info or 'longName' not in info:
            stock = yf.Ticker(f"{stock_id}.TWO")
            info = stock.info
        if info:
            company_name = info.get('longName') or info.get('shortName') or ""
            price = info.get('currentPrice', 'N/A')
    except: pass

    # 修正模型名稱：移除 -latest
    models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"]
    target = f"{company_name}({stock_id})" if company_name else stock_id
    
    for m in models:
        try:
            res = genai_client.models.generate_content(
                model=m, contents=f"分析 {target} 的{mode}，針對 2026 展望。",
                config=types.GenerateContentConfig(tools=[{'google_search': {}}])
            )
            if res.text:
                ai_analysis = res.text
                break
        except: continue

    name_display = f" {company_name}" if company_name else ""
    return f"【{mode}】\n📊 {stock_id}{name_display}\n💰 現價: {price}\n\n{ai_analysis}"

# --- 資料庫與回覆輔助函式 (維持原樣) ---
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
