import asyncio
import os
import shutil
import sys
import zipfile
import tarfile
import subprocess
import logging
import uuid
import aiohttp
import time
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse
import psutil
from curl_cffi.requests import AsyncSession
from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Telegram Vars
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# OceanVeil Vars
OV_EMAIL = os.environ.get("OV_EMAIL")
OV_PASSWORD = os.environ.get("OV_PASSWORD")
PROXY = os.environ.get("PROXY")

# Check Config
if not all([API_ID, API_HASH, BOT_TOKEN, OV_EMAIL, OV_PASSWORD]):
    logger.error("Missing variables in .env!")
    sys.exit(1)

OUTPUT_ROOT = "downloads"
MAX_CONCURRENT_DOWNLOADS = 3
MAX_FILENAME_LENGTH = 50

# Tool Config
TOOL_BINARY_NAME_WIN = "N_m3u8DL-RE.exe"
TOOL_BINARY_NAME_LINUX = "N_m3u8DL-RE"
TOOL_URL_WIN = "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.2.0/N_m3u8DL-RE_v0.2.0_windows-x64.zip"
TOOL_URL_LINUX = "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.2.0/N_m3u8DL-RE_v0.2.0_linux-x64.tar.gz"

IS_WINDOWS = sys.platform == 'win32'
TOOL_BINARY = TOOL_BINARY_NAME_WIN if IS_WINDOWS else TOOL_BINARY_NAME_LINUX
TOOL_URL = TOOL_URL_WIN if IS_WINDOWS else TOOL_URL_LINUX

def get_tool_path():
    """Get the path to the N_m3u8DL-RE binary"""
    if IS_WINDOWS:
        # Check current directory
        if os.path.exists(TOOL_BINARY_NAME_WIN):
            return os.path.abspath(TOOL_BINARY_NAME_WIN)
        # Check PATH
        path = shutil.which("N_m3u8DL-RE")
        if path:
            return path
        return TOOL_BINARY_NAME_WIN
    else:
        # Check /usr/local/bin
        if os.path.exists("/usr/local/bin/N_m3u8DL-RE"):
            return "/usr/local/bin/N_m3u8DL-RE"
        # Check PATH
        path = shutil.which("N_m3u8DL-RE")
        if path:
            return path
        # Check local directory
        if os.path.exists(TOOL_BINARY_NAME_LINUX):
            return os.path.abspath(TOOL_BINARY_NAME_LINUX)
        return TOOL_BINARY_NAME_LINUX

# Apply nest_asyncio
try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    pass

# Try to import TgCrypto for speed
try:
    import tgcrypto
    logger.info("TgCrypto detected! Encryption will be faster.")
except ImportError:
    logger.warning("TgCrypto not found. Install it for faster speeds: pip install TgCrypto")

# Global Registry
# Tasks: Dict[task_id, TaskObject]
active_tasks = {}
task_queue = {}
anilist_cache = {}
global_status_message = None
tool_lock = asyncio.Lock()
start_time = time.time()

# --- Helper Functions ---

async def search_anilist(title):
    """Search AniList for anime and get thumbnail"""
    clean_title = re.sub(r'\[.*?\]', '', title)
    clean_title = re.sub(r'[～~].*', '', clean_title)
    clean_title = clean_title.strip()
    
    if clean_title in anilist_cache:
        return anilist_cache[clean_title]
    
    query = '''
    query ($search: String) {
        Media (search: $search, type: ANIME) {
            id
            title {
                romaji
                english
                native
            }
            coverImage {
                large
                extraLarge
            }
            bannerImage
        }
    }
    '''
    
    variables = {'search': clean_title}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                'https://graphql.anilist.co',
                json={'query': query, 'variables': variables},
                headers={'Content-Type': 'application/json'}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('data', {}).get('Media'):
                        media = data['data']['Media']
                        result = {
                            'title': media['title'].get('romaji', clean_title),
                            'thumbnail': media['coverImage'].get('extraLarge') or media['coverImage'].get('large'),
                            'banner': media.get('bannerImage')
                        }
                        anilist_cache[clean_title] = result
                        return result
    except Exception as e:
        logger.error(f"AniList search error: {e}")
    
    return None

async def download_thumbnail(url, save_path):
    """Download thumbnail from URL"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    with open(save_path, 'wb') as f:
                        f.write(await resp.read())
                    return save_path
    except Exception as e:
        logger.error(f"Thumbnail download error: {e}")
    return None

def clean_filename(filename):
    """Remove unwanted strings and artifacts"""
    # Remove specific unwanted terms
    unwanted = ["Premium", "DubPremium", "Dub Premium", "dub premium", "dubpremium"]
    for word in unwanted:
        filename = re.sub(re.escape(word), "", filename, flags=re.IGNORECASE)
    
    # Remove tags like [Premium], [Dub], [Sub], [Dual]
    # We do this before stripping to ensure we catch the full tag
    filename = re.sub(r'\[(Premium|Dub|Sub|Dual)\]', '', filename, flags=re.IGNORECASE)
    
    # Remove empty brackets [] or ()
    filename = filename.replace("[]", "").replace("()", "")
    
    # Remove leading/trailing special chars that might be artifacts
    filename = filename.strip(" -_[]")
    
    # Clean up extra spaces
    filename = re.sub(r'\s+', ' ', filename).strip()
    
    # Remove characters that might confuse parsers or look weird
    filename = filename.replace('…', '').replace('～', '-')
    
    # Remove Windows invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)

    return filename


def normalize_search_text(value):
    value = unicodedata.normalize('NFKD', str(value)).casefold()
    value = ''.join(char for char in value if not unicodedata.combining(char))
    return ' '.join(re.findall(r'[a-z0-9]+', value))


def search_match_score(query, candidate):
    query = normalize_search_text(query)
    candidate = normalize_search_text(candidate)
    if not query or not candidate:
        return 0
    if query == candidate:
        return 100
    if query in candidate or candidate in query:
        return 95

    ratio = SequenceMatcher(None, query, candidate).ratio() * 100
    query_tokens = set(query.split())
    candidate_tokens = set(candidate.split())
    coverage = len(query_tokens & candidate_tokens) / len(query_tokens) * 100
    return max(ratio, coverage)


def rank_search_results(query, titles):
    scored = []
    for index, item in enumerate(titles):
        attributes = item.get('attributes', {})
        candidates = [
            attributes.get('name', ''),
            attributes.get('japaneseName', '') or '',
            attributes.get('romajiName', '') or '',
            attributes.get('promotionName', '') or ''
        ]
        score = max(search_match_score(query, candidate) for candidate in candidates)
        if score:
            scored.append((score, index, item))

    scored.sort(key=lambda match: (-match[0], match[1]))
    strong_matches = [item for score, _, item in scored if score >= 55]
    if strong_matches:
        return strong_matches[:30]
    return [item for _, _, item in scored[:15]]


def is_dub_title(title):
    return bool(re.search(r'\bdub(?:bed|premium)?\b', title, flags=re.IGNORECASE))


def variant_base_title(title):
    """Normalize Dub/Sub variants to the same title for pairing."""
    title = re.sub(
        r'[\[(]\s*(?:english\s+)?(?:dub|dubbed|dubpremium|sub|subbed)(?:\s+audio)?\s*[\])]',
        '',
        title,
        flags=re.IGNORECASE
    )
    title = re.sub(r'\b(?:english\s+)?(?:dub|dubbed|dubpremium|sub|subbed)(?:\s+audio)?\b', '', title, flags=re.IGNORECASE)
    return normalize_search_text(clean_filename(title))


def find_dual_pairs(results):
    grouped = {}
    for result in results:
        title = result.get('attributes', {}).get('name', '')
        result_id = str(result.get('id', ''))
        if not title or not result_id:
            continue

        base = variant_base_title(title)
        group = grouped.setdefault(base, {'title': clean_filename(title), 'dub_id': None, 'sub_id': None})
        if is_dub_title(title):
            group['dub_id'] = group['dub_id'] or result_id
        else:
            group['sub_id'] = group['sub_id'] or result_id

    return [group for group in grouped.values() if group['dub_id'] and group['sub_id']]


def dual_choice_text(pairs):
    if len(pairs) == 1:
        return f"🎬 **{pairs[0]['title']}**\n\nChoose which source you want for the video:"
    return "🎬 **Dual audio results**\n\nChoose a title and video source:"


def dual_choice_markup(pairs, user_id, ep_arg=None):
    episode = ep_arg or "_"
    rows = []
    for pair in pairs:
        title = pair['title'][:24]
        sub_label = "🇯🇵 Sub Video" if len(pairs) == 1 else f"🇯🇵 Sub: {title}"
        dub_label = "🇬🇧 Dub Video" if len(pairs) == 1 else f"🇬🇧 Dub: {title}"
        rows.append([
            InlineKeyboardButton(
                sub_label,
                callback_data=f"dual|s|{pair['dub_id']}|{pair['sub_id']}|{episode}|{user_id}"
            ),
            InlineKeyboardButton(
                dub_label,
                callback_data=f"dual|d|{pair['dub_id']}|{pair['sub_id']}|{episode}|{user_id}"
            )
        ])
    return InlineKeyboardMarkup(rows)


def download_results_markup(results, command, user_id, ep_arg=None):
    episode = ep_arg or "_"
    rows = []
    for result in results:
        title = result.get('attributes', {}).get('name', 'Unknown')
        result_id = result.get('id')
        use_english = command == "engdl" or (command == "auto" and is_dub_title(title))
        mode = "e" if use_english else "d"
        rows.append([
            InlineKeyboardButton(
                f"Download: {title}"[:60],
                callback_data=f"download|{mode}|{result_id}|{episode}|{user_id}"
            )
        ])
    return InlineKeyboardMarkup(rows)


def create_short_filename(series_name, episode_num, suffix):
    """Create filename: 'Title - Subtitle - ## [Type].mp4'"""
    series_name = clean_filename(series_name)
    
    # Keep the full name with subtitle, just truncate if too long
    max_series_len = 32
    
    if len(series_name) > max_series_len:
        series_name = series_name[:max_series_len-1]
    
    # Remove [Dub] or [Sub] from series name if present to avoid duplication (just in case clean_filename didn't catch it somehow)
    series_name = re.sub(r'\[(Dub|Sub|Dual)\]', '', series_name, flags=re.IGNORECASE).strip()
    
    # Double check for empty brackets after removal
    series_name = series_name.replace("[]", "").strip()
    
    filename = f"{series_name} - {episode_num} {suffix}.mp4"
    
    if len(filename) > MAX_FILENAME_LENGTH:
        max_series_len = MAX_FILENAME_LENGTH - 20
        series_name = series_name[:max_series_len]
        filename = f"{series_name} - {episode_num} {suffix}.mp4"
    
    return filename

def format_bytes(size):
    """Convert bytes to human readable format"""
    if size == 0:
        return "0 B"
    power = 2**10
    n = 0
    labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power and n < 4:
        size /= power
        n += 1
    return f"{size:.2f} {labels[n]}"

def format_time(seconds):
    """Convert seconds to human readable time"""
    if seconds < 0: seconds = 0
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"

class GlobalProgressTracker:
    def __init__(self):
        self.last_update = 0

    async def update_ui(self):
        global global_status_message
        now = time.time()
        if now - self.last_update < 3: # Update every 3 seconds
            return

        self.last_update = now
        
        if not active_tasks:
            return

        text = ""
        idx = 1
        
        # Sort by start time or just list
        sorted_tasks = list(active_tasks.values())
        
        for task in sorted_tasks:
            # Calculate progress bar
            pct = task.get('percentage', 0)
            filled = int(pct / 10) # 0-10
            # Custom hexagons as requested: ⬢ / ⬡
            bar = "⬢" * filled + "⬡" * (10 - filled)

            processed = task.get('processed_bytes', 0)
            total = task.get('total_bytes', 0)
            speed = task.get('speed', 0)
            elapsed = now - task.get('start_time', now)
            eta = task.get('eta', 0)

            # Status line
            user_name = task.get('user_name', 'Unknown')
            task_id = task.get('task_id', 'Unknown')
            name = task.get('name', 'Unknown Task')
            status = task.get('status', 'Processing')

            text += f"{idx}. {name}\n\n"
            text += f"Task By {user_name} ( `#{task_id}` )\n"
            text += f"┟ [{bar}] {pct:.2f}%\n"
            text += f"┠ Processed → {format_bytes(processed)} of {format_bytes(total)}\n"
            text += f"┠ Status → {status}\n"
            text += f"┠ Speed → {format_bytes(speed)}/s\n"
            text += f"┠ Time → {format_time(elapsed)} ( ETA: {format_time(eta)} )\n"
            text += f"┖ Stop → /cancel {task_id}\n\n"

            idx += 1

        # Bot Stats
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        uptime = format_time(time.time() - start_time)
        disk_free = psutil.disk_usage('.').free
        
        text += f"⌬ Bot Stats\n"
        text += f"┟ CPU → {cpu}% | F → {format_bytes(disk_free)}\n"
        text += f"┖ RAM → {ram}% | UP → {uptime}"

        for t in active_tasks.values():
            msg = t.get('status_msg')
            if msg:
                try:
                    if msg.text != text: # Only edit if changed
                        await msg.edit(text)
                except Exception as e:
                    logger.debug(f"Failed to update message: {e}")

class OCEANVEIL:
    def __init__(self):
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.session = None
        self.auth_header = None
        self.cookies = None
        self.lock = asyncio.Lock()

    async def get_session(self):
        if self.session is None:
            kwargs = {
                "impersonate": "chrome", 
                "headers": {"User-Agent": self.user_agent}
            }
            if PROXY:
                kwargs["proxies"] = {"http": PROXY, "https": PROXY}
            self.session = AsyncSession(**kwargs)
        return self.session

    async def login(self):
        async with self.lock:
            if self.auth_header:
                return True
            session = await self.get_session()
            logger.info(f"Logging in as {OV_EMAIL}...")
            try:
                res = await session.post(
                    "https://oceanveil.net/api/v1/users/login",
                    json={"user": {"email": OV_EMAIL, "password": OV_PASSWORD}}
                )
                if res.status_code not in (200, 201):
                    logger.error(f"Login failed: {res.status_code}")
                    return False

                self.auth_header = res.headers.get('authorization')
                self.cookies = res.cookies.get_dict()
                logger.info("Login successful.")
                return True
            except Exception as e:
                logger.error(f"Login error: {e}")
                return False

    async def search_title(self, query):
        if not self.auth_header and not await self.login():
            return []

        session = await self.get_session()
        url = "https://oceanveil.net/api/v1/anime_titles"
        titles = []
        seen_ids = set()

        try:
            for _ in range(25):
                res = await session.get(
                    url,
                    headers={"authorization": self.auth_header},
                    cookies=self.cookies
                )
                if res.status_code in (401, 403):
                    self.auth_header = None
                    if not await self.login():
                        return []
                    res = await session.get(
                        url,
                        headers={"authorization": self.auth_header},
                        cookies=self.cookies
                    )
                if res.status_code != 200:
                    logger.error(f"Title search failed: {res.status_code}")
                    return []

                payload = res.json()
                for item in payload.get('data', []):
                    item_id = str(item.get('id', ''))
                    if item_id and item_id not in seen_ids:
                        seen_ids.add(item_id)
                        titles.append(item)

                next_url = (payload.get('links') or {}).get('next')
                if not isinstance(next_url, str) or not next_url:
                    break
                next_url = urljoin(url, next_url)
                if urlparse(next_url).hostname != "oceanveil.net":
                    logger.warning("Ignored an unexpected pagination URL from the title API.")
                    break
                url = next_url

            initial_matches = rank_search_results(query, titles)

            async def fetch_related_titles(item):
                item_id = str(item.get('id', ''))
                if not item_id.isdigit():
                    return []
                detail_url = (
                    f"https://oceanveil.net/api/v1/anime_titles/{item_id}"
                    "?include%5B%5D=season_anime_titles"
                    "&include%5B%5D=mature_title"
                    "&include%5B%5D=general_title"
                )
                response = await session.get(
                    detail_url,
                    headers={"authorization": self.auth_header},
                    cookies=self.cookies
                )
                if response.status_code != 200:
                    return []
                return [
                    related for related in response.json().get('included', [])
                    if related.get('type') == 'animeTitle'
                ]

            related_batches = await asyncio.gather(
                *(fetch_related_titles(item) for item in initial_matches[:10]),
                return_exceptions=True
            )
            for batch in related_batches:
                if isinstance(batch, Exception):
                    logger.debug(f"Related-title search failed: {batch}")
                    continue
                for item in batch:
                    item_id = str(item.get('id', ''))
                    if item_id and item_id not in seen_ids:
                        seen_ids.add(item_id)
                        titles.append(item)

            return rank_search_results(query, titles)
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    async def get_episodes(self, id):
        if not self.auth_header:
            if not await self.login(): 
                return [], None, None, None, None
        
        session = await self.get_session()
        logger.info(f"Fetching data for Anime ID: {id}...")
        data = []
        url = f"https://oceanveil.net/api/v1/anime_titles/{id}?include%5B%5D=anime_episodes"
        
        try:
            res = await session.get(
                url, headers={"authorization": self.auth_header}, cookies=self.cookies
            )
            if res.status_code == 401 or res.status_code == 403:
                logger.info("Token expired, attempting re-login...")
                self.auth_header = None
                if await self.login():
                    res = await session.get(
                        url, headers={"authorization": self.auth_header}, cookies=self.cookies
                    )
                    
            if res.status_code != 200:
                logger.error(f"Failed to fetch episodes: {res.status_code}")
                return [], None, None, None, None

            json_resp = res.json()
            attributes = json_resp['data'].get("attributes", {})
            series_name = attributes.get("name", "Unknown")
            
            # Detect the variant before cleaning removes the [Dub] marker.
            is_dub = is_dub_title(series_name)
            series_name_clean = clean_filename(series_name)
            
            lang_code = "eng" if is_dub else "jpn"
            lang_name = "English" if is_dub else "Japanese"
            
            if 'included' in json_resp:
                for i in json_resp['included']:
                    if i.get('type') == "animeEpisode":
                        ep_name = i['attributes']['name']
                        ep_num = i['attributes']['displayNumber']
                        ep_id = i.get('id')
                        
                        full_name = f"{series_name_clean} - Episode {ep_num} - {ep_name}"
                        full_name = clean_filename(full_name)
                        
                        data.append({
                            'name': full_name,
                            'url': f"https://oceanveil.net/api/v1/anime_episodes/{ep_id}/video.m3u8",
                            'ep_id': ep_id, 
                            'anime_id': id,
                            'ep_num': str(ep_num),
                            'ep_title': ep_name,
                            'series_name': series_name_clean,
                            'lang_code': lang_code,
                            'lang_name': lang_name
                        })
            
            logger.info(f"Found {len(data)} episodes for {series_name} ({lang_name}).")
            return data, self.auth_header, self.cookies, self.user_agent, lang_code
        except Exception as e:
            logger.error(f"Error fetching ID {id}: {e}")
            return [], None, None, None, None

async def setup_tool():
    tool_path = get_tool_path()
    if os.path.exists(tool_path):
        return

    # Lock to prevent race conditions during download
    async with tool_lock:
        if os.path.exists(tool_path):
            return

        logger.info(f"Downloading N_m3u8DL-RE tool ({TOOL_BINARY})...")
        async with aiohttp.ClientSession() as session:
            async with session.get(TOOL_URL) as resp:
                if resp.status == 200:
                    archive_name = "tool.zip" if IS_WINDOWS else "tool.tar.gz"
                    with open(archive_name, 'wb') as f:
                        f.write(await resp.read())

                    if IS_WINDOWS:
                        with zipfile.ZipFile(archive_name, 'r') as z:
                            for f in z.namelist():
                                if f.endswith(".exe"):
                                    with open(TOOL_BINARY, "wb") as t:
                                        shutil.copyfileobj(z.open(f), t)
                    else:
                        with tarfile.open(archive_name, "r:gz") as tar:
                            for member in tar.getmembers():
                                if "N_m3u8DL-RE" in member.name and not member.name.endswith("/"):
                                    f = tar.extractfile(member)
                                    with open(TOOL_BINARY, "wb") as t:
                                        shutil.copyfileobj(f, t)
                                    os.chmod(TOOL_BINARY, 0o755)
                                    break

                    os.remove(archive_name)
                    logger.info("Tool setup complete.")
                else:
                    logger.error("Failed to download tool.")

async def download_file(semaphore, ep_data, auth, cookies, ua, save_dir, task_id, cancel_event=None):
    """Downloads to a temp folder"""
    async with semaphore:
        if cancel_event and cancel_event.is_set():
            return None
            
        url = ep_data['url']
        temp_filename = f"temp_{ep_data['anime_id']}_{ep_data['ep_num']}"
        
        referer = f"https://oceanveil.net/anime_titles/{ep_data['anime_id']}?episode={ep_data['ep_id']}"
        cookies_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
        binary = get_tool_path()
        
        os.makedirs(save_dir, exist_ok=True)
        final_path = os.path.join(save_dir, f"{temp_filename}.mp4")
        
        if os.path.exists(final_path):
            return final_path

        logger.info(f"[DOWNLOADING] Ep {ep_data['ep_num']}...")

        # Update Task Status
        if task_id in active_tasks:
            active_tasks[task_id]['status'] = f"Downloading Ep {ep_data['ep_num']}"
            active_tasks[task_id]['name'] = ep_data['name']

        cmd = [
            binary, url,
            "-H", f"Cookie: {cookies_str}",
            "-H", f"Authorization: {auth}",
            "-H", f"User-Agent: {ua}",
            "-H", f"Referer: {referer}",
            "--auto-select", "--save-dir", save_dir, "--save-name", temp_filename, "--no-log"
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.PIPE
        )
        
        start_t = time.time()
        while proc.returncode is None:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                await proc.wait()
                return None

            # Simple progress simulation or check file size
            try:
                if os.path.exists(final_path):
                    size = os.path.getsize(final_path)
                    if task_id in active_tasks:
                        active_tasks[task_id]['processed_bytes'] = size
                        active_tasks[task_id]['total_bytes'] = size * 1.5 # Estimate? Hard without total.
                        # N_m3u8DL-RE doesn't expose total easily until end or parsing log.
                        # We'll just show downloaded size.
                        active_tasks[task_id]['speed'] = size / (time.time() - start_t)
            except:
                pass

            await progress_tracker.update_ui()
            await asyncio.sleep(1)
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
        
        if proc.returncode == 0:
            return final_path
        else:
            logger.error(f"[ERROR] Download failed for {ep_data['name']}")
            return None

def mux_dual_audio(audio_file, video_file, output_path, audio_lang, video_lang, video_has_primary_audio=False):
    """Muxes Audio from File 1 + Video from File 2"""
    logger.info(f"   -> Muxing: {os.path.basename(output_path)}")

    if video_has_primary_audio:
        lang1_code = "eng" if video_lang == "eng" else "jpn"
        lang1_title = "English" if video_lang == "eng" else "Japanese"
        lang2_code = "jpn" if audio_lang == "jpn" else "eng"
        lang2_title = "Japanese" if audio_lang == "jpn" else "English"
    else:
        lang1_code = "eng" if audio_lang == "eng" else "jpn"
        lang1_title = "English" if audio_lang == "eng" else "Japanese"
        lang2_code = "jpn" if video_lang == "jpn" else "eng"
        lang2_title = "Japanese" if video_lang == "jpn" else "English"

    cmd = [
        "ffmpeg", "-y",
        "-i", video_file,
        "-i", audio_file,
        "-map", "0:v",
    ]

    if video_has_primary_audio:
        cmd.extend([
            "-map", "0:a",
            "-map", "1:a",
            f"-metadata:s:a:0", f"language={lang1_code}",
            f"-metadata:s:a:0", f"title={lang1_title}",
            f"-disposition:a:0", "default",
            f"-metadata:s:a:1", f"language={lang2_code}",
            f"-metadata:s:a:1", f"title={lang2_title}",
        ])
    else:
        cmd.extend([
            "-map", "1:a",
            "-map", "0:a",
            f"-metadata:s:a:0", f"language={lang1_code}",
            f"-metadata:s:a:0", f"title={lang1_title}",
            f"-disposition:a:0", "default",
            f"-metadata:s:a:1", f"language={lang2_code}",
            f"-metadata:s:a:1", f"title={lang2_title}",
        ])

    cmd.extend(["-c", "copy", "-shortest", "-loglevel", "error", output_path])
    
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"[MUX ERROR] {e}")
        return False
    except FileNotFoundError:
        logger.error("[CRITICAL] FFmpeg not found.")
        return False

# --- Bot Logic ---

app = Client(
    "ocean_bot", 
    api_id=int(API_ID), 
    api_hash=API_HASH, 
    bot_token=BOT_TOKEN,
    ipv6=False
)
Client.UPLOAD_CHUNK_SIZE = 1024 * 1024 * 4

ocean = None
sem = None
progress_tracker = GlobalProgressTracker()

@app.on_message(filters.command(["start", "Start", "START"]))
async def start_cmd(client, message):
    await message.reply_text(
        "🌊 **OceanVeil Bot Ready!**\n\n"
        "📥 **Commands:**\n"
        "/dl <id> [ep] - Download episodes\n"
        "/engdl <id> [ep] - Download English dub\n"
        "/sdl <id/name> [ep] - Smart Dub/Sub selection\n"
        "/dual <id1> <id2> [ep] - Dual Audio (Sub Video)\n"
        "/engvdiddual <id1> <id2> [ep] - Dual Audio (Dub Video)\n"
        "/queue - Show all tasks\n"
        "/cancel <id> - Cancel a task"
    )

@app.on_message(filters.command(["queue", "status"]))
async def queue_cmd(client, message: Message):
    if not active_tasks:
        await message.reply_text("📭 No active tasks.")
        return
    
    # Force update
    await progress_tracker.update_ui()
    # If the user wants a new message for status, we can send one.
    # But update_ui updates the existing 'status_msg' of tasks.
    # We'll send a new dashboard here.
    # For now, just let the auto-update handle it, or reply with "See above".
    await message.reply_text("🔄 Check active task messages for dashboard updates.")

@app.on_message(filters.command(["cancel"]))
async def cancel_cmd(client, message: Message):
    args = message.command
    if len(args) < 2:
        await message.reply_text("❌ Usage: /cancel <task_id>")
        return
    
    target_id = args[1]

    if target_id in active_tasks:
        task = active_tasks[target_id]
        if 'cancel_event' in task:
            task['cancel_event'].set()
            await message.reply_text(f"🛑 Cancelling task `{target_id}`...")
        else:
            await message.reply_text("❌ Cannot cancel this task.")
    else:
        await message.reply_text(f"❌ Task ID `{target_id}` not found.")

@app.on_message(filters.command(["sdl"]))
async def smart_dl_cmd(client, message: Message):
    args = message.command
    if len(args) < 2:
        await message.reply_text("Usage: /sdl <id/name> [-e ep/range]")
        return

    ep_arg = None
    if "-e" in args:
        e_index = args.index("-e")
        query = " ".join(args[1:e_index])
        if e_index + 1 < len(args):
            ep_arg = args[e_index + 1]
    else:
        query = " ".join(args[1:])

    if not query:
        await message.reply_text("Usage: /sdl <id/name> [-e ep/range]")
        return
    if ep_arg and not re.fullmatch(r'\d+(?:-\d+)?', ep_arg):
        await message.reply_text("❌ Invalid episode filter. Use: 5 or 1-5")
        return

    status = await message.reply_text(f"🔍 Looking for Dub and Sub variants of '{query}'...")
    selected_id = query if query.isdigit() else None

    if selected_id:
        episodes, _, _, _, lang_code = await ocean.get_episodes(selected_id)
        if not episodes:
            await status.edit("❌ Failed to fetch this title.")
            return
        search_query = variant_base_title(episodes[0]['series_name'])
        results = await ocean.search_title(search_query)
    else:
        lang_code = None
        results = await ocean.search_title(query)

    if not results:
        await status.edit(f"❌ No results found for '{query}'.")
        return

    pairs = find_dual_pairs(results)
    if selected_id:
        pairs = [pair for pair in pairs if selected_id in (pair['dub_id'], pair['sub_id'])]

    if pairs:
        pairs = pairs[:10]
        await status.edit(
            dual_choice_text(pairs),
            reply_markup=dual_choice_markup(pairs, message.from_user.id, ep_arg)
        )
        return

    if selected_id:
        command = "engdl" if lang_code == "eng" else "dl"
        fallback = [{
            'id': selected_id,
            'attributes': {'name': episodes[0]['series_name']}
        }]
        await status.edit(
            "ℹ️ No matching Dub/Sub variant was found. You can download the available version:",
            reply_markup=download_results_markup(fallback, command, message.from_user.id, ep_arg)
        )
        return

    text = f"🔍 **Search Results for '{query}'**\n\nNo Dub/Sub pair was found. Choose a version to download."
    await status.edit(
        text,
        reply_markup=download_results_markup(results[:15], "auto", message.from_user.id, ep_arg)
    )


@app.on_message(filters.command(["dl", "engdl"]))
async def dl_cmd(client, message: Message, args_override=None, user_override=None):
    user = user_override or message.from_user
    user_id = user.id
    user_name = user.first_name
    args = args_override or message.command
    command_name = args[0].lower()
    
    if len(args) < 2:
        await message.reply_text("Usage: /dl <id/name> [-e ep/range]")
        return
    
    ep_filter = None
    ep_arg = None
    
    if "-e" in args:
        e_index = args.index("-e")
        aid = " ".join(args[1:e_index])
        if e_index + 1 < len(args):
            ep_arg = args[e_index + 1]
    else:
        last_arg = args[-1]
        if len(args) > 2 and (last_arg.isdigit() or ('-' in last_arg and all(p.isdigit() for p in last_arg.split('-')))):
            aid = " ".join(args[1:-1])
            ep_arg = last_arg
        else:
            aid = " ".join(args[1:])

    if ep_arg:
        if '-' in ep_arg:
            try:
                start, end = map(int, ep_arg.split('-'))
                ep_filter = ('range', start, end)
            except:
                await message.reply_text("❌ Invalid range. Use: 1-5")
                return
        else:
            try:
                ep_num = int(ep_arg)
                ep_filter = ('single', ep_num)
            except:
                await message.reply_text("❌ Invalid episode number.")
                return
                
    if not aid.isdigit():
        status = await message.reply_text(f"🔍 Searching for '{aid}'...")
        results = await ocean.search_title(aid)
        if not results:
            await status.edit(f"❌ No results found for '{aid}'.")
            return
            
        text = f"🔍 **Search Results for '{aid}'**\n\nChoose a title to download."
        await status.edit(
            text,
            reply_markup=download_results_markup(results[:15], command_name, user_id, ep_arg)
        )
        return
    else:
        status = await message.reply_text(f"🔍 Fetching info for {aid}...")
    
    # We do setup_tool here to ensure it's ready, but now it's locked.
    await setup_tool()

    episodes, auth, cookies, ua, lang_code = await ocean.get_episodes(aid)
    if not episodes:
        await status.edit("❌ Failed to fetch episodes.")
        return
    
    if ep_filter:
        if ep_filter[0] == 'single':
            episodes = [ep for ep in episodes if int(ep['ep_num']) == ep_filter[1]]
        elif ep_filter[0] == 'range':
            start, end = ep_filter[1], ep_filter[2]
            episodes = [ep for ep in episodes if start <= int(ep['ep_num']) <= end]
    
    if not episodes:
        await status.edit("❌ No episodes found.")
        return
    
    series_clean = episodes[0]['series_name']
    # Determine suffix based on command or detection
    if command_name == "engdl":
        suffix = "[Dub]"
    else:
        suffix = "[Dub]" if lang_code == "eng" else "[Sub]"
    
    # Start Task
    unique_id = str(uuid.uuid4())[:8]
    temp_dir = os.path.join("temp_work", unique_id)
    cancel_event = asyncio.Event()

    # Register Task
    active_tasks[unique_id] = {
        'task_id': unique_id,
        'user_id': user_id,
        'user_name': user_name,
        'cancel_event': cancel_event,
        'status_msg': status,
        'name': f"{series_clean} {suffix}",
        'status': "Starting...",
        'percentage': 0,
        'processed_bytes': 0,
        'total_bytes': 0,
        'speed': 0,
        'start_time': time.time(),
        'eta': 0
    }
    
    try:
        # Cache Anilist data
        # search_anilist returns the data, we should verify it is cached
        await search_anilist(series_clean)

        for idx, ep in enumerate(episodes, 1):
            if cancel_event.is_set():
                break
            
            # Update task info for global UI
            active_tasks[unique_id]['name'] = f"{series_clean} - Ep {ep['ep_num']}"
            active_tasks[unique_id]['percentage'] = (idx / len(episodes)) * 100
            
            temp_path = await download_file(sem, ep, auth, cookies, ua, temp_dir, unique_id, cancel_event)
            if not temp_path or not os.path.exists(temp_path):
                continue
            
            final_name = create_short_filename(series_clean, ep['ep_num'], suffix)
            final_path = os.path.join(temp_dir, final_name)

            if os.path.exists(final_path): os.remove(final_path)
            shutil.move(temp_path, final_path)
            
            # Uploading
            active_tasks[unique_id]['status'] = "Uploading"
            await progress_tracker.update_ui()

            try:
                # Mock upload progress in global UI via progress callback if possible
                # Pyrogram progress callback doesn't easily map back to our global loop unless we hook it.
                # For now, simple upload message in global UI is "Uploading".
                
                caption = (
                    f"🎬 **{clean_filename(series_clean)}** - Episode {ep['ep_num']}\n"
                    f"📝 {ep['ep_title']}\n"
                    f"🎭 Type: {suffix}"
                )
                
                # Fetch thumb from cache if available
                thumb_path = None
                if series_clean in anilist_cache and anilist_cache[series_clean].get('thumbnail'):
                     thumb_path = await download_thumbnail(anilist_cache[series_clean]['thumbnail'], f"thumb_{unique_id}.jpg")

                # Define a progress hook for upload
                async def progress_hook(current, total):
                    if unique_id in active_tasks:
                        active_tasks[unique_id]['status'] = "Uploading"
                        active_tasks[unique_id]['processed_bytes'] = current
                        active_tasks[unique_id]['total_bytes'] = total
                        # Speed calc omitted for simplicity but could be added
                        await progress_tracker.update_ui()

                await message.reply_document(
                    document=final_path,
                    thumb=thumb_path,
                    caption=caption,
                    progress=progress_hook
                )
                
                if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
                if os.path.exists(final_path): os.remove(final_path)
                
            except Exception as e:
                logger.error(f"Upload error: {e}")
        
        if not cancel_event.is_set():
            active_tasks[unique_id]['status'] = "Completed"
            await progress_tracker.update_ui()
            await asyncio.sleep(2) # Show completed state briefly

    finally:
        if unique_id in active_tasks:
            del active_tasks[unique_id]
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        # Final update to remove task from UI
        await progress_tracker.update_ui()
        await status.delete() # Remove status message when done

@app.on_message(filters.command(["dual", "engvdiddual"]))
async def dual_cmd(client, message: Message, args_override=None, user_override=None):
    # ... Implementation similar to dl_cmd but for dual audio ...
    # Integrating Global UI and Registry
    user = user_override or message.from_user
    user_id = user.id
    user_name = user.first_name
    args = args_override or message.command
    command_name = args[0].lower()
    
    if len(args) < 2:
        await message.reply_text("Usage: /dual <id1> <id2> [-e ep/range]")
        return

    ep_arg = None
    command_args = args[1:]

    if "-e" in command_args:
        e_index = command_args.index("-e")
        if e_index + 1 < len(command_args):
            ep_arg = command_args[e_index + 1]
        command_args = command_args[:e_index]
    elif len(command_args) >= 3:
        last_arg = command_args[-1]
        if last_arg.isdigit() or ('-' in last_arg and all(part.isdigit() for part in last_arg.split('-'))):
            ep_arg = last_arg
            command_args = command_args[:-1]

    if ep_arg and not re.fullmatch(r'\d+(?:-\d+)?', ep_arg):
        await message.reply_text("❌ Invalid episode filter. Use: 5 or 1-5")
        return
    if not command_args:
        await message.reply_text("Usage: /dual <id1> <id2> [-e ep/range]")
        return

    if len(command_args) >= 2 and command_args[0].isdigit() and command_args[1].isdigit():
        id1, id2 = command_args[0], command_args[1]
    else:
        name_str = " ".join(command_args)
        status = await message.reply_text(f"🔍 Searching for dual variants of {name_str}...")
        results = await ocean.search_title(name_str)
        pairs = find_dual_pairs(results)

        if not pairs:
            await status.edit(f"❌ {name_str} doesn't have both Sub and Dub variants for Dual.")
            return

        pairs = pairs[:10]
        await status.edit(
            dual_choice_text(pairs),
            reply_markup=dual_choice_markup(pairs, user_id, ep_arg)
        )
        return

    await setup_tool()
    status = await message.reply_text("🔍 Fetching info...")
    eps1, auth1, cookies1, ua1, lang1 = await ocean.get_episodes(id1)
    eps2, auth2, cookies2, ua2, lang2 = await ocean.get_episodes(id2)
    
    if not eps1 or not eps2:
        await status.edit("❌ Failed to fetch info.")
        return

    # ... (Language mapping logic same as before) ...
    is_engvdiddual = command_name == "engvdiddual"
    
    if lang1 == "eng" and lang2 != "eng":
        dub_eps, sub_eps = eps1, eps2
        dub_id, sub_id = id1, id2
        dub_auth, dub_cookies, dub_ua = auth1, cookies1, ua1
        sub_auth, sub_cookies, sub_ua = auth2, cookies2, ua2
    elif lang2 == "eng" and lang1 != "eng":
        dub_eps, sub_eps = eps2, eps1
        dub_id, sub_id = id2, id1
        dub_auth, dub_cookies, dub_ua = auth2, cookies2, ua2
        sub_auth, sub_cookies, sub_ua = auth1, cookies1, ua1
    else:
        dub_eps, sub_eps = eps1, eps2
        dub_id, sub_id = id1, id2
        dub_auth, dub_cookies, dub_ua = auth1, cookies1, ua1
        sub_auth, sub_cookies, sub_ua = auth2, cookies2, ua2

    if is_engvdiddual:
        video_eps, audio_eps = dub_eps, sub_eps
        video_auth, video_cookies, video_ua = dub_auth, dub_cookies, dub_ua
        audio_auth, audio_cookies, audio_ua = sub_auth, sub_cookies, sub_ua
        video_has_primary = True
        video_id, audio_id = dub_id, sub_id
    else:
        video_eps, audio_eps = sub_eps, dub_eps
        video_auth, video_cookies, video_ua = sub_auth, sub_cookies, sub_ua
        audio_auth, audio_cookies, audio_ua = dub_auth, dub_cookies, dub_ua
        video_has_primary = False
        video_id, audio_id = sub_id, dub_id

    # Filter episodes
    if ep_arg:
        try:
            if '-' in ep_arg:
                start, end = map(int, ep_arg.split('-'))
                video_eps = [ep for ep in video_eps if start <= int(ep['ep_num']) <= end]
                audio_eps = [ep for ep in audio_eps if start <= int(ep['ep_num']) <= end]
            else:
                ep_num = int(ep_arg)
                video_eps = [ep for ep in video_eps if int(ep['ep_num']) == ep_num]
                audio_eps = [ep for ep in audio_eps if int(ep['ep_num']) == ep_num]
        except:
            await message.reply_text("❌ Invalid episode filter")
            return

    if not video_eps or not audio_eps:
        await status.edit("❌ No episodes found matching filter.")
        return

    series_clean = video_eps[0]['series_name']
    unique_id = str(uuid.uuid4())[:8]
    task_root = os.path.join("temp_work", unique_id)
    audio_temp = os.path.join(task_root, "audio")
    video_temp = os.path.join(task_root, "video")
    
    cancel_event = asyncio.Event()

    active_tasks[unique_id] = {
        'task_id': unique_id,
        'user_id': user_id,
        'user_name': user_name,
        'cancel_event': cancel_event,
        'status_msg': status,
        'name': f"{series_clean} [Dual]",
        'status': "Starting...",
        'percentage': 0,
        'processed_bytes': 0,
        'total_bytes': 0,
        'speed': 0,
        'start_time': time.time(),
        'eta': 0
    }

    try:
        # Restore AniList search for Dual mode
        await search_anilist(series_clean)
        
        active_tasks[unique_id]['status'] = "Downloading Sources"
        await progress_tracker.update_ui()

        # We need to pass unique_id to download_file to allow it to update status
        tasks = []
        for ep in audio_eps: 
            tasks.append(download_file(sem, ep, audio_auth, audio_cookies, audio_ua, audio_temp, unique_id, cancel_event))
        for ep in video_eps: 
            tasks.append(download_file(sem, ep, video_auth, video_cookies, video_ua, video_temp, unique_id, cancel_event))
        await asyncio.gather(*tasks)
        
        if cancel_event.is_set(): return

        # ... (Muxing and Uploading Logic) ...
        # (Simplified for brevity, but includes status updates)

        active_tasks[unique_id]['status'] = "Muxing & Uploading"
        
        audio_map = {}
        for ep in audio_eps:
            path = os.path.join(audio_temp, f"temp_{audio_id}_{ep['ep_num']}.mp4")
            if os.path.exists(path): audio_map[str(ep['ep_num'])] = path

        for idx, ep in enumerate(video_eps, 1):
            if cancel_event.is_set(): break
            
            ep_num = str(ep['ep_num'])
            video_path = os.path.join(video_temp, f"temp_{video_id}_{ep_num}.mp4")
            if not os.path.exists(video_path): continue
            
            if ep_num in audio_map:
                final_filename = create_short_filename(series_clean, ep['ep_num'], "[Dual]")
                output_path = os.path.join(task_root, final_filename)
                
                # Mux
                active_tasks[unique_id]['status'] = f"Muxing Ep {ep_num}"
                await progress_tracker.update_ui()

                a_lang = audio_eps[0]['lang_code']
                v_lang = video_eps[0]['lang_code']
                
                if mux_dual_audio(audio_map[ep_num], video_path, output_path, a_lang, v_lang, video_has_primary):
                    # Upload
                    active_tasks[unique_id]['status'] = f"Uploading Ep {ep_num}"
                    await progress_tracker.update_ui()

                    try:
                        caption = (
                            f"🎬 **{clean_filename(series_clean)}** - Episode {ep_num}\n"
                            f"📝 {ep['ep_title']}\n"
                            f"🎭 Type: [Dual Audio]"
                        )
                        
                        thumb_path = None
                        if series_clean in anilist_cache and anilist_cache[series_clean].get('thumbnail'):
                             thumb_path = await download_thumbnail(anilist_cache[series_clean]['thumbnail'], f"thumb_{unique_id}.jpg")

                        # Progress Hook for upload
                        async def progress_hook(current, total):
                            if unique_id in active_tasks:
                                active_tasks[unique_id]['status'] = "Uploading"
                                active_tasks[unique_id]['processed_bytes'] = current
                                active_tasks[unique_id]['total_bytes'] = total
                                await progress_tracker.update_ui()

                        await message.reply_document(
                            document=output_path,
                            thumb=thumb_path,
                            caption=caption,
                            progress=progress_hook
                        )
                        if os.path.exists(output_path): os.remove(output_path)
                        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
                    except: pass

    finally:
        if unique_id in active_tasks:
            del active_tasks[unique_id]
        if os.path.exists(task_root):
            shutil.rmtree(task_root, ignore_errors=True)
        await progress_tracker.update_ui()
        await status.delete()

@app.on_callback_query(filters.regex(r'^(dual|download)\|'))
async def selection_callback(client, callback_query):
    parts = callback_query.data.split('|')

    try:
        owner_id = int(parts[-1])
    except (TypeError, ValueError):
        await callback_query.answer("Invalid selection.", show_alert=True)
        return

    if callback_query.from_user.id != owner_id:
        await callback_query.answer("This selection belongs to another user.", show_alert=True)
        return

    action = parts[0]
    if action == "dual" and len(parts) == 6:
        _, mode, dub_id, sub_id, episode, _ = parts
        if mode not in ("s", "d") or not dub_id.isdigit() or not sub_id.isdigit():
            await callback_query.answer("Invalid selection.", show_alert=True)
            return
        command = "dual" if mode == "s" else "engvdiddual"
        args = [command, dub_id, sub_id]
    elif action == "download" and len(parts) == 5:
        _, mode, anime_id, episode, _ = parts
        if mode not in ("d", "e") or not anime_id.isdigit():
            await callback_query.answer("Invalid selection.", show_alert=True)
            return
        command = "dl" if mode == "d" else "engdl"
        args = [command, anime_id]
    else:
        await callback_query.answer("Invalid selection.", show_alert=True)
        return

    if episode != "_":
        if not re.fullmatch(r'\d+(?:-\d+)?', episode):
            await callback_query.answer("Invalid episode filter.", show_alert=True)
            return
        args.extend(["-e", episode])

    await callback_query.answer("Starting...")
    await callback_query.message.edit_text("⏳ Starting your selection...")

    if action == "dual":
        await dual_cmd(client, callback_query.message, args, callback_query.from_user)
    else:
        await dl_cmd(client, callback_query.message, args, callback_query.from_user)


if __name__ == "__main__":
    async def main():
        global ocean, sem
        ocean = OCEANVEIL()
        sem = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        
        print("Starting Bot...")
        await app.start()
        print("Bot running. Press Ctrl+C to stop.")
        await idle()
        await app.stop()

    app.run(main())
