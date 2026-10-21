import streamlit as st
import cloudscraper
import json
import sqlite3
import statistics
import time
import re
from openai import OpenAI

# --- 1. BAZA PODATAKA ---
conn = sqlite3.connect('olx_deals.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS seen_ads 
             (id INTEGER PRIMARY KEY, title TEXT, price REAL, market_price REAL, profit REAL, url TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
conn.commit()

# Kreiramo CloudScraper koji imitira pravi Chrome browser
scraper = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "bs-BA,bs;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://olx.ba/",
    "Origin": "https://olx.ba"
}

BANNED_WORDS = ["kutija", "prazna", "neispravno", "neispravan", "dijelovi", "otkup", "kupujem", "iznajmljujem", "najam", "naljepnica", "skin", "kabal", "popravka", "servis"]

# --- 2. PAMETNO POVLAČENJE OGLASA SA OLX.BA ---
def parse_olx_price(ad):
    """Izvlači brojčanu cijenu bez obzira u kojem je formatu OLX vrati"""
    p = ad.get("price")
    if isinstance(p, (int, float)):
        return float(p)
    if isinstance(p, str):
        clean = re.sub(r'[^\d.]', '', p.replace('.', '').replace(',', '.'))
        try:
            return float(clean)
        except:
            pass
    # Provjera display_price polja ako postoji
    dp = ad.get("display_price", "")
    if dp and isinstance(dp, str):
        nums = re.findall(r'\d+', dp.replace('.', ''))
        if nums:
            return float(nums[0])
    return 0.0

def fetch_latest_olx_ads(query, limit=10):
    # Pokušaj 1: Novi OLX API
    urls_to_try = [
        f"https://api.olx.ba/search?q={query}&sort_by=date&sort_order=desc&per_page={limit}",
        f"https://olx.ba/api/search?q={query}&sort_by=date&sort_order=desc&per_page={limit}"
    ]
    
    for url in urls_to_try:
        try:
            # Pokušavamo sa curl_cffi ako je dostupan (najjači za Cloudflare)
            try:
                from curl_cffi import requests as cf_requests
                r = cf_requests.get(url, headers=HEADERS, impersonate="chrome120", timeout=12)
            except ImportError:
                r = scraper.get(url, headers=HEADERS, timeout=12)
                
            if r.status_code == 200:
                data = r.json()
                ads = data.get("data", []) or data.get("listings", []) or data.get("results", [])
                if ads:
                    return ads, "OK"
        except Exception as e:
            last_err = str(e)
            
    return [], f"OLX API blokira zahtjev sa ovog servera (Status: {r.status_code if 'r' in locals() else 'Err'})."

@st.cache_data(ttl=3600)
def get_market_median_price(standardized_name):
    ads, status = fetch_latest_olx_ads(standardized_name, limit=25)
    if ads:
        prices = []
        for ad in ads:
            p = parse_olx_price(ad)
            if p > 50:
                prices.append(p)
        if len(prices) >= 3:
            prices.sort()
            cut = max(1, int(len(prices) * 0.15))
            filtered = prices[cut:-cut] if len(prices) > 5 else prices
            return round(statistics.median(filtered), -1)
    return 0

# --- 3. GROQ AI ANALIZA ---
def analyze_with_groq(client, title, listed_price):
    prompt = f"""
    Analiziraj ovaj OLX.ba oglas:
    Naslov: "{title}"
    Cijena: {listed_price} KM

    Zadatak:
    1. Ako je oglas samo za dodatnu opremu (džojstik, igrica, kuler, prazno kućište) ili uslugu, postavi "is_valid_hardware" na false. Samo kompletne konzole, grafičke kartice, procesori, laptopi i PC su true.
    2. Napiši čist, kratak naziv modela za pretragu cijene (npr. "PlayStation 5", "RTX 3060", "RX 6600", "Ryzen 5 5600").

    Vrati isključivo JSON:
    {{
        "is_valid_hardware": true,
        "standardized_name": "string"
    }}
    """
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
                time.sleep(6)
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
        scraper.post(url, json=payload, timeout=5)
    except Exception:
        pass

# --- 5. WEB SAJT (STREAMLIT DASHBOARD) ---
st.set_page_config(page_title="OLX.ba AI Sniper", page_icon="⚡", layout="wide")
st.title("⚡ OLX.ba AI Arbitrage Sniper (Groq No-Limit Edition)")
st.markdown("Koristi **Groq (Llama 3.3 70B)** + Anti-Cloudflare zaštitu za direktno skeniranje OLX.ba.")

with st.sidebar:
    st.header("⚙️ Konfiguracija")
    groq_key = st.text_input("Groq API Key (gsk_...)", value=st.secrets.get("GROQ_API_KEY", ""), type="password")
    tg_token = st.text_input("Telegram Bot Token", value=st.secrets.get("TELEGRAM_TOKEN", ""), type="password")
    tg_chat_id = st.text_input("Telegram Chat ID", value=st.secrets.get("TELEGRAM_CHAT_ID", ""))
    
    st.divider()
    search_terms = st.text_area("Šta skeniramo? (odvojeno zarezom)", "ps5, playstation 5, rtx 3060, rtx 3070, rx 6600")
    min_profit = st.slider("Minimalni profit za alarm (KM)", 30, 300, 30, step=10)
    auto_loop = st.checkbox("🔄 Automatski skeniraj svakih 3 minute")
    
    st.divider()
    if st.button("🗑️ Očisti bazu pregledanih oglasa"):
        c.execute("DELETE FROM seen_ads")
        conn.commit()
        st.success("Baza očišćena! Klikni na Pokreni Skeniranje.")

if st.button("🚀 Pokreni Skeniranje Sada", type="primary") or auto_loop:
    if not groq_key:
        st.warning("⚠️ Unesi besplatni Groq API ključ lijevo!")
    else:
        client = OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1")
        queries = [q.strip() for q in search_terms.split(",") if q.strip()]
        
        status_box = st.empty()
        log_expander = st.expander("🕵️‍♂️ Pogledaj detaljan rad skenera uživo (Debug Log)", expanded=True)
        
        for q in queries:
            status_box.info(f"🔍 Skeniram OLX.ba za: **{q}**...")
            ads, api_status = fetch_latest_olx_ads(q, limit=8)
            
            if not ads:
                log_expander.error(f"❌ Za '{q}' nije vraćen nijedan oglas. Razlog: {api_status}")
                continue
            else:
                log_expander.write(f"✅ Pronađeno **{len(ads)}** oglasa na OLX-u za `{q}`. Analiziram cijene...")
            
            for ad in ads:
                ad_id = ad.get("id")
                title = ad.get("title", "Nepoznat naslov")
                raw_price = parse_olx_price(ad)
                ad_url = f"https://olx.ba/artikal/{ad_id}"
                
                c.execute("SELECT id FROM seen_ads WHERE id=?", (ad_id,))
                if c.fetchone():
                    log_expander.caption(f"⏭️ Preskačem (već u bazi): {title}")
                    continue
                
                title_lower = title.lower()
                if raw_price <= 45 or any(bad_word in title_lower for bad_word in BANNED_WORDS):
                    log_expander.caption(f"🚫 Ignorišem (Cijena {raw_price} KM ili zabranjena riječ): {title}")
                    c.execute("INSERT INTO seen_ads (id, title, price, market_price, profit, url) VALUES (?, ?, ?, 0, 0, ?)", (ad_id, title, raw_price, ad_url))
                    conn.commit()
                    continue
                
                ai_data = analyze_with_groq(client, title, raw_price)
                time.sleep(2.0)
                
                if not ai_data or not ai_data.get("is_valid_hardware"):
                    log_expander.caption(f"🤖 AI odbacio (dodatna oprema/igrica): {title}")
                    c.execute("INSERT INTO seen_ads (id, title, price, market_price, profit, url) VALUES (?, ?, ?, 0, 0, ?)", (ad_id, title, raw_price, ad_url))
                    conn.commit()
                    continue
                
                std_name = ai_data.get("standardized_name", q)
                market_price = get_market_median_price(std_name)
                
                if market_price > 0:
                    fast_sell_price = round(market_price * 0.90, -1)
                    profit = fast_sell_price - raw_price
                    
                    log_expander.success(f"💎 Obrađen: **{title}** | Cijena: {raw_price} KM | Tržišna: {market_price} KM | Profit: {profit} KM")
                    
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
                        
        status_box.success("✅ Skeniranje završeno!")

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
