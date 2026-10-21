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

scraper = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "bs-BA,bs;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://olx.ba/",
    "Origin": "https://olx.ba"
}

BANNED_WORDS = [
    "kutija", "prazna", "neispravno", "neispravan", "dijelovi", "za dijelove",
    "otkup", "kupujem", "iznajmljujem", "najam", "naljepnica", "skin", 
    "popravka", "servis", "zamjena za"
]

# --- 2. OLX.BA API FUNKCIJE (VIŠE STRANICA + KATEGORIJE) ---
def parse_olx_price(ad):
    p = ad.get("price")
    if isinstance(p, (int, float)):
        return float(p)
    if isinstance(p, str):
        clean = re.sub(r'[^\d.]', '', p.replace('.', '').replace(',', '.'))
        try:
            return float(clean)
        except:
            pass
    dp = ad.get("display_price", "")
    if dp and isinstance(dp, str):
        nums = re.findall(r'\d+', dp.replace('.', ''))
        if nums:
            return float(nums[0])
    return 0.0

def fetch_olx_ads_multipage(query="", category_id=None, total_limit=40, min_price=100):
    """Povlači više stranica oglasa sa OLX-a uz filtriranje minimalne cijene i kategorije"""
    all_ads = []
    per_page = 40
    pages = max(1, (total_limit + per_page - 1) // per_page)
    
    for page in range(1, pages + 1):
        params = [
            f"sort_by=date",
            f"sort_order=desc",
            f"per_page={per_page}",
            f"page={page}"
        ]
        if query:
            params.append(f"q={query}")
        if category_id and int(category_id) > 0:
            params.append(f"category_id={int(category_id)}")
        if min_price > 0:
            params.append(f"price_from={int(min_price)}")
            
        query_string = "&".join(params)
        urls_to_try = [
            f"https://api.olx.ba/search?{query_string}",
            f"https://olx.ba/api/search?{query_string}"
        ]
        
        page_ads = []
        for url in urls_to_try:
            try:
                try:
                    from curl_cffi import requests as cf_requests
                    r = cf_requests.get(url, headers=HEADERS, impersonate="chrome120", timeout=12)
                except ImportError:
                    r = scraper.get(url, headers=HEADERS, timeout=12)
                    
                if r.status_code == 200:
                    data = r.json()
                    page_ads = data.get("data", []) or data.get("listings", []) or data.get("results", [])
                    if page_ads:
                        break
            except Exception:
                pass
                
        if not page_ads:
            break
            
        all_ads.extend(page_ads)
        if len(all_ads) >= total_limit:
            break
            
    return all_ads[:total_limit]

@st.cache_data(ttl=3600)
def get_market_median_price(standardized_name, min_valid_price=100):
    """Računa srednju cijenu na OLX-u za dati model"""
    ads = fetch_olx_ads_multipage(query=standardized_name, total_limit=40, min_price=min_valid_price)
    if ads:
        prices = []
        for ad in ads:
            title_l = ad.get("title", "").lower()
            if any(bw in title_l for bw in BANNED_WORDS):
                continue
            p = parse_olx_price(ad)
            if p >= min_valid_price:
                prices.append(p)
        if len(prices) >= 3:
            prices.sort()
            cut = max(1, int(len(prices) * 0.15))
            filtered = prices[cut:-cut] if len(prices) > 5 else prices
            return round(statistics.median(filtered), -1)
    return 0

# --- 3. BATCH GROQ AI ANALIZA (Analizira 10 oglasa odjednom!) ---
def analyze_batch_with_groq(client, ads_batch):
    items_text = "\n".join([f"ID: {a['id']} | Naslov: \"{a['title']}\" | Cijena: {a['price']} KM" for a in ads_batch])
    
    prompt = f"""
    Analiziraj ovu listu OLX.ba oglasa za hardver i konzole:
    {items_text}

    PRAVILA:
    1. "is_valid_hardware" je TRUE ako se prodaje kompletna konzola (npr. PS4, PS5, PS5 Pro, Xbox, Switch), grafička kartica (GPU), procesor (CPU), kompletan PC/kompjuter, laptop ili monitor.
    2. VAŽNO: Ako oglas prodaje konzolu ili PC ZAJEDNO sa igricama ili džojsticima/opremom (npr. "PS5 Pro + FC 27 + Oprema"), to JE validna konzola -> postavi "is_valid_hardware": true!
    3. "is_valid_hardware" je FALSE samo ako se prodaje ISKLJUČIVO igrica (CD/nalog), samo džojstik, samo kabal/kutija ili usluga.
    4. Za "standardized_name" napiši čist, kratak naziv glavnog uređaja radi poređenja cijene na OLX-u (npr. "PlayStation 5", "PlayStation 5 Pro", "PlayStation 4", "RTX 3060", "RTX 3070", "RX 6600", "Ryzen 5 5600").

    Vrati isključivo JSON objekat sa listom "results" za svaki ID:
    {{
        "results": [
            {{
                "id": 12345,
                "is_valid_hardware": true,
                "standardized_name": "PlayStation 5"
            }}
        ]
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
            data = json.loads(res.choices[0].message.content)
            return {str(item.get("id")): item for item in data.get("results", [])}
        except Exception as e:
            if "429" in str(e) or "rate_limit" in str(e).lower():
                time.sleep(6)
            else:
                return {}
    return {}

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
st.set_page_config(page_title="OLX.ba AI Sniper PRO", page_icon="⚡", layout="wide")
st.title("⚡ OLX.ba AI Arbitrage Sniper PRO (Multi-Page & Kategorije)")

with st.sidebar:
    st.header("⚙️ API Konfiguracija")
    groq_key = st.text_input("Groq API Key (gsk_...)", value=st.secrets.get("GROQ_API_KEY", ""), type="password")
    tg_token = st.text_input("Telegram Bot Token", value=st.secrets.get("TELEGRAM_TOKEN", ""), type="password")
    tg_chat_id = st.text_input("Telegram Chat ID", value=st.secrets.get("TELEGRAM_CHAT_ID", ""))
    
    st.divider()
    st.header("🎯 Filteri Skeniranja")
    search_terms = st.text_area(
        "Pojmovi za pretragu (odvojeno zarezom):", 
        "ps5, playstation 5, ps4, rtx 3060, rtx 3070, rtx 3080, rx 6600, rx 6700, ryzen 5, gaming pc"
    )
    
    custom_cat_id = st.number_input("OLX ID Kategorije (0 = sve kategorije)", min_value=0, value=0, help="Upiši broj kategorije sa OLX-a ako želiš ograničiti samo na nju, ili ostavi 0.")
    ads_per_query = st.slider("Broj oglasa po pojmu (Dubina skeniranja)", 10, 80, 40, step=10)
    min_ad_price = st.slider("Ignoriši oglase jeftinije od (KM) - izbacuje igrice/kablove", 50, 400, 120, step=10)
    min_profit = st.slider("Minimalni profit za Telegram alarm (KM)", 20, 300, 40, step=10)
    auto_loop = st.checkbox("🔄 Automatski skeniraj svakih 3 minute")
    
    st.divider()
    if st.button("🗑️ Očisti bazu pregledanih oglasa"):
        c.execute("DELETE FROM seen_ads")
        conn.commit()
        st.success("Baza očišćena! Klikni na Pokreni Skeniranje.")

if st.button("🚀 Pokreni Duboko Skeniranje Sada", type="primary") or auto_loop:
    if not groq_key:
        st.warning("⚠️ Unesi besplatni Groq API ključ lijevo!")
    else:
        client = OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1")
        queries = [q.strip() for q in search_terms.split(",") if q.strip()]
        
        status_box = st.empty()
        log_expander = st.expander("🕵️‍♂️ Pogledaj detaljan rad skenera uživo (Debug Log)", expanded=True)
        
        for q in queries:
            status_box.info(f"🔍 Skeniram do **{ads_per_query}** najnovijih oglasa za: **{q}** (Cijena > {min_ad_price} KM)...")
            ads = fetch_olx_ads_multipage(query=q, category_id=custom_cat_id, total_limit=ads_per_query, min_price=min_ad_price)
            
            if not ads:
                log_expander.warning(f"⚠️ Nema oglasa za '{q}' sa cijenom većom od {min_ad_price} KM.")
                continue
                
            candidates = []
            for ad in ads:
                ad_id = ad.get("id")
                title = ad.get("title", "Nepoznat naslov")
                raw_price = parse_olx_price(ad)
                ad_url = f"https://olx.ba/artikal/{ad_id}"
                
                c.execute("SELECT id FROM seen_ads WHERE id=?", (ad_id,))
                if c.fetchone():
                    continue
                
                title_lower = title.lower()
                if raw_price < min_ad_price or any(bad_word in title_lower for bad_word in BANNED_WORDS):
                    c.execute("INSERT INTO seen_ads (id, title, price, market_price, profit, url) VALUES (?, ?, ?, 0, 0, ?)", (ad_id, title, raw_price, ad_url))
                    conn.commit()
                    continue
                
                candidates.append({"id": ad_id, "title": title, "price": raw_price, "url": ad_url})
            
            log_expander.write(f"✅ Za `{q}` povučeno **{len(ads)}** oglasa -> **{len(candidates)}** novih kandidata ide na AI procjenu...")
            
            # Šaljemo po 10 oglasa odjednom na AI (Batch processing - 10x brže!)
            for i in range(0, len(candidates), 10):
                batch = candidates[i:i+10]
                ai_results = analyze_batch_with_groq(client, batch)
                time.sleep(1.5)
                
                for item in batch:
                    ad_id = item["id"]
                    title = item["title"]
                    raw_price = item["price"]
                    ad_url = item["url"]
                    
                    res = ai_results.get(str(ad_id))
                    if not res or not res.get("is_valid_hardware"):
                        log_expander.caption(f"🤖 AI odbacio (nije glavni uređaj): {title} ({raw_price} KM)")
                        c.execute("INSERT INTO seen_ads (id, title, price, market_price, profit, url) VALUES (?, ?, ?, 0, 0, ?)", (ad_id, title, raw_price, ad_url))
                        conn.commit()
                        continue
                    
                    std_name = res.get("standardized_name", q)
                    market_price = get_market_median_price(std_name, min_valid_price=min_ad_price)
                    
                    if market_price > 0:
                        fast_sell_price = round(market_price * 0.90, -1)
                        profit = fast_sell_price - raw_price
                        
                        log_expander.success(f"💎 Obrađen: **[{std_name}] {title}** | Cijena: {raw_price} KM | OLX Prosjek: {market_price} KM | Profit: {profit} KM")
                        
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
                            
        status_box.success("✅ Duboko skeniranje završeno!")

# --- 6. PRIKAZ REZULTATA NA SAJTU ---
st.subheader("📊 Pronađeni oglasi i kalkulacija zarade")
c.execute("SELECT title, price, market_price, profit, url, created_at FROM seen_ads WHERE market_price > 0 ORDER BY profit DESC LIMIT 100")
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
    st.info("Klikni na '🗑️ Očisti bazu pregledanih oglasa' lijevo, pa na '🚀 Pokreni Duboko Skeniranje Sada'.")

if auto_loop:
    time.sleep(180)
    st.rerun()
