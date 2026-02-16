from dotenv import load_dotenv
load_dotenv() # 這行會自動去讀取 .env 檔案裡的變數
import os
import re
from flask import Flask, request, abort
from google import genai
import yfinance as yf
from google.genai import types  # ⬅️ 關鍵就是這一行！
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
)
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

# --- 設定區 (請確保 Secrets 已設定) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
print(f"DEBUG: 抓到的金鑰是: {GEMINI_API_KEY[:5] if GEMINI_API_KEY else '空的！'}") # 加入這行
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET") # 需要從 LINE 後台拿這個新的 Secret

genai_client = genai.Client(api_key=GEMINI_API_KEY)
line_config = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def get_deep_stock_report(stock_id):
    """
    優化版：具備階層式降級與 PCB 產業深度分析邏輯
    """
    print(f"\n🔍 開始為股票 {stock_id} 製作報告...")
    
    if not GEMINI_API_KEY:
        return "系統配置錯誤：找不到 API Key"

    ticker_str = f"{stock_id}.TW"
    stock = yf.Ticker(ticker_str)
    
    try:
        # 1. 抓取基本面數據
        info = stock.info
        price = info.get('currentPrice', 'N/A')
        pe = info.get('trailingPE', 0)
        print(f"📈 抓取成功 - 現價: {price}, PE: {pe}")
        
        # 2. 針對你的興趣優化 Prompt (加入 PCB 與 2026 展望)
        prompt = f"""
        你現在是專精台股電子產業（特別是 PCB 與 AI 供應鏈）的資深分析師。
        請針對股票代碼 {stock_id} 分析 2026 年的趨勢：
        1. 【外資評價】最近三個月前三大外資的目標價與評等。
        2. 【PCB/AI 佈局】若該公司涉及 PCB、載板或 AI 伺服器，請詳述其 2026 擴產進度。
        3. 【未來增長】預估 2026 年 EPS 與增長率 (%)。
        4. 【數據計算】目前 PE 為 {pe:.2f}，請計算 PEG 並判斷位階。
        5. 【總結】給予『成長潛力』或『目前溢價』的結論。
        請用專業、精簡的條列式回覆。
        """

        # 3. 呼叫 Gemini (具備降級與錯誤處理機制)
        print("🚀 正在發送請求給 Gemini API...")
        
        # 定義想要嘗試的模型順序
        models_to_try = [
            "models/gemini-2.0-flash", 
            "models/gemini-flash-latest", 
            "models/gemini-2.5-flash"
        ]
        ai_analysis = ""

        for model_name in models_to_try:
            try:
                response = genai_client.models.generate_content(
                    model=model_name, 
                    contents=prompt,
                    config=types.GenerateContentConfig(
                    tools=[{'google_search': {}}] # 開啟聯網功能
                    )
                )
                if response and response.text:
                    ai_analysis = response.text
                    print(f"✅ 使用 {model_name} 分析成功！")
                    break # 成功就跳出迴圈
            except Exception as inner_e:
                if "429" in str(inner_e):
                    print(f"⚠️ {model_name} 額度滿了，切換下一個模型...")
                    continue
                else:
                    raise inner_e # 其他錯誤(如 400/403)直接拋出

        if not ai_analysis:
            ai_analysis = "⚠️ 目前所有 AI 模型額度皆已耗盡，請一分鐘後再試。"

    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(f"❌ 發生關鍵錯誤：\n{error_msg}")
        
        # 針對常見錯誤給予白話回覆
        if "429" in str(e):
            ai_analysis = "目前查詢人數過多（API 額度耗盡），請等一分鐘後再試試看！"
        elif "403" in str(e):
            ai_analysis = "API Key 權限不足，請檢查 Google AI Studio 設定。"
        else:
            ai_analysis = f"AI 分析暫時失效。原因：{type(e).__name__}"

    return f"📊 {stock_id} 深度報告\n💰 現價: {price}\n📉 PE: {pe:.2f}\n\n{ai_analysis}"

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
    user_msg = event.message.text.strip()
    
    # 如果使用者輸入的是 4 位數字，視為股票查詢
    if re.match(r'^\d{4}$', user_msg):
        report = get_deep_stock_report(user_msg)
        with ApiClient(line_config) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=report)]
                )
            )

if __name__ == "__main__":
    # 讓程式自動抓取雲端環境分配的 PORT，若抓不到則預設 5000
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
