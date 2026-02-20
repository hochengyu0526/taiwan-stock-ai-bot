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
# 1. 環境與路徑初始化
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'stock_robot.db')

def init_db():
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

# 🔥 新增：檢查資料庫狀態的路由
@app.route("/check_db")
def check_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM user_states')
        count = cursor.fetchone()[0]
        conn.close()
        return f"📊 目前資料庫共有 {count} 筆使用者設定資料。"
    except Exception as e:
        return f"❌ 查詢失敗: {str(e)}"

# ==========================================
# 3. 核心分析邏輯 (修正 UnboundLocalError)
# ==========================================

def get_custom_report(stock_id, mode):
    """具備變數安全與多模型備援的分析邏輯"""
    # 🚀 第一步：立刻初始化所有變數，給予預設值 (放在 try 之外)
    company_name = stock_id  
    price = "N/A"
    ai_analysis = "分析暫時無法生成"
    
    ticker_str = f"{stock_id}.TW"
    stock = yf.Ticker(ticker_str)
    
    try:
        # 嘗試抓取 yfinance 資料
        info = stock.info
        if info:
            company_name = info.get('longName') or info.get('shortName') or stock_id
            price = info.get('currentPrice', 'N/A')
        
        # 依模式生成 Prompt
        prompt = f"分析 {company_name}({stock_id}) 的{mode}。請針對 2026 年展望進行分析。"

        # 備援模型清單 (修正名稱格式)
        models_to_try = [
            "gemini-2.0-flash", 
            "gemini-1.5-flash-latest", 
            "gemini-1.5-pro-latest"
        ]

        # 嘗試調用 AI
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
                continue 

    except Exception as e:
        print(f"❌ yfinance 抓取發生問題: {e}")
        ai_analysis = f"資料抓取失敗，請確認代碼 {stock_id} 是否正確。"

    # 🚀 第二步：現在 return 絕對不會出錯，因為所有變數在最開頭都已經定義過值了
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

