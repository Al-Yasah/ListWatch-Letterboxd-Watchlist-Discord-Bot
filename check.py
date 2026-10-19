import os
import re
import sqlite3
import datetime
import time
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
USER_ID = os.getenv("DISCORD_USER_ID")
LETTERBOXD_USER = os.getenv("LETTERBOXD_USERNAME", "Exodiac")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")

DB_PATH = "tracker.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notified_movies (
                slug TEXT PRIMARY KEY,
                title TEXT,
                date_notified TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

def is_notified(slug: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM notified_movies WHERE slug = ?", (slug,))
        return cursor.fetchone() is not None

def mark_notified(slug: str, title: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO notified_movies (slug, title) VALUES (?, ?)", (slug, title))

def fetch_watchlist(username: str) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    movies = []
    page = 1

    print(f"Scraping watchlist for {username}...")
    while True:
        url = f"https://letterboxd.com/{username}/watchlist/page/{page}/"
        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code != 200:
                break

            soup = BeautifulSoup(res.text, "html.parser")
            posters = soup.select("div.film-poster")
            if not posters:
                break

            page_added = 0
            for p in posters:
                img = p.find("img")
                if not img or not img.get("alt"):
                    continue

                raw_title = img["alt"].strip()
                if not raw_title or raw_title.lower() == "poster":
                    continue

                parent_a = p.find_parent("a") or p.find("a")
                parent_li = p.find_parent("li")
                
                slug = None
                if parent_li and parent_li.has_attr("data-film-slug"):
                    slug = parent_li["data-film-slug"]
                elif parent_a and parent_a.get("href"):
                    slug = parent_a["href"].strip("/").split("/")[-1]
                
                if not slug:
                    slug = re.sub(r"[^\w\s-]", "", raw_title).strip().lower().replace(" ", "-")

                match = re.match(r"^(.*?)(?:\s*\((\d{4})\))?$", raw_title)
                title = match.group(1).strip() if match else raw_title
                year = match.group(2) if (match and match.group(2)) else None

                if not any(m["title"].lower() == title.lower() for m in movies):
                    movies.append({"slug": slug, "title": title, "year": year})
                    page_added += 1

            print(f"Scraped page {page}: +{page_added} movies (Total: {len(movies)})")
            if page_added == 0:
                break

            page += 1
            time.sleep(0.4)
        except Exception as e:
            print(f"Error fetching page {page}: {e}")
            break

    return movies

def check_digital_status_global(title: str, year: str | None) -> dict | None:
    search_url = "https://api.themoviedb.org/3/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": title}
    if year:
        params["primary_release_year"] = year

    try:
        res = requests.get(search_url, params=params, timeout=10).json()
        results = res.get("results", [])
        if not results:
            return None

        movie = results[0]
        movie_id = movie["id"]
        poster_path = movie.get("poster_path")
        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
        today = datetime.date.today().isoformat()

        is_digital = False
        earliest_digital_date = None

        # Check all countries for Digital / TV release dates
        dates_url = f"https://api.themoviedb.org/3/movie/{movie_id}/release_dates"
        dates_res = requests.get(dates_url, params={"api_key": TMDB_API_KEY}, timeout=10).json()

        for country in dates_res.get("results", []):
            for item in country.get("release_dates", []):
                if item.get("type") in [4, 6]:
                    r_date = item.get("release_date", "")[:10]
                    if r_date and r_date <= today:
                        is_digital = True
                        if earliest_digital_date is None or r_date < earliest_digital_date:
                            earliest_digital_date = r_date

        # Check all countries globally for streaming/rent/buy providers
        providers_url = f"https://api.themoviedb.org/3/movie/{movie_id}/watch/providers"
        prov_res = requests.get(providers_url, params={"api_key": TMDB_API_KEY}, timeout=10).json()
        all_countries = prov_res.get("results", {})

        streams = set()
        rent_buy = set()

        for _, data in all_countries.items():
            for p in data.get("flatrate", []):
                streams.add(p["provider_name"])
            for p in data.get("rent", []) + data.get("buy", []):
                rent_buy.add(p["provider_name"])

        if is_digital or streams or rent_buy:
            return {
                "title": movie["title"],
                "overview": movie.get("overview", "No synopsis provided."),
                "poster_url": poster_url,
                "digital_date": earliest_digital_date,
                "stream": sorted(list(streams)),
                "rent_buy": sorted(list(rent_buy)),
            }
    except Exception as e:
        print(f"Error checking TMDb globally for '{title}': {e}")

    return None

def send_discord_dm(user_id: str, bot_token: str, movie_data: dict, slug: str):
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json"
    }
    
    # 1. Create DM channel
    dm_res = requests.post(
        "https://discord.com/api/v10/users/@me/channels",
        headers=headers,
        json={"recipient_id": user_id},
        timeout=10
    )
    if dm_res.status_code not in (200, 201):
        print(f"Failed to open DM channel: {dm_res.text}")
        return False
        
    channel_id = dm_res.json()["id"]

    # 2. Build Embed
    embed = {
        "title": f"🎬 {movie_data['title']} is now on Digital (Global)!",
        "url": f"https://letterboxd.com/film/{slug}/",
        "description": movie_data["overview"][:250] + "..." if len(movie_data["overview"]) > 250 else movie_data["overview"],
        "color": 0x00E054,
        "fields": [],
        "footer": {"text": "Availability: Global • TMDb"}
    }
    if movie_data["poster_url"]:
        embed["thumbnail"] = {"url": movie_data["poster_url"]}

    if movie_data["stream"]:
        embed["fields"].append({
            "name": "Streaming Globally On",
            "value": ", ".join(movie_data["stream"][:8]),
            "inline": False
        })
    if movie_data["rent_buy"]:
        embed["fields"].append({
            "name": "Rent / Buy (VOD)",
            "value": ", ".join(movie_data["rent_buy"][:8]),
            "inline": False
        })
    if movie_data["digital_date"]:
        embed["fields"].append({
            "name": "Digital Release Date",
            "value": movie_data["digital_date"],
            "inline": True
        })

    # 3. Post Message
    msg_res = requests.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers=headers,
        json={"embeds": [embed]},
        timeout=10
    )
    return msg_res.status_code in (200, 201)

def main():
    init_db()
    watchlist = fetch_watchlist(LETTERBOXD_USER)
    print(f"\nProcessing {len(watchlist)} titles globally...")

    new_alerts = 0
    for idx, item in enumerate(watchlist, 1):
        slug = item["slug"]
        title = item["title"]

        if is_notified(slug):
            continue

        print(f"[{idx}/{len(watchlist)}] Checking '{title}'...")
        info = check_digital_status_global(title, item["year"])
        if info:
            print(f"  -> Match found! Sending DM...")
            if send_discord_dm(USER_ID, BOT_TOKEN, info, slug):
                mark_notified(slug, title)
                new_alerts += 1
                time.sleep(1)

    print(f"\nSync complete. Sent {new_alerts} alerts.")

if __name__ == "__main__":
    main()