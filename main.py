# main.py
import os
import base64
import sqlite3
import threading
import time
import logging
from collections import Counter
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, redirect, request, render_template, jsonify

import requests

load_dotenv()

# --- Logging ---------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spotify-tracker")

# --- Flask setup ----------------------------------------------------------
app = Flask(__name__)

# --- Config / env --------------------------------------------------------
CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
REDIRECT_URI = os.environ.get("REDIRECT_URI")  # e.g. https://yourapp.onrender.com/callback
SCOPE = "user-read-currently-playing user-read-playback-state"

# --- Globals --------------------------------------------------------------
access_token = None
refresh_token = None
token_expires_at = None

# Use absolute DB path so it works on Render
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "spotify_tracks.db")

# Current live tracking state (kept in memory)
current_track_id = None
current_start_time = None
current_track_duration = 180  # seconds (fallback)

# Poll interval in seconds
POLL_INTERVAL = 5

# ---------- Database init -------------------------------------------------
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
    log.info("DB initialized at %s", DB_PATH)

init_db()

# ---------- Spotify Auth helpers ------------------------------------------
def get_auth_url():
    return (
        "https://accounts.spotify.com/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPE}"
    )

def exchange_code_for_token(code):
    """Exchange authorization code for access + refresh tokens."""
    global access_token, refresh_token, token_expires_at
    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_string.encode()).decode()

    token_url = "https://accounts.spotify.com/api/token"
    headers = {"Authorization": f"Basic {b64_auth}"}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI
    }

    r = requests.post(token_url, headers=headers, data=data, timeout=10)
    if r.status_code != 200:
        log.error("Token exchange failed: %s %s", r.status_code, r.text)
        return False

    tokens = r.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)
    token_expires_at = datetime.now().timestamp() + int(expires_in) - 60
    log.info("Obtained access token (expires in %s s).", expires_in)
    return True

def refresh_access_token():
    """Use refresh token to obtain a new access token."""
    global access_token, token_expires_at, refresh_token
    if not refresh_token:
        log.warning("No refresh token available to refresh access token.")
        return False

    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_string.encode()).decode()
    token_url = "https://accounts.spotify.com/api/token"
    headers = {"Authorization": f"Basic {b64_auth}"}
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}

    try:
        r = requests.post(token_url, headers=headers, data=data, timeout=10)
        if r.status_code != 200:
            log.error("Refresh token failed: %s %s", r.status_code, r.text)
            return False
        tokens = r.json()
        access_token = tokens.get("access_token")
        expires_in = tokens.get("expires_in", 3600)
        token_expires_at = datetime.now().timestamp() + int(expires_in) - 60
        log.info("Refreshed access token (expires in %s s).", expires_in)
        return True
    except Exception as e:
        log.exception("Exception refreshing token: %s", e)
        return False

# ---------- Background polling -------------------------------------------
def background_track_polling():
    """
    Runs in a background thread. Polls Spotify's currently-playing endpoint
    and logs play periods to SQLite. Designed to run continuously on Render.
    """
    global current_track_id, current_start_time, access_token, token_expires_at, current_track_duration

    log.info("Background polling thread running (interval %s s)", POLL_INTERVAL)
    while True:
        try:
            # Refresh access token if near expiry
            if token_expires_at and datetime.now().timestamp() > token_expires_at:
                refreshed = refresh_access_token()
                if not refreshed:
                    log.warning("Token refresh failed; will retry later.")

            if not access_token:
                # nothing to do without an access token
                time.sleep(POLL_INTERVAL)
                continue

            url = "https://api.spotify.com/v1/me/player/currently-playing"
            headers = {"Authorization": f"Bearer {access_token}"}
            r = requests.get(url, headers=headers, timeout=10)

            # 204 = nothing playing, 200 = data, other = error
            if r.status_code == 204:
                # nothing playing; mark paused/stopped
                if current_start_time is not None:
                    # When we receive 204 we treat it as stop/pause: end current track
                    now = datetime.now()
                    try:
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute('''
                            UPDATE track_history
                            SET end_time = ?
                            WHERE track_id = ? AND end_time IS NULL
                        ''', (now, current_track_id))
                        conn.commit()
                        conn.close()
                        log.info("Marked end_time for track %s at %s (204)", current_track_id, now)
                    except Exception:
                        log.exception("Failed to update end_time on 204")
                    current_start_time = None
                current_track_duration = 180
                time.sleep(POLL_INTERVAL)
                continue

            if r.status_code != 200:
                log.warning("currently-playing returned %s: %s", r.status_code, r.text)
                time.sleep(POLL_INTERVAL)
                continue

            data = r.json()
            # defensive checks
            item = data.get("item")
            is_playing = data.get("is_playing", False)
            if not item or not is_playing:
                # If item missing or not playing: pause behavior
                if current_start_time is not None:
                    # End previous track if it was playing
                    now = datetime.now()
                    try:
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute('''
                            UPDATE track_history
                            SET end_time = ?
                            WHERE track_id = ? AND end_time IS NULL
                        ''', (now, current_track_id))
                        conn.commit()
                        conn.close()
                        log.info("Marked end_time for track %s at %s (paused/stopped)", current_track_id, now)
                    except Exception:
                        log.exception("Failed to update end_time on pause/stop")
                    current_start_time = None
                current_track_duration = 180
                time.sleep(POLL_INTERVAL)
                continue

            # Extract useful info
            track_id = item.get("id")
            track_name = item.get("name", "Unknown")
            artists = ", ".join([a.get("name", "") for a in item.get("artists", [])])
            album = item.get("album", {})
            album_name = album.get("name")
            album_images = album.get("images") or []
            album_image = album_images[0].get("url") if album_images else None
            duration_ms = item.get("duration_ms", None)
            now = datetime.now()

            if duration_ms:
                current_track_duration = int(duration_ms / 1000)

            # If the track changed, close previous and insert new
            if track_id != current_track_id:
                # End previous
                if current_track_id is not None:
                    try:
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute('''
                            UPDATE track_history
                            SET end_time = ?
                            WHERE track_id = ? AND end_time IS NULL
                        ''', (now, current_track_id))
                        conn.commit()
                        conn.close()
                        log.info("Ended previous track %s at %s", current_track_id, now)
                    except Exception:
                        log.exception("Failed to end previous track on change")

                # Insert new track
                try:
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute('''
                        INSERT INTO track_history
                        (track_id, track_name, artists, album_name, album_image, start_time)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (track_id, track_name, artists, album_name, album_image, now))
                    conn.commit()
                    conn.close()
                    log.info("Inserted new track %s - %s", track_id, track_name)
                except Exception:
                    log.exception("Failed to insert new track")

                current_track_id = track_id
                current_start_time = now

            else:
                # same track; if current_start_time was None (resumed), set it
                if current_start_time is None:
                    current_start_time = now
                    log.info("Resumed tracking for track %s at %s", current_track_id, now)

        except Exception as e:
            log.exception("Background polling error: %s", e)

        time.sleep(POLL_INTERVAL)

# We will start this in before_first_request to be safe with Gunicorn
_background_thread_started = False

@app.before_first_request
def start_background_thread():
    global _background_thread_started
    if _background_thread_started:
        return
    thread = threading.Thread(target=background_track_polling)
    thread.daemon = True
    thread.start()
    _background_thread_started = True
    log.info("Background polling thread started via before_first_request")

# ---------- Routes --------------------------------------------------------
@app.route("/")
def login():
    # redirect user to Spotify consent screen
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        msg = "Missing CLIENT_ID/CLIENT_SECRET/REDIRECT_URI environment variables"
        log.error(msg)
        return msg, 500
    return redirect(get_auth_url())

@app.route("/callback")
def callback():
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        log.error("Spotify callback returned error: %s", error)
        return f"Spotify returned error: {error}", 400
    if not code:
        log.error("No code parameter in callback")
        return "Missing code in callback", 400

    ok = exchange_code_for_token(code)
    if not ok:
        return "Failed to exchange code for token. Check logs.", 500

    # Helpful logging for debugging (won't leak to users but in server logs)
    log.info("Access token and refresh token obtained. Redirecting to dashboard.")
    return redirect("/dashboard")

@app.route("/dashboard")
def dashboard():
    # total minutes across all finished plays
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT start_time, end_time FROM track_history")
    rows = c.fetchall()
    conn.close()

    total_seconds = 0
    for row in rows:
        try:
            start_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S.%f")
            end_time = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S.%f") if row[1] else None
            if end_time:
                total_seconds += int((end_time - start_time).total_seconds())
        except Exception:
            # skip malformed rows
            continue
    total_minutes = total_seconds // 60

    # top artists
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT artists FROM track_history")
    artist_rows = c.fetchall()
    conn.close()
    artist_counter = Counter()
    for row in artist_rows:
        if not row or not row[0]:
            continue
        artists = [a.strip() for a in row[0].split(",") if a.strip()]
        artist_counter.update(artists)
    top_artists = artist_counter.most_common(10)

    # top tracks
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT track_name, artists, album_image FROM track_history")
    track_rows = c.fetchall()
    conn.close()
    track_counter = Counter()
    track_info = {}
    for row in track_rows:
        if not row:
            continue
        key = (row[0], row[1])
        track_counter[key] += 1
        track_info[key] = row[2]
    top_tracks_list = []
    for (track_name, artists), count in track_counter.most_common(50):
        top_tracks_list.append({
            "track_name": track_name,
            "artists": artists,
            "count": count,
            "album_image": track_info.get((track_name, artists))
        })

    user_profile = get_user_profile()

    return render_template("dashboard.html",
                           total_minutes=total_minutes,
                           top_artists=top_artists,
                           top_tracks=top_tracks_list,
                           user_profile=user_profile)

@app.route("/current-track")
def current_track():
    global current_track_id, current_start_time, current_track_duration
    # If not authenticated
    if not access_token:
        return jsonify({"error": "User not authenticated"}), 401

    # If no track playing
    if not current_track_id or not current_start_time:
        return jsonify({"message": "No track currently playing"}), 200

    now = datetime.now()
    seconds_played = int((now - current_start_time).total_seconds())

    # fetch last known metadata from DB (fallback)
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

# ---------- User profile helper ------------------------------------------
def get_user_profile():
    if not access_token:
        return None
    try:
        url = "https://api.spotify.com/v1/me"
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            return {
                "display_name": data.get("display_name", "Spotify User"),
                "profile_image": data.get("images")[0]["url"] if data.get("images") else None
            }
        else:
            log.warning("Failed to fetch user profile: %s %s", res.status_code, res.text)
    except Exception:
        log.exception("Exception while fetching user profile")
    return None

# ---------- Local dev helper ----------------------------------------------
if __name__ == "__main__":
    # For local testing only; on Render use Procfile + gunicorn
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
