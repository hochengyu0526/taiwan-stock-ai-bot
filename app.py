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
# 1. 初始化與資料庫邏輯
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'stock_robot.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS user_states (user_id TEXT PRIMARY KEY, last_mode TEXT)')
    conn.commit()
    conn.close()
    print(f"✅ 資料庫已就緒: {DB_PATH}")

init_db()

app = Flask(__name__)

# --- API 設定 (請在 Render 後台 Environment 設定) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

genai_client = genai.Client(api_key=GEMINI_API_KEY)
line_config = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ==========================================
# 2. 核心分析邏輯 (修正模型名稱與強化日誌)
# ==========================================

def get_custom_report(stock_id, mode):
    company_name, price, ai_analysis = "", "N/A", "AI 分析暫時無法生成"
    
    print(f"📡 [開始處理] 股票代碼: {stock_id} | 模式: {mode}")

    # 1. 抓取資料 (優先嘗試台股上市上櫃)
    try:
        stock = yf.Ticker(f"{stock_id}.TW")
        info = stock.info
        if not info or 'longName' not in info:
            stock = yf.Ticker(f"{stock_id}.TWO")
            info = stock.info
        if info:
            company_name = info.get('longName') or info.get('shortName') or ""
            price = info.get('currentPrice', 'N/A')
            print(f"📊 資料抓取成功: {company_name} | 現價: {price}")
    except Exception as e:
        print(f"⚠️ yfinance 抓取異常: {str(e)}")

    # 2. AI 分析：使用你清單中確認的準確名稱
    # 順序：Gemini 3 (預覽版) -> 2.0 Flash (主力) -> Flash Latest (保底)
    models_to_try = [
        "models/gemini-3-flash-preview", 
        "models/gemini-2.0-flash", 
        "models/gemini-flash-latest"
    ]

    target = f"{company_name}({stock_id})" if company_name else stock_id
    # 優化 Prompt：讓 AI 自己去查正確的台股資訊，避免 yfinance 抓到美股價格 (如 1260.0)
    prompt = f"""
    你是專業的台股投資顧問。請搜尋並分析【{target}】的{mode}。
    請注意：
    1. 務必確認這是台灣股市的股票。
    2. 結合 2026 年最新的產業展望與法人預估。
    3. 分析其技術位階或籌碼動向。
    """

    for model_name in models_to_try:
        try:
            print(f"📡 嘗試調用模型: {model_name}")
            response = genai_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(tools=[{'google_search': {}}])
            )
            if response and response.text:
                ai_analysis = response.text
                print(f"✨ 模型 {model_name} 分析成功！")
                break
        except Exception as ai_e:
            # 這裡能幫你在 Logs 抓出 429 (額度滿) 或其他錯誤
            print(f"❌ 模型 {model_name} 失敗原因: {str(ai_e)}")
            continue

    name_display = f" {company_name}" if company_name else ""
    return f"【{mode}】\n📊 {stock_id}{name_display}\n💰 現價: {price}\n\n{ai_analysis}"

# ==========================================
# 3. 非同步背景任務 (解決 Timeout)
# ==========================================

def async_task(user_id, stock_id, mode):
    """背景跑分析，完成後 Push 給使用者"""
    try:
        report = get_custom_report(stock_id, mode)
        with ApiClient(line_config) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=report)]
            ))
        print(f"✅ [Push成功] 使用者: {user_id}")
    except Exception as e:
        print(f"🚨 [背景任務崩潰]: {str(e)}")

# ==========================================
# 4. 路由與訊息處理
# ==========================================

@app.route("/")
def health(): return "Bot is live!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK' # 立刻回傳 OK，防止 LINE 逾時重試

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()
    
    # 選單切換
    modes = ["基本面分析", "估值分析", "技術面分析", "籌碼面分析"]
    if user_msg in modes:
        save_user_mode(user_id, user_msg)
        send_reply(event, f"✅ 已切換至【{user_msg}】模式")
        return

    # 股票代碼 (2308)
    if re.match(r'^\d{4}$', user_msg):
        current_mode = get_user_mode(user_id)
        # 1. 立即回覆，讓使用者知道機器人有在動
        send_reply(event, f"【{current_mode}】\n📊 {user_msg}\n正在分析中，請稍候約 15~30 秒...")
        # 2. 開啟 Thread 背景處理長任務
        threading.Thread(target=async_task, args=(user_id, user_msg, current_mode)).start()

# --- 輔助函式 ---
def save_user_mode(uid, m):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO user_states (user_id, last_mode) VALUES (?, ?)', (uid, m))
    conn.commit(); conn.close()

def get_user_mode(uid):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('SELECT last_mode FROM user_states WHERE user_id = ?', (uid,))
    res = c.fetchone(); conn.close()
    return res[0] if res else "基本面分析"

def send_reply(event, text):
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=event.reply_token, messages=[TextMessage(text=text)]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
