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

# Thread starter flag
thread_started = False

# Globals
access_token = None
refresh_token = None
token_expires_at = None
DB_PATH = "spotify_tracks.db"
current_track_id = None
current_start_time = None
current_track_duration = 180


# ------------------------------------------------------
# DATABASE
# ------------------------------------------------------
def init_db():
    print(f"DB initialized at {os.path.abspath(DB_PATH)}")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
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
    """)
    conn.commit()
    conn.close()


init_db()


# ------------------------------------------------------
# AUTH URL
# ------------------------------------------------------
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

    res = requests.post(token_url, headers=headers, data=data)
    tokens = res.json()

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)
    token_expires_at = datetime.now().timestamp() + expires_in - 60

    return redirect("/dashboard")


# ------------------------------------------------------
# REFRESH ACCESS TOKEN
# ------------------------------------------------------
def refresh_access_token():
    global access_token, refresh_token, token_expires_at
    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_string.encode()).decode()

    url = "https://accounts.spotify.com/api/token"
    headers = {"Authorization": f"Basic {b64_auth}"}
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}

    try:
        res = requests.post(url, headers=headers, data=data)
        tokens = res.json()

        access_token = tokens.get("access_token")
        expires_in = tokens.get("expires_in", 3600)
        token_expires_at = datetime.now().timestamp() + expires_in - 60

        print("Token refreshed")

    except Exception as e:
        print("Error refreshing:", e)


# ------------------------------------------------------
# BACKGROUND TRACK POLLING
# ------------------------------------------------------
def background_track_polling():
    global current_track_id, current_start_time, current_track_duration

    print("Background thread started")

    while True:
        try:
            if token_expires_at and datetime.now().timestamp() > token_expires_at:
                refresh_access_token()

            if access_token:
                url = "https://api.spotify.com/v1/me/player/currently-playing"
                headers = {"Authorization": f"Bearer {access_token}"}
                res = requests.get(url, headers=headers, timeout=10)

                if res.status_code == 200:
                    data = res.json()

                    if data.get("item") and data.get("is_playing"):
                        track_id = data["item"]["id"]
                        track_name = data["item"]["name"]
                        artists = ", ".join([a["name"] for a in data["item"]["artists"]])
                        album_name = data["item"]["album"]["name"]
                        album_image = data["item"]["album"]["images"][0]["url"]
                        duration_ms = data["item"]["duration_ms"]
                        now = datetime.now()

                        current_track_duration = int(duration_ms / 1000)

                        # New track
                        if track_id != current_track_id:
                            # Close previous track
                            if current_track_id:
                                conn = sqlite3.connect(DB_PATH)
                                c = conn.cursor()
                                c.execute("""
                                    UPDATE track_history
                                    SET end_time=?
                                    WHERE track_id=? AND end_time IS NULL
                                """, (now, current_track_id))
                                conn.commit()
                                conn.close()

                            # Insert new
                            conn = sqlite3.connect(DB_PATH)
                            c = conn.cursor()
                            c.execute("""
                                INSERT INTO track_history
                                (track_id, track_name, artists, album_name, album_image, start_time)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (track_id, track_name, artists, album_name, album_image, now))
                            conn.commit()
                            conn.close()

                            current_track_id = track_id
                            current_start_time = now

                        # Same track but resumed
                        elif current_start_time is None:
                            current_start_time = now

                    else:
                        current_start_time = None
                        current_track_duration = 180

        except Exception as e:
            print("Polling error:", e)

        time.sleep(5)


# ------------------------------------------------------
# GET USER PROFILE
# ------------------------------------------------------
def get_user_profile():
    if not access_token:
        return None
    try:
        url = "https://api.spotify.com/v1/me"
        headers = {"Authorization": f"Bearer {access_token}"}
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            data = r.json()
            return {
                "display_name": data.get("display_name", "Spotify User"),
                "profile_image": data.get("images")[0]["url"] if data.get("images") else None
            }
    except:
        pass
    return None


# ------------------------------------------------------
# DASHBOARD
# ------------------------------------------------------
@app.route("/dashboard")
def dashboard():
    global thread_started

    # Start background thread once
    if not thread_started:
        threading.Thread(target=background_track_polling, daemon=True).start()
        thread_started = True

    # total minutes
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT start_time, end_time FROM track_history")
    rows = c.fetchall()
    conn.close()

    total_seconds = 0
    for start, end in rows:
        start = datetime.strptime(start, "%Y-%m-%d %H:%M:%S.%f")
        if end:
            end = datetime.strptime(end, "%Y-%m-%d %H:%M:%S.%f")
            total_seconds += int((end - start).total_seconds())

    total_minutes = total_seconds // 60

    # top artists
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT artists FROM track_history")
    artist_rows = c.fetchall()
    conn.close()

    artist_counter = Counter()
    for row in artist_rows:
        artist_counter.update([a.strip() for a in row[0].split(",")])

    top_artists = artist_counter.most_common(10)

    # top tracks
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT track_name, artists, album_image FROM track_history")
    track_rows = c.fetchall()
    conn.close()

    track_counter = Counter()
    track_info = {}

    for name, artists, img in track_rows:
        key = (name, artists)
        track_counter[key] += 1
        track_info[key] = img

    top_tracks = [
        {
            "track_name": name,
            "artists": artists,
            "count": count,
            "album_image": track_info[(name, artists)]
        }
        for (name, artists), count in track_counter.most_common(50)
    ]

    user_profile = get_user_profile()

    return render_template("dashboard.html",
                           total_minutes=total_minutes,
                           top_artists=top_artists,
                           top_tracks=top_tracks,
                           user_profile=user_profile)


# ------------------------------------------------------
# CURRENT TRACK
# ------------------------------------------------------
@app.route("/current-track")
def current_track():
    if not access_token:
        return jsonify({"error": "Not logged in"}), 401

    if not current_track_id or not current_start_time:
        return jsonify({"message": "No track currently playing"})

    now = datetime.now()
    seconds_played = int((now - current_start_time).total_seconds())

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT track_name, artists, album_name, album_image
        FROM track_history
        WHERE track_id=?
        ORDER BY start_time DESC LIMIT 1
    """, (current_track_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"message": "Track not found"})

    return jsonify({
        "track_name": row[0],
        "artists": row[1],
        "album_name": row[2],
        "album_image": row[3],
        "seconds_played": seconds_played,
        "duration": current_track_duration
    })


# ------------------------------------------------------
# RUN
# ------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)
