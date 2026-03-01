import time
from dotenv import load_dotenv
load_dotenv()
import re
import feedparser
import yfinance as yf
import json
import sqlite3
import urllib.parse
import os
from datetime import datetime, timedelta
from google import genai

# ==========================================
# 1. 設定區 & 路徑處理
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai_client = genai.Client(api_key=GEMINI_API_KEY)

# 💡 自動定位資料庫路徑：確保資料庫檔案 (.db) 永遠放在根目錄
# 不論本程式是在根目錄執行還是子資料夾執行都能正確對準
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 如果本程式在 data_monitor 資料夾內，就往上一層找 db；若在根目錄就直接用
if os.path.basename(BASE_DIR) == 'data_monitor':
    DB_PATH = os.path.join(BASE_DIR, "..", "market_tsmc.db")
else:
    DB_PATH = os.path.join(BASE_DIR, "market_tsmc.db")

# ==========================================
# 2. 功能模組
# ==========================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
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

def ai_analyze_news(title):
    """
    使用 Gemini 2.0 Flash 進行精確的情緒分析
    """
    prompt = f"""
    分析這則台股新聞：'{title}'
    請專注於「台股大盤(整體趨勢)」或「台積電(2330)」的關聯性。
    若無關請 SKIP。
    請嚴格回傳 JSON 格式：
    {{
        "decision": "ANALYZE" 或 "SKIP",
        "stock_id": "大盤請填 '0000'，台積電請填 '2330'",
        "sentiment_score": -1.0(極度悲觀) 到 1.0(極度樂觀),
        "reason": "繁體中文簡短理由"
    }}
    """
    
    # 優先使用你之前測試成功的 Flash 模型
    models_to_try = ["models/gemini-2.0-flash", 
            "models/gemini-flash-latest", 
            "models/gemini-2.5-flash"]
    
    for model_name in models_to_try:
        try:
            response = genai_client.models.generate_content(model=model_name, contents=prompt)
            if response and response.text:
                # 去除 Markdown 標籤
                clean_json = re.sub(r'```json\n?|```', '', response.text).strip()
                return clean_json
        except Exception:
            continue
    return '{"decision": "SKIP"}'

def save_to_db(data, entry, current_price, clean_stock_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO signals (timestamp, stock_id, sentiment_score, title, reason, price_at_msg, link)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), clean_stock_id, 
              data.get("sentiment_score"), entry.title, data.get("reason"), current_price, entry.link))
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success
    except Exception as e:
        print(f"❌ DB 寫入錯誤: {e}")
        return False

# ==========================================
# 3. 主程序
# ==========================================

def start_monitoring():
    init_db()
    print(f"🚀 [{datetime.now().strftime('%H:%M')}] 啟動大盤/台積電數據收集...")
    
    # 聚焦大盤與台積電關鍵字
    keywords = ["台積電", "2330", "TSMC", "加權指數", "大盤", "台指","川普","選舉","聯準會","Fed","戰爭","關稅","貿易戰","制裁",
               "降息","升息","通膨","外資買賣超","爆大量","融資","崩盤","泡沫","台海","兩岸","中東"]
    query_content = f"({' OR '.join(keywords)}) when:12h"
    
    params = {
        'q': query_content,
        'hl': 'zh-TW', 'gl': 'TW', 'ceid': 'TW:zh-Hant'
    }
    rss_url = f"https://news.google.com/rss/search?{urllib.parse.urlencode(params)}"
    feed = feedparser.parse(rss_url)
    
    time_threshold = datetime.now() - timedelta(hours=12)
    count = 0
    
    for entry in feed.entries:
        try:
            pub_date = datetime.fromtimestamp(time.mktime(entry.published_parsed))
            if pub_date < time_threshold: continue
            
            raw_analysis = ai_analyze_news(entry.title)
            data = json.loads(raw_analysis)
            
            if data.get("decision") == "ANALYZE":
                stock_id = data.get("stock_id", "")
                # 判斷抓取哪個報價
                ticker_id = "^TWII" if "0000" in str(stock_id) else "2330.TW"
                
                ticker = yf.Ticker(ticker_id)
                hist = ticker.history(period="1d")
                price = hist['Close'].iloc[-1] if not hist.empty else 0.0
                
                if save_to_db(data, entry, price, stock_id):
                    print(f"✅ 已存入: {entry.title[:20]}... ({stock_id})")
                    count += 1
        except Exception as e:
            continue

    print(f"🏁 任務完成，本次入庫 {count} 筆。")

if __name__ == "__main__":
    start_monitoring()
