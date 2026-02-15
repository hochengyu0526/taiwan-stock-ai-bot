import time
import re
import feedparser
import yfinance as yf
import json
import sqlite3
import urllib.parse
import requests
import os
from datetime import datetime, timedelta
from google import genai
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage
)

# ==========================================
# 1. 設定區 (從 GitHub Secrets 或 .env 讀取)
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
        if change_pct > 7: return f"⚠️ 股價已反應 ({change_pct:.1f}%)"
        elif change_pct < 1: return f"🔥 股價尚未反應 ({change_pct:.1f}%)"
        else: return f"小幅波動 ({change_pct:.1f}%)"
    except:
        return "股價查詢失敗"

def ai_analyze_news(title):
    """
    優化版：支援多模型降級，優先使用你測試成功的 flash-latest
    """
    prompt = f"""
    分析台股新聞：'{title}'
    若涉及 PCB 載板、AI 伺服器、或 2026 年展望請優先分析。
    請嚴格以 JSON 回傳，不要包含 Markdown 標籤：
    {{
        "decision": "ANALYZE" 或 "SKIP",
        "stock_id": "4位代碼",
        "sentiment_score": -1.0 到 1.0,
        "reason": "簡短分析理由",
        "lead_indicator": "展望重點"
    }}
    """
    # 模型優先順序：使用你剛測試成功的最新穩定版
    models_to_try = ["models/gemini-flash-latest", "models/gemini-2.0-flash", "models/gemini-1.5-flash"]
    
    for model_name in models_to_try:
        try:
            response = genai_client.models.generate_content(model=model_name, contents=prompt)
            if response and response.text:
                # 移除可能存在的 Markdown 區塊
                return re.sub(r'```json\n?|```', '', response.text).strip()
        except Exception as e:
            if "429" in str(e):
                print(f"⚠️ {model_name} 額度滿了，嘗試下一個...")
                continue
    return '{"decision": "SKIP"}'

def save_to_db(data, entry, current_price):
    try:
        conn = sqlite3.connect('stock_robot.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO signals (timestamp, stock_id, sentiment_score, title, reason, price_at_msg, link)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (time.strftime('%Y-%m-%d %H:%M:%S'), data.get("stock_id"), data.get("sentiment_score"), entry.title, data.get("reason"), current_price, entry.link))
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success
    except:
        return False

def push_to_line(message):
    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        push_request = PushMessageRequest(to=LINE_USER_ID, messages=[TextMessage(text=message)])
        line_bot_api.push_message(push_request)
def get_real_url(google_url):
    """
    將 Google News 的冗長連結轉為新聞來源的真實連結
    """
    try:
        # 使用 requests.get 追蹤跳轉，timeout 設短一點避免卡住
        response = requests.get(google_url, timeout=5, allow_redirects=True)
        return response.url
    except:
        return google_url # 失敗就回傳原網址
# ==========================================
# 3. 主程序 (加入時間過濾邏輯)
# ==========================================

def start_monitoring():
    init_db()
    print(f"🚀 [{datetime.now().strftime('%H:%M')}] 啟動『超級監測模式』...")
    
    # 1. 你提供的完整關鍵字清單 (擴充版)
    keywords_list = [
        "PCB", "載板", "ABF", "欣興", "南電", "景碩", "臻鼎", 
        "AI伺服器", "散熱", "水冷", "台積電", "輝達", "NVIDIA",
        "2026展望", "半導體", "機器人", "電源", "美股", "台股", 
        "低軌衛星", "馬斯克", "川普", "聯準會", "降息", "升息", 
        "矽光子", "探針卡", "測試", "台積電設備", "台積電供應鏈", 
        "銅箔", "CCL", "電動車", "AI", "CPO", "網通", "記憶體","關稅",
        "電力"
        
    ]
    
    # 2. 組合搜尋內容：全部使用 OR 邏輯
    query_content = f"({' OR '.join(keywords_list)}) when:12h"
    
    # 3. 安全 URL 編碼：處理括號、空格與中文字
    params = {
        'q': query_content,
        'hl': 'zh-TW',
        'gl': 'TW',
        'ceid': 'TW:zh-Hant'
    }
    rss_url = f"https://news.google.com/rss/search?{urllib.parse.urlencode(params)}"
    
    print(f"📡 搜尋字串編碼完成，範圍包含：{len(keywords_list)} 個關鍵標籤")
    
    feed = feedparser.parse(rss_url)
    print(f"📊 搜尋完成，初篩發現 {len(feed.entries)} 則候選新聞")

    # 4. 嚴格 12 小時過濾器
    time_threshold = datetime.now() - timedelta(hours=12)
    news_count = 0
    
    for entry in feed.entries:
        try:
            pub_date = datetime.fromtimestamp(time.mktime(entry.published_parsed))
            if pub_date < time_threshold:
                continue 
            
            print(f"🔍 正在處理：{entry.title[:25]}...")

            # 5. AI 分析
            raw_analysis = ai_analyze_news(entry.title)
            data = json.loads(raw_analysis)
            
            # 6. 判定與發送
            if data.get("decision") == "ANALYZE" and data.get("stock_id"):
                stock_id = re.search(r'\d{4}', str(data.get("stock_id"))).group()
                
                # 取得週末相容股價
                ticker = f"{stock_id}.TW"
                hist = yf.Ticker(ticker).history(period="5d")
                
                if not hist.empty:
                    current_price = hist['Close'].iloc[-1]
                    price_status = get_stock_price_status(stock_id)
                    
                    # 🔥 獲取真實網址，解決 LINE 反白問題
                    try:
                        real_link = requests.get(entry.link, timeout=5).url
                    except:
                        real_link = entry.link
                    
                    if save_to_db(data, entry, current_price):
                        sentiment_emoji = "📈" if data.get("sentiment_score", 0) > 0 else "📉"
                        report = (
                            f"【🚨 產業領先指標】\n📰 {entry.title}\n\n"
                            f"🎯 標的：{stock_id} ({data.get('sentiment_score')} {sentiment_emoji})\n"
                            f"💰 參考價：{current_price:.2f}\n"
                            f"💡 分析：{data.get('reason')}\n"
                            f"📊 位階：{price_status}\n🔗 {real_link}"
                        )
                        push_to_line(report)
                        print(f"✅ [發送成功] {stock_id}")
                        news_count += 1
                    
        except Exception as e:
            print(f"❌ 處理單則新聞失敗: {str(e)}")

    print(f"🏁 監測結束，本次共推送 {news_count} 則報告。")

if __name__ == "__main__":
    start_monitoring()
