import time
import re
import feedparser
import yfinance as yf
import json
import sqlite3
import os
from google import genai
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage
)

# ==========================================
# 1. 設定區 (從 GitHub Secrets 讀取)
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")

# 初始化客戶端
genai_client = genai.Client(api_key=GEMINI_API_KEY)
line_config = Configuration(access_token=LINE_ACCESS_TOKEN)

# ==========================================
# 2. 功能模組
# ==========================================

def init_db():
    conn = sqlite3.connect('stock_robot.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            stock_id TEXT,
            sentiment_score REAL,
            title TEXT,
            reason TEXT,
            price_at_msg REAL,
            link TEXT UNIQUE
        )
    ''')
    conn.commit()
    conn.close()

def get_stock_price_status(stock_id):
    try:
        ticker = f"{stock_id}.TW"
        stock = yf.Ticker(ticker)
        hist = stock.history(period="5d")
        if hist.empty: return "無法取得股價"
        start_p = hist['Close'].iloc[0]
        end_p = hist['Close'].iloc[-1]
        change_pct = ((end_p - start_p) / start_p) * 100
        if change_pct > 10: return f"⚠️ 股價已反應 ({change_pct:.1f}%)"
        elif change_pct < 2: return f"🔥 股價尚未反應 ({change_pct:.1f}%)"
        else: return f"小幅波動 ({change_pct:.1f}%)"
    except:
        return "股價查詢失敗"

def ai_analyze_news(title):
    prompt = f"分析台股新聞：'{title}'，請以 JSON 回傳：decision(SKIP/ANALYZE), stock_id(4位代碼), sentiment_score(-1到1), reason, lead_indicator。"
    try:
        response = genai_client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return response.text.strip()
    except:
        return '{"decision": "SKIP"}'

def save_to_db(data, entry, current_price):
    try:
        conn = sqlite3.connect('stock_robot.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO signals (timestamp, stock_id, sentiment_score, title, reason, price_at_msg, link)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (time.strftime('%Y-%m-%d %H:%M:%S'), data.get("stock_id"), data.get("sentiment_score"), entry.title, data.get("reason"), current_price, entry.link))
        conn.commit()
        conn.close()
        return True
    except:
        return False

def push_to_line(message):
    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        push_request = PushMessageRequest(to=LINE_USER_ID, messages=[TextMessage(text=message)])
        line_bot_api.push_message(push_request)

# ==========================================
# 3. 主程序 (單次執行)
# ==========================================

def start_monitoring():
    init_db()
    print("🚀 開始掃描新聞...")
    rss_url = "https://news.google.com/rss/search?q=台股+漲價+展望+擴產&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    feed = feedparser.parse(rss_url)
    
    # 只看最新的 5 則新聞
    for entry in feed.entries[:5]:
        raw_analysis = ai_analyze_news(entry.title)
        try:
            clean_json = re.sub(r'```json\n?|```', '', raw_analysis).strip()
            data = json.loads(clean_json)
            
            if data.get("decision") == "ANALYZE" and data.get("stock_id"):
                stock_id = re.search(r'\d{4}', str(data.get("stock_id"))).group()
                
                # 取得當前股價與狀態
                ticker = f"{stock_id}.TW"
                current_price = yf.Ticker(ticker).history(period="1d")['Close'].iloc[-1]
                price_status = get_stock_price_status(stock_id)
                
                # 存檔 (若回傳 True 代表是新新聞，才推播)
                if save_to_db(data, entry, current_price):
                    sentiment_emoji = "📈" if data.get("sentiment_score", 0) > 0 else "📉"
                    report = (
                        f"【🚨 產業領先指標】\n📰 {entry.title}\n\n"
                        f"🎯 標的：{stock_id} ({data.get('sentiment_score')} {sentiment_emoji})\n"
                        f"💰 股價：{current_price:.2f}\n"
                        f"💡 分析：{data.get('reason')}\n"
                        f"📊 位階：{price_status}\n🔗 {entry.link}"
                    )
                    push_to_line(report)
                    print(f"✅ 已處理並推播：{stock_id}")
        except Exception as e:
            print(f"解析失敗: {e}")

if __name__ == "__main__":
    start_monitoring()
