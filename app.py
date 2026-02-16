from dotenv import load_dotenv
load_dotenv()
import os
import re
import sqlite3
from flask import Flask, request, abort
from google import genai
import yfinance as yf
from google.genai import types

# ==========================================
# 1. 環境與路徑初始化 (絕對路徑鎖定)
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'stock_robot.db')

def init_db():
    """確保資料表在 Render 啟動時即存在"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_states (
            user_id TEXT PRIMARY KEY,
            last_mode TEXT
        )
    ''')
    conn.commit()
    conn.close()
    print(f"✅ 資料庫已初始化: {DB_PATH}")

init_db()

app = Flask(__name__)

# --- API 設定 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

# 使用 2026 最新版 genai Client
genai_client = genai.Client(api_key=GEMINI_API_KEY)

from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

line_config = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ==========================================
# 2. 資料庫讀寫邏輯
# ==========================================

def save_user_mode(user_id, mode):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO user_states (user_id, last_mode) VALUES (?, ?)', (user_id, mode))
    conn.commit()
    conn.close()

def get_user_mode(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT last_mode FROM user_states WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else "基本面分析"

# ==========================================
# 3. 核心分析邏輯 (多模型降級版本)
# ==========================================

def get_custom_report(stock_id, mode):
    """具備多模型 Fallback 的深度分析"""
    ticker_str = f"{stock_id}.TW"
    stock = yf.Ticker(ticker_str)
    
    try:
        info = stock.info
        company_name = info.get('longName') or info.get('shortName') or stock_id
        price = info.get('currentPrice', 'N/A')
        
        # 依模式生成 Prompt
        if mode == "估值分析":
            prompt = f"分析 {company_name}({stock_id}) 2025-2026 EPS 預估與合理位階。"
        elif mode == "技術面分析":
            prompt = f"分析 {company_name}({stock_id}) 的支撐壓力位與 KDJ/RSI 指標。"
        elif mode == "籌碼面分析":
            prompt = f"分析 {company_name}({stock_id}) 外資投信動向，並用 ASCII 畫出量能圖。"
        else:
            prompt = f"分析 {company_name}({stock_id}) 2026 年 PCB 與 AI 供應鏈基本面展望。"

        # 🚀 階層式模型清單 (根據你提供的可用清單排序)
        models_to_try = [
            "models/gemini-3-flash-preview",    # 優先使用最強模型
            "models/gemini-3-pro-preview",
            "models/gemini-2.5-flash",
            "models/gemini-2.5-pro",
            "models/gemini-2.0-flash",
            "models/gemini-flash-latest"        # 最終穩定備援
        ]

        ai_analysis = ""
        for model_name in models_to_try:
            try:
                print(f"📡 嘗試使用模型: {model_name}")
                response = genai_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(tools=[{'google_search': {}}])
                )
                if response and response.text:
                    ai_analysis = response.text
                    print(f"✅ {model_name} 分析成功！")
                    break
            except Exception as inner_e:
                print(f"⚠️ {model_name} 失敗: {inner_e}")
                continue # 嘗試下一個模型

        if not ai_analysis:
            ai_analysis = "目前 AI 流量過載，請稍後再試。"

    except Exception as e:
        ai_analysis = f"資料抓取失敗: {e}"

    return f"【{mode}】\n📊 {stock_id} {company_name}\n💰 現價: {price}\n\n{ai_analysis}"

# ==========================================
# 4. LINE Webhook 處理
# ==========================================

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()
    
    modes = ["基本面分析", "估值分析", "技術面分析", "籌碼面分析"]
    if user_msg in modes:
        save_user_mode(user_id, user_msg)
        send_reply(event, f"✅ 已切換至【{user_msg}】模式")
        return

    if re.match(r'^\d{4}$', user_msg):
        current_mode = get_user_mode(user_id)
        report = get_custom_report(user_msg, current_mode)
        send_reply(event, report)

def send_reply(event, text):
    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
