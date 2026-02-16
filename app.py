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

# ==========================================
# 1. 環境與路徑初始化 (關鍵修正)
# ==========================================

# 取得目前程式檔案所在的絕對路徑，確保 SQLite 不會迷路
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'stock_robot.db')

def init_db():
    """初始化 SQLite 資料庫 - 鎖定絕對路徑"""
    print(f"📦 正在初始化資料庫，路徑: {DB_PATH}")
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
    print("✅ 資料庫表 user_states 已確認存在")

# 🔥 在 Flask 啟動前強制執行初始化，避開 Gunicorn 進入點問題
init_db()

app = Flask(__name__)

# --- 設定區 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

genai_client = genai.Client(api_key=GEMINI_API_KEY)
line_config = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ==========================================
# 2. 資料庫讀寫邏輯
# ==========================================

def save_user_mode(user_id, mode):
    """儲存使用者的模式選擇"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO user_states (user_id, last_mode) VALUES (?, ?)', (user_id, mode))
    conn.commit()
    conn.close()

def get_user_mode(user_id):
    """讀取使用者的模式選擇，預設為基本面分析"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT last_mode FROM user_states WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else "基本面分析"

# ==========================================
# 3. 客製化分析邏輯 (依模式生成 Prompt)
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
            請依據以下格式回覆：
            * 以 2025 年預估 EPS [數值] 搭配合理本益比 [倍數] 倍，合理股價為 [價格]
            * 以 2026 年預估 EPS [數值] 搭配合理本益比 [倍數] 倍，合理股價為 [價格]
            結論：目前位階(低估/合理/高估)。
            """
        elif mode == "技術面分析":
            prompt = f"""
            你現在是技術分析專家。請分析股票 {company_name}({stock_id}) 的 K 線狀態。
            請條列呈現：支撐位、壓力位、均線狀態(月線/季線)、指標訊號(KDJ/RSI)。
            最後給予短線多空結論。
            """
        elif mode == "籌碼面分析":
            prompt = f"""
            你現在是籌碼分析師。請分析股票 {company_name}({stock_id}) 的法人動向。
            1. 詳述外資與投信近一週買賣趨勢。
            2. 使用 ASCII 符號 (█) 製作簡易的外資/投信力道圖。
            3. 總結籌碼集中度。
            """
        else:
            prompt = f"""
            你現在是專精台股電子產業（PCB 與 AI 供應鏈）的分析師。
            請針對股票 {company_name}({stock_id}) 分析 2026 年基本面、擴產進度與 EPS 成長預測。
            """

        response = genai_client.models.generate_content(
            model="models/gemini-2.0-flash", 
            contents=prompt,
            config=types.GenerateContentConfig(tools=[{'google_search': {}}])
        )
        ai_analysis = response.text if response.text else "無法生成報告。"

    except Exception as e:
        print(f"❌ 錯誤: {e}")
        ai_analysis = f"分析失敗。原因: {type(e).__name__}"

    return f"【{mode}】\n📊 {stock_id} {company_name}\n💰 現價: {price}\n\n{ai_analysis}"

# ==========================================
# 4. LINE Webhook 路由
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
    
    # A. 模式切換邏輯
    modes = ["基本面分析", "估值分析", "技術面分析", "籌碼面分析"]
    if user_msg in modes:
        save_user_mode(user_id, user_msg)
        send_reply(event, f"✅ 已切換至【{user_msg}】模式\n請輸入 4 位數代碼（如：3037）")
        return

    # B. 股票查詢邏輯
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
