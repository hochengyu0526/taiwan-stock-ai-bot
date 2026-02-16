from dotenv import load_dotenv
load_dotenv()
import os
import re
import sqlite3
from flask import Flask, request, abort
from google import genai
import yfinance as yf
from google.genai import types
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
)
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

# --- 設定區 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

genai_client = genai.Client(api_key=GEMINI_API_KEY)
line_config = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ==========================================
# 1. 資料庫狀態管理 (State Machine)
# ==========================================

def init_db():
    """初始化 SQLite 資料庫"""
    conn = sqlite3.connect('stock_robot.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_states (
            user_id TEXT PRIMARY KEY,
            last_mode TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_user_mode(user_id, mode):
    """儲存使用者的模式選擇"""
    conn = sqlite3.connect('stock_robot.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO user_states (user_id, last_mode) VALUES (?, ?)', (user_id, mode))
    conn.commit()
    conn.close()

def get_user_mode(user_id):
    """讀取使用者的模式選擇，預設為基本面"""
    conn = sqlite3.connect('stock_robot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT last_mode FROM user_states WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else "基本面分析"

# ==========================================
# 2. 客製化分析邏輯 (依模式生成 Prompt)
# ==========================================

def get_custom_report(stock_id, mode):
    """根據模式調用 Gemini 進行特定分析"""
    print(f"\n🎯 模式: {mode} | 目標: {stock_id}")
    
    ticker_str = f"{stock_id}.TW"
    stock = yf.Ticker(ticker_str)
    
    try:
        info = stock.info
        company_name = info.get('longName') or info.get('shortName') or stock_id
        price = info.get('currentPrice', 'N/A')
        pe = info.get('trailingPE', 0)
        
        # 依模式定義專屬 Prompt
        if mode == "估值分析":
            prompt = f"""
            你現在是精算分析師。請分析股票 {company_name}({stock_id}) 的估值。
            請『嚴格』遵守以下格式回覆，不准有贅詞：
            * 以 2025 年預估 EPS [數值] 搭配合理本益比 [倍數] 倍，合理股價為 [價格]
            * 以 2026 年預估 EPS [數值] 搭配合理本益比 [倍數] 倍，合理股價為 [價格]
            結論：目前位階(低估/合理/高估)。
            """
        elif mode == "技術面分析":
            prompt = f"""
            你現在是技術分析專家。請分析股票 {company_name}({stock_id}) 的 K 線狀態。
            請用條列式呈現：
            * 支撐位：[價格]
            * 壓力位：[價格]
            * 均線狀態：(如站上月線、季線趨勢)
            * 指標訊號：(KDJ、RSI 或 MACD 現況)
            結論：短線多空判斷。
            """
        elif mode == "籌碼面分析":
            prompt = f"""
            你現在是籌碼追蹤專家。請分析股票 {company_name}({stock_id}) 的法人動向。
            1. 詳述外資與投信近一週買賣超張數。
            2. 請用以下格式模擬『籌碼力道圖』(ASCII 文字版)：
               外資：██████░░░░ (買/賣力道)
               投信：██░░░░░░░░ (買/賣力道)
            3. 總結：籌碼集中度分析。
            """
        else: # 基本面分析 (你的原始 PCB/AI 邏輯)
            prompt = f"""
            你現在是專精台股電子產業的資深分析師。
            請針對股票 {company_name}({stock_id}) 分析 2026 年基本面：
            1. 【外資評價】目標價與評等。
            2. 【PCB/AI 佈局】2026 擴產或競爭力。
            3. 【未來增長】預估 2026 年 EPS 與增長率 (%)。
            結論：成長潛力。
            """

        # 呼叫 Gemini
        response = genai_client.models.generate_content(
            model="models/gemini-2.0-flash", 
            contents=prompt,
            config=types.GenerateContentConfig(tools=[{'google_search': {}}])
        )
        ai_analysis = response.text if response.text else "無法生成分析，請稍後再試。"

    except Exception as e:
        print(f"❌ 錯誤: {e}")
        ai_analysis = f"分析失敗。可能是代碼 {stock_id} 查無資料或 API 額度已滿。"

    return f"【{mode}】\n📊 {stock_id} {company_name}\n💰 現價: {price}\n\n{ai_analysis}"

# ==========================================
# 3. LINE 訊息處理邏輯
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
    
    # A. 處理選單切換模式 (需與 LINE 圖文選單按鈕文字一致)
    modes = ["基本面分析", "估值分析", "技術面分析", "籌碼面分析"]
    if user_msg in modes:
        save_user_mode(user_id, user_msg)
        reply_text = f"✅ 已切換至【{user_msg}】模式\n請輸入 4 位數股票代碼 (例如: 3037)"
        send_reply(event, reply_text)
        return

    # B. 處理股票查詢
    if re.match(r'^\d{4}$', user_msg):
        current_mode = get_user_mode(user_id)
        # 先回覆一個「處理中」訊息增加使用者體驗
        report = get_custom_report(user_msg, current_mode)
        send_reply(event, report)

def send_reply(event, text):
    """輔助函式：發送 LINE 文字回覆"""
    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=text)]
            )
        )

if __name__ == "__main__":
    init_db() # 初始化資料庫
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
