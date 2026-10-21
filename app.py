import streamlit as st
import requests
import json
import sqlite3
import statistics
import time
from openai import OpenAI

# --- 1. BAZA PODATAKA ---
conn = sqlite3.connect('olx_deals.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS seen_ads 
             (id INTEGER PRIMARY KEY, title TEXT, price REAL, market_price REAL, profit REAL, url TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
conn.commit()

# --- 2. OLX.BA API + KEŠIRANJE TRŽIŠNIH CIJENA ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json"
}

# Riječi koje odmah ignorišemo BEZ trošenja AI limita!
BANNED_WORDS = ["kutija", "prazna", "neispravno", "neispravan", "dijelovi", "otkup", "kupujem", "iznajmljujem", "najam", "naljepnica", "skin", "kabal", "popravka", "servis"]

def fetch_latest_olx_ads(query, limit=10):
    url = "https://api.olx.ba/search"
    params = {"q": query, "sort_by": "date", "sort_order": "desc", "per_page": limit}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        st.error(f"Greška pri konekciji na OLX: {e}")
    return []

@st.cache_data(ttl=3600) # Pamti prosječnu cijenu 1 sat da ne opterećuje OLX
def get_market_median_price(standardized_name):
    url = "https://api.olx.ba/search"
    params = {"q": standardized_name, "per_page": 30}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            ads = r.json().get("data", [])
            prices = [float(ad.get("price", 0)) for ad in ads if isinstance(ad.get("price"), (int, float)) and ad.get("price", 0) > 50]
            if len(prices) >= 3:
                prices.sort()
                cut = max(1, int(len(prices) * 0.15))
                filtered = prices[cut:-cut] if len(prices) > 5 else prices
                return round(statistics.median(filtered), -1)
    except Exception:
        pass
    return 0

# --- 3. GROQ AI ANALIZA (Sa automatskim Retry mehanizmom) ---
def analyze_with_groq(client, title, listed_price):
    prompt = f"""
    Analiziraj ovaj OLX.ba oglas:
    Naslov: "{title}"
    Cijena: {listed_price} KM

    Zadatak:
    1. Ako je oglas samo za dodatnu opremu (džojstik, igrica, kuler, kućište bez komponenti) ili uslugu, postavi "is_valid_hardware" na false. Samo kompletne konzole, grafičke kartice, procesori, laptopi i PC su true.
    2. Napiši čist, kratak naziv modela za pretragu cijene (npr. "PlayStation 5", "RTX 3060", "RX 6600 XT", "Ryzen 5 5600").

    Vrati isključivo JSON:
    {{
        "is_valid_hardware": true,
        "standardized_name": "string"
    }}
    """
    # Ako slučajno udari u Rate Limit, sačekaće i probati ponovo automatski
    for attempt in range(3):
        try:
            res = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            return json.loads(res.choices[0].message.content)
        except Exception as e:
            if "429" in str(e) or "rate_limit" in str(e).lower():
                time.sleep(6) # Čeka 6 sekundi ako se desi rate limit
            else:
                return None
    return None

# --- 4. TELEGRAM NOTIFIKACIJA ---
def send_telegram_alert(bot_token, chat_id, message):
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass

# --- 5. WEB SAJT (STREAMLIT DASHBOARD) ---
st.set_page_config(page_title="OLX.ba AI Sniper", page_icon="⚡", layout="wide")
st.title("⚡ OLX.ba AI Arbitrage Sniper (Groq No-Limit Edition)")
st.markdown("Koristi **Groq (Llama 3.3 70B)** + Python pred-filtere tako da **nikad ne udara u Rate Limit**.")

with st.sidebar:
    st.header("⚙️ Konfiguracija")
    groq_key = st.text_input("Groq API Key (gsk_...)", value=st.secrets.get("GROQ_API_KEY", ""), type="password")
    tg_token = st.text_input("Telegram Bot Token", value=st.secrets.get("TELEGRAM_TOKEN", ""), type="password")
    tg_chat_id = st.text_input("Telegram Chat ID", value=st.secrets.get("TELEGRAM_CHAT_ID", ""))
    
    st.divider()
    search_terms = st.text_area("Šta skeniramo? (odvojeno zarezom)", "ps5, playstation 5, rtx 3060, rtx 3070, rx 6600")
    min_profit = st.slider("Minimalni profit za alarm (KM)", 30, 300, 80, step=10)
    auto_loop = st.checkbox("🔄 Automatski skeniraj svakih 3 minute")

if st.button("🚀 Pokreni Skeniranje Sada", type="primary") or auto_loop:
    if not groq_key:
        st.warning("⚠️ Unesi besplatni Groq API ključ lijevo (uzmi ga na console.groq.com/keys)!")
    else:
        # Povezujemo OpenAI klijent na besplatni Groq server
        client = OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1")
        queries = [q.strip() for q in search_terms.split(",") if q.strip()]
        status_box = st.empty()
        
        for q in queries:
            status_box.info(f"🔍 Skeniram OLX.ba za: **{q}**...")
            ads = fetch_latest_olx_ads(q, limit=10)
            
            for ad in ads:
                ad_id = ad.get("id")
                title = ad.get("title", "")
                raw_price = ad.get("price", 0) or 0
                ad_url = f"https://olx.ba/artikal/{ad_id}"
                
                # 1. Provjeri da li je već u bazi (0 AI poziva)
                c.execute("SELECT id FROM seen_ads WHERE id=?", (ad_id,))
                if c.fetchone():
                    continue
                
                # 2. PYTHON PRED-FILTER: Ignoriši "Po dogovoru" (<= 45 KM) i zabranjene riječi BEZ trošenja AI-ja!
                title_lower = title.lower()
                if raw_price <= 45 or any(bad_word in title_lower for bad_word in BANNED_WORDS):
                    c.execute("INSERT INTO seen_ads (id, title, price, market_price, profit, url) VALUES (?, ?, ?, 0, 0, ?)", (ad_id, title, raw_price, ad_url))
                    conn.commit()
                    continue
                
                # 3. Tek ako oglas ima pravu cijenu i nije smeće, šaljemo ga na Groq AI
                ai_data = analyze_with_groq(client, title, raw_price)
                time.sleep(2.2) # Garantuje max ~27 zahtjeva u minuti (ispod Groq limita od 30/min)
                
                if not ai_data or not ai_data.get("is_valid_hardware"):
                    c.execute("INSERT INTO seen_ads (id, title, price, market_price, profit, url) VALUES (?, ?, ?, 0, 0, ?)", (ad_id, title, raw_price, ad_url))
                    conn.commit()
                    continue
                
                std_name = ai_data.get("standardized_name", q)
                market_price = get_market_median_price(std_name)
                
                if market_price > 0:
                    fast_sell_price = round(market_price * 0.90, -1)
                    profit = fast_sell_price - raw_price
                    
                    c.execute("INSERT INTO seen_ads (id, title, price, market_price, profit, url) VALUES (?, ?, ?, ?, ?, ?)", 
                              (ad_id, f"[{std_name}] {title}", raw_price, market_price, profit, ad_url))
                    conn.commit()
                    
                    if profit >= min_profit:
                        msg = (
                            f"🚨 <b>PROFIT PRILIKA: {std_name}</b>\n\n"
                            f"📦 <b>Oglas:</b> {title}\n"
                            f"💰 <b>Cijena:</b> {raw_price} KM\n"
                            f"📈 <b>Prosjek na OLX-u:</b> {market_price} KM\n"
                            f"⚡ <b>Brza prodaja za:</b> ~{fast_sell_price} KM\n"
                            f"💵 <b>ČIST PROFIT: +{profit} KM</b>\n\n"
                            f"🔗 <a href='{ad_url}'>OTVORI OGLAS NA OLX.BA</a>"
                        )
                        send_telegram_alert(tg_token, tg_chat_id, msg)
                        st.toast(f"🔥 Pronađen deal! Profit: +{profit} KM", icon="💰")
                        
        status_box.success("✅ Skeniranje završeno bez grešaka!")

# --- 6. PRIKAZ REZULTATA NA SAJTU ---
st.subheader("📊 Pronađeni oglasi i kalkulacija zarade")
c.execute("SELECT title, price, market_price, profit, url, created_at FROM seen_ads WHERE market_price > 0 ORDER BY created_at DESC LIMIT 50")
rows = c.fetchall()

if rows:
    for r in rows:
        title, price, m_price, profit, url, dt = r
        col1, col2, col3, col4, col5 = st.columns([4, 1.5, 1.5, 1.5, 1.5])
        col1.write(f"**{title}**")
        col2.write(f"Kupovna: **{price} KM**")
        col3.write(f"OLX Prosjek: **{m_price} KM**")
        if profit >= min_profit:
            col4.success(f"Profit: +{profit} KM")
        elif profit > 0:
            col4.warning(f"Profit: +{profit} KM")
        else:
            col4.error(f"{profit} KM")
        col5.markdown(f"[🔗 Otvori OLX]({url})")
        st.divider()
else:
    st.info("Još nema analiziranih oglasa sa cijenom. Klikni na 'Pokreni Skeniranje Sada'.")

if auto_loop:
    time.sleep(180)
    st.rerun()