import os
import sys
import json
import logging
import warnings
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn
import requests
import praw
from dotenv import load_dotenv
from google_play_scraper import Sort, reviews as play_reviews_fetch

# Suppress warnings
warnings.simplefilter("ignore", category=UserWarning)
load_dotenv()

app = FastAPI(title="Blinkit Growth Discovery App")
logging.getLogger("prawcore").setLevel(logging.ERROR)

CONFIG = {
    "APP_STORE_ID": "960335206",
    "PLAY_STORE_PACKAGE_ID": "com.grofers.customerapp",
    "REDDIT_CLIENT_ID": os.environ.get("REDDIT_CLIENT_ID", ""),
    "REDDIT_CLIENT_SECRET": os.environ.get("REDDIT_CLIENT_SECRET", ""),
    "USER_AGENT": "windows:com.blinkitdiscovery:v1.0.0 (by /u/DueTeacher8363)",
    "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", "")
}

# Persistent Shared Scraped Cache (shared dashboard context)
# Netlify/Lambda functions are read-only except for /tmp
CACHE_FILE = "/tmp/state_cache.json" if os.path.exists("/tmp") else "state_cache.json"

def fetch_from_rss(feed_url: str, keyword: str = "blinkit") -> list:
    import xml.etree.ElementTree as ET
    from bs4 import BeautifulSoup
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(feed_url, headers=headers, timeout=10)
        if response.status_code != 200:
            return []
        root = ET.fromstring(response.content)
        items = []
        for item in root.findall(".//item"):
            title = item.find("title").text if item.find("title") is not None else ""
            link = item.find("link").text if item.find("link") is not None else ""
            desc = (
                item.find("description").text
                if item.find("description") is not None
                else ""
            )
            if keyword.lower() in title.lower() or keyword.lower() in desc.lower():
                items.append({
                    "source": "Quick-Commerce RSS",
                    "title": title,
                    "url": link,
                    "snippet": BeautifulSoup(desc, "html.parser").get_text()[:200] if desc else ""
                })
        return items
    except Exception as e:
        print(f"RSS Scrape Error: {e}", file=sys.stderr)
        return []

def load_cache():
    default_cache = {
        "last_scraped": None,
        "reddit_posts": [],
        "app_store_reviews": [],
        "play_store_reviews": [],
        "rss_news": []
    }
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "play_store_reviews" not in data:
                    data["play_store_reviews"] = []
                if "rss_news" not in data:
                    data["rss_news"] = []
                return data
        except Exception:
            return default_cache
    return default_cache

def save_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(shared_cache, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving cache: {e}", file=sys.stderr)

shared_cache = load_cache()

# Pydantic Schemas
class ChatRequest(BaseModel):
    message: str
    history: List[Dict[str, str]]

class ScrapeConfig(BaseModel):
    mode: str  # "raw" or "ai"

# Scraper Core Logic
def run_reddit_scrape(client_id: str, client_secret: str, user_agent: str) -> list:
    queries = ["blinkit groceries", "zepto instamart habit", "quick commerce category"]
    posts = []
    if not client_id or not client_secret:
        return posts
    try:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent
        )
        for query in queries:
            # Fetch 20 posts per query for a total of ~60 posts
            results = reddit.subreddit("all").search(query, limit=20, sort="relevance")
            for post in results:
                posts.append({
                    "subreddit": post.subreddit.display_name,
                    "title": post.title,
                    "text": post.selftext[:1000]
                })
    except Exception as e:
        print(f"Reddit Scrape Error: {e}", file=sys.stderr)
    return posts

def run_app_store_scrape(app_id: str) -> list:
    url = f"https://itunes.apple.com/in/rss/customerreviews/id={app_id}/sortBy=mostRecent/json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    reviews = []
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()
        entries = data.get("feed", {}).get("entry", [])
        if entries is None:
            entries = []
        elif isinstance(entries, dict):
            entries = [entries]
            
        for entry in entries:
            if "content" in entry:
                reviews.append({
                    "rating": entry.get("im:rating", {}).get("label", "N/A"),
                    "title": entry.get("title", {}).get("label", ""),
                    "text": entry.get("content", {}).get("label", ""),
                    "author": entry.get("author", {}).get("name", {}).get("label", "")
                })
    except Exception as e:
        print(f"App Store Scrape Error: {e}", file=sys.stderr)
    return reviews

def run_play_store_scrape(package_id: str) -> list:
    try:
        result, _ = play_reviews_fetch(
            package_id,
            lang="en",
            country="in",
            sort=Sort.NEWEST,
            count=50
        )
        clean_reviews = []
        for review in result:
            clean_reviews.append({
                "rating": str(review.get("score", "N/A")),
                "title": "Play Store Review",
                "text": review.get("content", ""),
                "author": review.get("userName", "")
            })
        return clean_reviews
    except Exception as e:
        print(f"Play Store Scrape Error: {e}", file=sys.stderr)
        return []

def call_gemini_api(history: List[Dict[str, str]], context: dict) -> str:
    gemini_key = CONFIG["GEMINI_API_KEY"]
    if not gemini_key:
        return "Gemini API Key is missing. Please set the GEMINI_API_KEY env variable."
        
    combined_context = json.dumps(context, indent=2)
    
    contents = []
    system_instruction = f"""
You are an expert PM Growth Analyst for Blinkit. Your goal is to objectively analyze the customer feedback (App Store reviews, Play Store reviews, and Reddit posts) to uncover entry barriers, shopping habits, customer frustrations, and friction points regarding quick commerce category discovery.

Here is the live scraped customer feedback context:
{combined_context}

### INTERACTION INSTRUCTIONS
1. **Greeting Handling**: If the user's message is a simple greeting (such as "hello", "hi", "hey", "good morning", etc.), respond politely and warmly. Introduce yourself briefly as the Blinkit AI PM Growth Assistant, and invite them to ask specific questions about the scraped feedback data, shopping habits, or customer frustrations. Do NOT dump the full analysis report for a simple greeting.
2. **Analysis Queries**: If the user asks a product discovery, analysis, or growth question, provide a detailed, highly structured response structured as follows:
   - **Executive Summary**: Begin your response with a concise **5-6 line summary paragraph** capturing the core findings and overall conclusion.
   - **Detailed Analysis**: Follow the summary with the detailed, structured explanation covering the sections below.

### ANALYSIS QUESTIONS TO COVER (for analysis queries)
- **Habitual Behaviors**: Why do users repeatedly buy from the same categories? What specific role do shopping habits and routine loops play in their behavior?
- **Discovery Barriers**: What prevents users from exploring new categories? How do users discover products today?
- **Information Needs**: What information, guarantees, or assurances do users need before they feel comfortable trying a new category?
- **Recurring Frustrations & Unmet Needs**: What frustrations emerge repeatedly in the data? What unmet needs emerge consistently across these discussions?
- **User Segmentation**: Which user segments (e.g., convenience-focused, price-sensitive, quality-first) appear more likely to experiment vs stick to habits?

### METHODOLOGY DEMONSTRATION (for analysis queries)
In your synthesis, you must explicitly highlight and demonstrate:
- **Workflow & Data Gathering**: How this engine gathers, parses, and formats data from Reddit, Google Play Store, and Apple App Store.
- **Theme Identification**: How recurring customer themes are identified and grouped from the raw feedback.
- **Insight Generation**: The process of converting raw user complaints/discussions into actionable PM insights.
- **Quality & Validation**: How the quality, relevance, and authenticity of these insights are validated (e.g., cross-referencing multiple platforms, checking sample sizes, looking for recurring patterns).

Structure the response professionally with clear headings, bullet points, and bold key terms. Focus entirely on what the user data itself indicates.
"""

    for i, turn in enumerate(history):
        role = "user" if turn["role"] == "user" else "model"
        text = turn["text"]
        if i == 0 and role == "user":
            text = f"{system_instruction}\n\nUser Query: {text}"
            
        contents.append({
            "role": role,
            "parts": [{"text": text}]
        })

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.4
        }
    }
    
    try:
        res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
        res.raise_for_status()
        res_data = res.json()
        return res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        return f"Gemini API Call failed: {e}"

# FastAPI Web Routes
@app.post("/api/scrape")
async def trigger_scrape(cfg: ScrapeConfig):
    # Sync environment variables
    CONFIG["REDDIT_CLIENT_ID"] = os.environ.get("REDDIT_CLIENT_ID", CONFIG["REDDIT_CLIENT_ID"])
    CONFIG["REDDIT_CLIENT_SECRET"] = os.environ.get("REDDIT_CLIENT_SECRET", CONFIG["REDDIT_CLIENT_SECRET"])
    CONFIG["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", CONFIG["GEMINI_API_KEY"])

    reddit_data = run_reddit_scrape(
        CONFIG["REDDIT_CLIENT_ID"],
        CONFIG["REDDIT_CLIENT_SECRET"],
        CONFIG["USER_AGENT"]
    )
    app_store_data = run_app_store_scrape(CONFIG["APP_STORE_ID"])
    play_store_data = run_play_store_scrape(CONFIG["PLAY_STORE_PACKAGE_ID"])
    rss_data = fetch_from_rss("https://news.google.com/rss/search?q=blinkit", keyword="blinkit")
    
    import datetime
    shared_cache["last_scraped"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    shared_cache["reddit_posts"] = reddit_data
    shared_cache["app_store_reviews"] = app_store_data
    shared_cache["play_store_reviews"] = play_store_data
    shared_cache["rss_news"] = rss_data
    save_cache()
    
    auto_analysis = None
    if cfg.mode == "ai":
        context = {
            "reddit_posts": reddit_data,
            "app_store_reviews": app_store_data,
            "play_store_reviews": play_store_data,
            "rss_news": rss_data
        }
        initial_history = [{
            "role": "user",
            "text": "Perform a full PM synthesis covering Category Barriers, Habit Loops, and MVP Validation."
        }]
        auto_analysis = call_gemini_api(initial_history, context)
    
    return {
        "status": "Success",
        "timestamp": shared_cache["last_scraped"],
        "reddit_count": len(reddit_data),
        "app_store_count": len(app_store_data),
        "play_store_count": len(play_store_data),
        "rss_count": len(rss_data),
        "auto_analysis": auto_analysis
    }

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    context = {
        "reddit_posts": shared_cache["reddit_posts"],
        "app_store_reviews": shared_cache["app_store_reviews"],
        "play_store_reviews": shared_cache["play_store_reviews"],
        "rss_news": shared_cache["rss_news"]
    }
    # Append the user's incoming message to the sent history
    full_history = req.history + [{"role": "user", "text": req.message}]
    reply = call_gemini_api(full_history, context)
    return {"reply": reply}

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    # Load initial data if empty
    if not shared_cache["reddit_posts"] and not shared_cache["app_store_reviews"] and not shared_cache["play_store_reviews"] and not shared_cache["rss_news"]:
        shared_cache["reddit_posts"] = run_reddit_scrape(
            CONFIG["REDDIT_CLIENT_ID"],
            CONFIG["REDDIT_CLIENT_SECRET"],
            CONFIG["USER_AGENT"]
        )
        shared_cache["app_store_reviews"] = run_app_store_scrape(CONFIG["APP_STORE_ID"])
        shared_cache["play_store_reviews"] = run_play_store_scrape(CONFIG["PLAY_STORE_PACKAGE_ID"])
        shared_cache["rss_news"] = fetch_from_rss("https://news.google.com/rss/search?q=blinkit", keyword="blinkit")
        import datetime
        shared_cache["last_scraped"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_cache()

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Blinkit Growth Discovery Engine</title>
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <!-- Markdown Parser -->
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <style>
            :root {{
                --bg-primary: #ffffff;
                --bg-secondary: #f9fafb;
                --bg-card: #ffffff;
                --accent: #000000;
                --accent-hover: #1f2937;
                --text-main: #111827;
                --text-muted: #6b7280;
                --border-color: #e5e7eb;
                --chat-user-bg: #1f2937;
                --chat-bot-bg: #f3f4f6;
            }}
            * {{
                box-sizing: border-box;
                margin: 0;
                padding: 0;
                font-family: 'Plus Jakarta Sans', sans-serif;
            }}
            body {{
                background-color: var(--bg-primary);
                color: var(--text-main);
                height: 100vh;
                display: flex;
                flex-direction: column;
                overflow: hidden;
            }}
            header {{
                padding: 20px 40px;
                background-color: var(--bg-primary);
                border-bottom: 1px solid var(--border-color);
                display: flex;
                justify-content: space-between;
                align-items: center;
                flex-shrink: 0;
            }}
            .logo-area h1 {{
                font-size: 1.5rem;
                font-weight: 700;
                letter-spacing: -0.5px;
                color: #000000;
            }}
            .logo-area p {{
                font-size: 0.8rem;
                color: var(--text-muted);
                margin-top: 2px;
            }}
            .app-layout {{
                flex: 1;
                display: flex;
                overflow: hidden;
            }}
            
            /* Left Side: Scraper Control (Compact) */
            .sidebar-control {{
                width: 25%;
                border-right: 1px solid var(--border-color);
                background-color: var(--bg-secondary);
                display: flex;
                flex-direction: column;
                padding: 30px;
                gap: 20px;
                justify-content: flex-start;
            }}
            
            /* Right Side: Main Work Area */
            .main-content {{
                flex: 1;
                display: flex;
                flex-direction: column;
                overflow: hidden;
                background-color: var(--bg-primary);
            }}
            .tabs-header {{
                display: flex;
                background-color: var(--bg-primary);
                border-bottom: 1px solid var(--border-color);
                padding: 0 40px;
            }}
            .tab-btn {{
                padding: 20px 30px;
                background: none;
                border: none;
                color: var(--text-muted);
                font-weight: 600;
                font-size: 0.95rem;
                cursor: pointer;
                border-bottom: 2px solid transparent;
                transition: all 0.2s;
            }}
            .tab-btn.active {{
                color: var(--accent);
                border-bottom-color: var(--accent);
            }}
            .tab-content {{
                flex: 1;
                display: none;
                flex-direction: column;
                overflow: hidden;
                padding: 40px;
            }}
            .tab-content.active {{
                display: flex;
            }}

            .panel-card {{
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 24px;
                box-shadow: 0 1px 3px rgba(0,0,0,0.05);
            }}
            .panel-card h2 {{
                font-size: 0.95rem;
                font-weight: 700;
                margin-bottom: 14px;
                color: #000;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }}
            .form-group {{
                margin-bottom: 16px;
            }}
            .form-group label {{
                display: block;
                font-size: 0.75rem;
                font-weight: 600;
                color: var(--text-main);
                margin-bottom: 6px;
            }}
            select {{
                width: 100%;
                padding: 11px 14px;
                background: #fff;
                border: 1px solid var(--border-color);
                border-radius: 6px;
                color: var(--text-main);
                font-size: 0.85rem;
                outline: none;
            }}
            select:focus {{
                border-color: var(--accent);
            }}
            .btn-accent {{
                background-color: var(--accent);
                color: #ffffff;
                font-weight: 600;
                padding: 12px 20px;
                border: 1px solid var(--accent);
                border-radius: 6px;
                cursor: pointer;
                font-size: 0.88rem;
                width: 100%;
                transition: all 0.2s;
                text-align: center;
            }}
            .btn-accent:hover {{
                background-color: var(--accent-hover);
                border-color: var(--accent-hover);
            }}

            /* Data Feed Grid Layout */
            .feed-grid {{
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 30px;
                height: 100%;
                overflow: hidden;
            }}
            .feed-column {{
                display: flex;
                flex-direction: column;
                overflow: hidden;
            }}
            .feed-column h3 {{
                font-size: 1.05rem;
                font-weight: 700;
                margin-bottom: 16px;
                color: #000;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }}
            .feed-column h3 span {{
                font-size: 0.8rem;
                padding: 3px 8px;
                background: var(--border-color);
                border-radius: 20px;
                color: var(--text-main);
            }}
            .scrollable-list {{
                flex: 1;
                overflow-y: auto;
                display: flex;
                flex-direction: column;
                gap: 16px;
                padding-right: 8px;
            }}
            .card {{
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 8px;
                padding: 20px;
                font-size: 0.88rem;
                line-height: 1.6;
                box-shadow: 0 1px 2px rgba(0,0,0,0.02);
                transition: border-color 0.2s;
            }}
            .card:hover {{
                border-color: #000000;
            }}
            .card-meta {{
                display: flex;
                justify-content: space-between;
                font-size: 0.72rem;
                color: var(--text-muted);
                font-weight: 700;
                margin-bottom: 8px;
            }}
            .rating-badge {{
                background: #f3f4f6;
                color: #000;
                padding: 2px 6px;
                border-radius: 4px;
                border: 1px solid var(--border-color);
            }}
            .card-title {{
                font-weight: 700;
                color: #000;
                margin-bottom: 6px;
                font-size: 0.95rem;
            }}

            /* Chat Layout */
            .chat-messages {{
                flex: 1;
                overflow-y: auto;
                display: flex;
                flex-direction: column;
                gap: 20px;
                padding-bottom: 20px;
                padding-right: 8px;
            }}
            .msg {{
                max-width: 80%;
                padding: 18px 22px;
                border-radius: 12px;
                line-height: 1.65;
                font-size: 0.92rem;
                animation: fadeIn 0.25s ease;
            }}
            .msg.user {{
                background-color: var(--chat-user-bg);
                color: #ffffff;
                align-self: flex-end;
                border-bottom-right-radius: 2px;
                font-weight: 500;
            }}
            .msg.bot {{
                background-color: var(--chat-bot-bg);
                border: 1px solid var(--border-color);
                color: var(--text-main);
                align-self: flex-start;
                border-bottom-left-radius: 2px;
            }}
            /* Markdown Styling inside bot messages */
            .msg.bot p {{ margin-bottom: 12px; }}
            .msg.bot ul, .msg.bot ol {{ margin-left: 20px; margin-bottom: 12px; }}
            .msg.bot li {{ margin-bottom: 6px; }}
            .msg.bot h3 {{ margin-top: 15px; margin-bottom: 8px; color: #000; font-size: 1.1rem; }}
            .msg.bot strong {{ color: #000; font-weight: 700; }}

            .chat-input-area {{
                display: flex;
                gap: 15px;
                align-items: center;
                background-color: var(--bg-secondary);
                border: 1px solid var(--border-color);
                padding: 16px 20px;
                border-radius: 12px;
                flex-shrink: 0;
            }}
            .chat-input-area input {{
                flex: 1;
                padding: 12px 16px;
                font-size: 0.9rem;
                background: #fff;
                border: 1px solid var(--border-color);
                color: var(--text-main);
            }}
            .chat-input-area button {{
                width: auto;
                padding: 12px 24px;
            }}
            .suggestions {{
                display: flex;
                gap: 8px;
                flex-wrap: wrap;
                margin-bottom: 12px;
            }}
            .sug-pill {{
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                color: var(--text-muted);
                padding: 6px 12px;
                border-radius: 20px;
                font-size: 0.75rem;
                cursor: pointer;
                transition: all 0.2s;
            }}
            .sug-pill:hover {{
                border-color: #000000;
                color: #000;
            }}

            @keyframes fadeIn {{
                from {{ opacity: 0; transform: translateY(6px); }}
                to {{ opacity: 1; transform: translateY(0); }}
            }}
            ::-webkit-scrollbar {{ width: 5px; }}
            ::-webkit-scrollbar-thumb {{ background: rgba(0,0,0,0.1); border-radius: 10px; }}
        </style>
    </head>
    <body>
        <header>
            <div class="logo-area">
                <h1>Blinkit Growth Discovery Engine</h1>
                <p>Real-Time Category Discovery Analytics & Conversational MVP</p>
            </div>
            <div style="font-size: 0.85rem; color: var(--text-muted);">
                Last Refresh: <span id="lastScrapeTime" style="color: #000; font-weight: 700;">{shared_cache["last_scraped"] or "Never"}</span>
            </div>
        </header>

        <div class="app-layout">
            <!-- Left Side Controls -->
            <div class="sidebar-control">
                <div class="panel-card">
                    <h2>Scraper Control</h2>
                    <div class="form-group" style="margin-top: 10px;">
                        <label>Extraction Mode</label>
                        <select id="scrapeMode">
                            <option value="raw">Fetch & Ingest Data (Raw View Only)</option>
                            <option value="ai">Fetch & Synthesize Insights (AI Analysis)</option>
                        </select>
                    </div>
                    <button class="btn-accent" id="scrapeBtn" onclick="triggerScrape()">Trigger Live Scrape</button>
                    <p style="font-size:0.7rem; color:var(--text-muted); margin-top:14px; line-height:1.4;">
                        *Server configuration loaded securely from `.env` file.
                    </p>
                </div>
            </div>

            <!-- Right Side Dashboard & Chat -->
            <div class="main-content">
                <div class="tabs-header">
                    <button class="tab-btn active" id="btn-chat" onclick="switchTab('tab-chat')">AI Chat Assistant</button>
                    <button class="tab-btn" id="btn-feed" onclick="switchTab('tab-feed')">Ingested Live Feed</button>
                </div>

                <!-- Tab 1: Chat Assistant -->
                <div class="tab-content active" id="tab-chat">
                    <div class="chat-messages" id="chatBox">
                        <!-- Messages render here -->
                    </div>

                    <div class="suggestions">
                        <div class="sug-pill" onclick="sendSuggestion('Why do users buy repeatedly from the same categories?')">Habit Drivers</div>
                        <div class="sug-pill" onclick="sendSuggestion('What prevents users from exploring new categories?')">Discovery Barriers</div>
                        <div class="sug-pill" onclick="sendSuggestion('Show me typical customer frustrations from the live feed.')">Customer Frustrations</div>
                        <div class="sug-pill" onclick="sendSuggestion('Provide a clean summary of actionable customer feedback.')">Actionable Insights</div>
                    </div>

                    <div class="chat-input-area">
                        <input type="text" id="chatInput" placeholder="Ask about growth, categories, or user frustrations..." onkeydown="if(event.key === 'Enter') sendChatMessage()">
                        <button class="btn-accent" onclick="sendChatMessage()">Send</button>
                    </div>
                </div>

                <!-- Tab 2: Raw feeds display -->
                <div class="tab-content" id="tab-feed">
                    <div class="feed-grid">
                        <div class="feed-column">
                            <h3>Mobile App Reviews <span id="appCountLabel">{len(shared_cache["app_store_reviews"]) + len(shared_cache["play_store_reviews"])}</span></h3>
                            <div class="scrollable-list" id="appFeedList">
                                <!-- Reviews cards -->
                            </div>
                        </div>
                        
                        <div class="feed-column">
                            <h3>Discussions & Forums <span id="redditCountLabel">{len(shared_cache["reddit_posts"]) + len(shared_cache["rss_news"])}</span></h3>
                            <div class="scrollable-list" id="redditFeedList">
                                <!-- Reddit and RSS cards -->
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <script>
            const redditPosts = {json.dumps(shared_cache["reddit_posts"])};
            const appStoreReviews = {json.dumps(shared_cache["app_store_reviews"])};
            const playStoreReviews = {json.dumps(shared_cache["play_store_reviews"])};
            const rssNews = {json.dumps(shared_cache["rss_news"])};
            
            // Client-Side Session Chat History
            let chatHistory = [];

            function switchTab(tabId) {{
                document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
                
                const btnId = tabId === 'tab-chat' ? 'btn-chat' : 'btn-feed';
                document.getElementById(btnId).classList.add('active');
                document.getElementById(tabId).classList.add('active');
            }}

            function renderFeeds() {{
                const appList = document.getElementById('appFeedList');
                const redditList = document.getElementById('redditFeedList');
                
                appList.innerHTML = '';
                redditList.innerHTML = '';

                appStoreReviews.forEach(rev => {{
                    const div = document.createElement('div');
                    div.className = 'card';
                    div.innerHTML = `<div class="card-meta"><span>iOS App Store</span><span class="rating-badge">${{rev.rating}}★</span></div>
                                     <div style="font-size: 0.72rem; color: var(--text-muted); margin-bottom: 4px;">By: ${{rev.author}}</div>
                                     <div class="card-title">${{rev.title}}</div>
                                     <div style="color:var(--text-muted);">${{rev.text}}</div>`;
                    appList.appendChild(div);
                }});

                playStoreReviews.forEach(rev => {{
                    const div = document.createElement('div');
                    div.className = 'card';
                    div.innerHTML = `<div class="card-meta"><span>Android Play Store</span><span class="rating-badge">${{rev.rating}}★</span></div>
                                     <div style="font-size: 0.72rem; color: var(--text-muted); margin-bottom: 4px;">By: ${{rev.author}}</div>
                                     <div class="card-title">${{rev.title}}</div>
                                     <div style="color:var(--text-muted);">${{rev.text}}</div>`;
                    appList.appendChild(div);
                }});

                redditPosts.forEach(post => {{
                    const div = document.createElement('div');
                    div.className = 'card';
                    div.innerHTML = `<div class="card-meta"><span>Reddit (r/${{post.subreddit}})</span></div>
                                     <div class="card-title">${{post.title}}</div>
                                     <div style="color:var(--text-muted);">${{post.text}}</div>`;
                    redditList.appendChild(div);
                }});

                rssNews.forEach(news => {{
                    const div = document.createElement('div');
                    div.className = 'card';
                    div.innerHTML = `<div class="card-meta"><span>Quick-Commerce RSS</span></div>
                                     <div class="card-title"><a href="${{news.url}}" target="_blank" style="color:inherit; text-decoration:underline;">${{news.title}}</a></div>
                                     <div style="color:var(--text-muted);">${{news.snippet}}</div>`;
                    redditList.appendChild(div);
                }});

                if (appStoreReviews.length === 0 && playStoreReviews.length === 0) {{
                    appList.innerHTML = '<div style="color:var(--text-muted); padding:20px; text-align:center;">No App reviews loaded. Click \"Trigger Live Scrape\" to fetch data.</div>';
                }}
                if (redditPosts.length === 0 && rssNews.length === 0) {{
                    redditList.innerHTML = '<div style="color:var(--text-muted); padding:20px; text-align:center;">No discussions loaded. Click \"Trigger Live Scrape\" to fetch data.</div>';
                }}
            }}

            const chatBox = document.getElementById('chatBox');
            
            function appendMessage(text, isUser) {{
                const div = document.createElement('div');
                div.className = 'msg ' + (isUser ? 'user' : 'bot');
                if (!isUser) {{
                    div.innerHTML = marked.parse(text);
                }} else {{
                    div.innerText = text;
                }}
                chatBox.appendChild(div);
                chatBox.scrollTop = chatBox.scrollHeight;
            }}

            function initializeChat() {{
                chatBox.innerHTML = '';
                
                // Read auto analysis session seeds from sessionStorage if they exist (survives scrape refreshes)
                const savedHistory = sessionStorage.getItem("blinkit_chat_history");
                if (savedHistory) {{
                    chatHistory = JSON.parse(savedHistory);
                }}
                
                if (chatHistory.length > 0) {{
                    chatHistory.forEach(turn => {{
                        appendMessage(turn.text, turn.role === 'user');
                    }});
                }} else {{
                    appendMessage("Hello! I am your AI PM Growth Assistant for Blinkit.\\n\\nI have loaded the live App Store reviews and Reddit posts from your local scraper console.\\n\\nAsk me any product discovery questions, or use the quick suggestions below to analyze user growth habits!", false);
                }}
            }}

            async function triggerScrape() {{
                const btn = document.getElementById('scrapeBtn');
                const originalText = btn.innerText;
                const mode = document.getElementById('scrapeMode').value;
                btn.innerText = 'Scraping Live Feeds...';
                btn.disabled = true;

                try {{
                    const res = await fetch('/api/scrape', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{
                            mode: mode
                        }})
                    }});
                    const data = await res.json();
                    
                    if (data.status === 'Success') {{
                        // Seed client-side chat session storage if AI analysis was compiled
                        if (data.auto_analysis) {{
                            const initialTurn = [
                                {{ role: 'user', text: 'Perform a full PM synthesis covering Category Barriers, Habit Loops, and MVP Validation.' }},
                                {{ role: 'model', text: data.auto_analysis }}
                            ];
                            sessionStorage.setItem("blinkit_chat_history", JSON.stringify(initialTurn));
                        }} else {{
                            sessionStorage.removeItem("blinkit_chat_history");
                        }}
                        location.reload();
                    }}
                }} catch (e) {{
                    alert('Error running scraper process.');
                }} finally {{
                    btn.innerText = originalText;
                    btn.disabled = false;
                }}
            }}

            async function sendChatMessage() {{
                const input = document.getElementById('chatInput');
                const text = input.value.trim();
                if (!text) return;
                
                appendMessage(text, true);
                input.value = '';

                const loadDiv = document.createElement('div');
                loadDiv.className = 'msg bot';
                loadDiv.innerText = 'Analyzing live feeds & generating growth recommendation...';
                chatBox.appendChild(loadDiv);
                chatBox.scrollTop = chatBox.scrollHeight;

                try {{
                    const res = await fetch('/api/chat', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ 
                            message: text,
                            history: chatHistory 
                        }})
                    }});
                    const data = await res.json();
                    loadDiv.remove();
                    
                    // Update client history session memory
                    chatHistory.push({{ role: 'user', text: text }});
                    chatHistory.push({{ role: 'model', text: data.reply }});
                    sessionStorage.setItem("blinkit_chat_history", JSON.stringify(chatHistory));

                    appendMessage(data.reply, false);
                }} catch (e) {{
                    loadDiv.innerText = 'Connection error. Make sure FastAPI server is running.';
                }}
            }}

            function sendSuggestion(text) {{
                document.getElementById('chatInput').value = text;
                sendChatMessage();
            }}

            // Run initializers
            renderFeeds();
            initializeChat();
        </script>
    </body>
    </html>
    """
    return html_content

if __name__ == "__main__":
    # Dynamically read the PORT environment variable for cloud deployment (Koyeb, Render, etc.)
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
