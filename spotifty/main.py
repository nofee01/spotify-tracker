from flask import Flask, redirect, request, render_template, jsonify
import requests
import os
import base64
from dotenv import load_dotenv
import sqlite3
from datetime import datetime
import threading
import time
from collections import Counter

load_dotenv()

app = Flask(__name__)

# Spotify credentials
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
SCOPE = "user-read-currently-playing user-read-playback-state"

# Global variables
access_token = None
refresh_token = None
token_expires_at = None
DB_PATH = "spotify_tracks.db"
current_track_id = None
current_start_time = None
current_track_duration = 180

# ---------- Initialize DB ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS track_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id TEXT,
            track_name TEXT NOT NULL,
            artists TEXT NOT NULL,
            album_name TEXT,
            album_image TEXT,
            start_time DATETIME,
            end_time DATETIME
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ---------- Spotify Auth ----------
def get_auth_url():
    return (
        "https://accounts.spotify.com/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPE}"
    )

@app.route("/")
def login():
    return redirect(get_auth_url())

@app.route("/callback")
def callback():
    global access_token, refresh_token, token_expires_at
    code = request.args.get("code")

    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_string.encode()).decode()

    token_url = "https://accounts.spotify.com/api/token"
    headers = {"Authorization": f"Basic {b64_auth}"}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI
    }

    response = requests.post(token_url, headers=headers, data=data)
    tokens = response.json()

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)
    token_expires_at = datetime.now().timestamp() + expires_in - 60

    return redirect("/dashboard")

# ---------- Refresh Spotify token ----------
def refresh_access_token():
    global access_token, token_expires_at, refresh_token
    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_string.encode()).decode()
    token_url = "https://accounts.spotify.com/api/token"
    headers = {"Authorization": f"Basic {b64_auth}"}
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }
    try:
        response = requests.post(token_url, headers=headers, data=data)
        tokens = response.json()
        access_token = tokens.get("access_token")
        expires_in = tokens.get("expires_in", 3600)
        token_expires_at = datetime.now().timestamp() + expires_in - 60
        print("Access token refreshed.")
    except Exception as e:
        print("Error refreshing token:", e)

# ---------- Background Spotify polling ----------
def background_track_polling():
    global current_track_id, current_start_time, access_token, token_expires_at, current_track_duration
    while True:
        try:
            if token_expires_at and datetime.now().timestamp() > token_expires_at:
                refresh_access_token()

            if access_token:
                url = "https://api.spotify.com/v1/me/player/currently-playing"
                headers = {"Authorization": f"Bearer {access_token}"}
                response = requests.get(url, headers=headers, timeout=10)

                if response.status_code == 200:
                    data = response.json()

                    if data.get("item") and data.get("is_playing"):
                        track_id = data['item']['id']
                        track_name = data['item']['name']
                        artists = ', '.join([a['name'] for a in data['item']['artists']])
                        album_name = data['item']['album']['name']
                        album_image = data['item']['album']['images'][0]['url']
                        duration_ms = data['item']['duration_ms']
                        now = datetime.now()

                        # Update duration in seconds
                        current_track_duration = int(duration_ms / 1000)

                        if track_id != current_track_id:
                            if current_track_id is not None:
                                conn = sqlite3.connect(DB_PATH)
                                c = conn.cursor()
                                c.execute('''
                                    UPDATE track_history
                                    SET end_time = ?
                                    WHERE track_id = ? AND end_time IS NULL
                                ''', (now, current_track_id))
                                conn.commit()
                                conn.close()

                            conn = sqlite3.connect(DB_PATH)
                            c = conn.cursor()
                            c.execute('''
                                INSERT INTO track_history (track_id, track_name, artists, album_name, album_image, start_time)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (track_id, track_name, artists, album_name, album_image, now))
                            conn.commit()
                            conn.close()

                            current_track_id = track_id
                            current_start_time = now

                        elif current_start_time is None:
                            current_start_time = now

                    else:
                        current_start_time = None
                        current_track_duration = 180

        except Exception as e:
            print("Background polling error:", e)

        time.sleep(5)

threading.Thread(target=background_track_polling, daemon=True).start()

# ---------- User profile ----------
def get_user_profile():
    if not access_token:
        return None
    try:
        url = "https://api.spotify.com/v1/me"
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            data = res.json()
            return {
                "display_name": data.get("display_name", "Spotify User"),
                "profile_image": data.get("images")[0]["url"] if data.get("images") else None
            }
    except:
        pass
    return None

# ---------- Dashboard ----------
@app.route("/dashboard")
def dashboard():
    # Total minutes
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT start_time, end_time FROM track_history")
    rows = c.fetchall()
    conn.close()

    total_seconds = 0
    for row in rows:
        start_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S.%f")
        end_time = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S.%f") if row[1] else None
        if end_time:
            total_seconds += int((end_time - start_time).total_seconds())
    total_minutes = total_seconds // 60

    # Top artists
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT artists FROM track_history")
    artist_rows = c.fetchall()
    conn.close()
    artist_counter = Counter()
    for row in artist_rows:
        artists = [a.strip() for a in row[0].split(",")]
        artist_counter.update(artists)
    top_artists = artist_counter.most_common(10)

    # Top tracks
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT track_name, artists, album_image FROM track_history")
    track_rows = c.fetchall()
    conn.close()
    track_counter = Counter()
    track_info = {}
    for row in track_rows:
        key = (row[0], row[1])
        track_counter[key] += 1
        track_info[key] = row[2]
    top_tracks_list = []
    for (track_name, artists), count in track_counter.most_common(50):
        top_tracks_list.append({
            "track_name": track_name,
            "artists": artists,
            "count": count,
            "album_image": track_info[(track_name, artists)]
        })

    user_profile = get_user_profile()

    return render_template("dashboard.html",
                           total_minutes=total_minutes,
                           top_artists=top_artists,
                           top_tracks=top_tracks_list,
                           user_profile=user_profile)

# ---------- Track currently playing ----------
@app.route("/current-track")
def current_track():
    if not access_token:
        return jsonify({"error": "User not authenticated"}), 401

    if not current_track_id or not current_start_time:
        return jsonify({"message": "No track currently playing"}), 200

    now = datetime.now()
    seconds_played = int((now - current_start_time).total_seconds())

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT track_name, artists, album_name, album_image FROM track_history "
        "WHERE track_id=? ORDER BY start_time DESC LIMIT 1", (current_track_id,)
    )
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"message": "Track info not found"}), 200

    return jsonify({
        "track_name": row[0],
        "artists": row[1],
        "album_name": row[2],
        "album_image": row[3],
        "seconds_played": seconds_played,
        "duration": current_track_duration
    })

# ---------- Run app ----------
if __name__ == "__main__":
    app.run(debug=True)
