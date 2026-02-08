import os
import logging
import asyncio
from collections import deque
import random
import nextcord
from nextcord.ext import commands
from yt_dlp import YoutubeDL
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from dotenv import load_dotenv
from nextcord import Embed, ui
import math

# 初始化日誌記錄
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 從 .env 檔案載入環境變數
load_dotenv()
FFMPEG_PATH = os.getenv('FFMPEG_PATH')
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
    'executable': FFMPEG_PATH
}
YTDL_OPTIONS = {
    'format': 'bestaudio[ext=m4a]/bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'cachedir': False,
    'skip_download': True,
}

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')

# 初始化 Spotify 和 YoutubeDL 客戶端
try:
    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIPY_CLIENT_ID, client_secret=SPOTIPY_CLIENT_SECRET)
    spotify = spotipy.Spotify(auth_manager=auth_manager)
except Exception as e:
    logging.error(f"Error initializing Spotify: {e}")
    spotify = None

ytdl = YoutubeDL(YTDL_OPTIONS)

# 初始化 Bot
intents = nextcord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 播放相關的佇列
song_queue = deque() # 準備播放的歌曲佇列 (已處理好的)
history_queue = deque() # 已播放的歌曲歷史紀錄
current_song = None
control_message = None

# 新增一個專門用於處理新請求的異步佇列
request_queue = asyncio.Queue()

# 新增快取字典
spotify_cache = {}

# --- 抽獎功能相關變數 ---
raffle_entries = {}
raffle_in_progress = False
raffle_message = None

# --- 互動式抽獎按鈕 ---
class RaffleView(ui.View):
    def __init__(self, item, winner_count):
        super().__init__(timeout=None)
        self.item = item
        self.winner_count = winner_count

    @ui.button(label="參加抽獎！🎉", style=nextcord.ButtonStyle.green)
    async def enter_raffle(self, button: ui.Button, interaction: nextcord.Interaction):
        global raffle_in_progress, raffle_entries
        if not raffle_in_progress:
            await interaction.response.send_message("抽獎已經結束了！", ephemeral=True)
            return

        user = interaction.user
        if user.id in raffle_entries:
            await interaction.response.send_message("您已經參加過這次抽獎了！", ephemeral=True)
        else:
            raffle_entries[user.id] = user
            await interaction.response.send_message("您已成功參加抽獎！祝您好運！", ephemeral=True)
            logging.info(f"User {user.name} entered the raffle.")

# --- 互動式音樂控制面板相關程式碼 ---

class MusicControls(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="⏮️ 上一首", style=nextcord.ButtonStyle.secondary)
    async def previous(self, button: ui.Button, interaction: nextcord.Interaction):
        await interaction.response.defer(ephemeral=True)
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.followup.send("我沒有連線到任何語音頻道。", ephemeral=True)
            return

        if not history_queue:
            await interaction.followup.send("沒有上一首歌曲。", ephemeral=True)
            return

        next_song_data = history_queue.pop()
        
        # 這裡不直接播放，而是將歌曲放入佇列頭部並觸發播放
        song_queue.appendleft(next_song_data)
        
        if vc.is_playing():
            vc.stop()
        else:
            await play_next_in_queue(interaction)
        
        await interaction.followup.send("正在播放上一首歌曲。", ephemeral=True)

    @ui.button(label="⏯️ 暫停/播放", style=nextcord.ButtonStyle.primary)
    async def playpause(self, button: ui.Button, interaction: nextcord.Interaction):
        vc = interaction.guild.voice_client
        if vc:
            if vc.is_playing():
                vc.pause()
                await interaction.response.send_message("已暫停", ephemeral=True)
            else:
                vc.resume()
                await interaction.response.send_message("已繼續播放", ephemeral=True)

    @ui.button(label="⏭️ 下一首", style=nextcord.ButtonStyle.secondary)
    async def skip(self, button: ui.Button, interaction: nextcord.Interaction):
        await interaction.response.defer(ephemeral=True)
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.followup.send("我沒有連線到任何語音頻道。", ephemeral=True)
            return
        
        if not song_queue and not vc.is_playing():
            await interaction.followup.send("目前沒有歌曲在播放或佇列中。", ephemeral=True)
            return

        if vc.is_playing():
            vc.stop()
        else:
            await play_next_in_queue(interaction)
        
        await interaction.followup.send("已跳過。", ephemeral=True)
    
    @ui.button(label="🗑️ 清空佇列", style=nextcord.ButtonStyle.danger)
    async def clear(self, button: ui.Button, interaction: nextcord.Interaction):
        global song_queue, history_queue, current_song
        song_queue.clear()
        history_queue.clear()
        current_song = None
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
        await interaction.response.send_message("佇列已清空。", ephemeral=True)

async def update_music_panel(interaction, title, duration, webpage_url, thumbnail=None):
    """更新或建立播放面板訊息，並包含原始影片連結和時長"""
    global control_message

    description_text = f"**[{title}]({webpage_url})**"

    embed = Embed(title="🎵 正在播放", description=description_text, color=0x1DB954)
    embed.add_field(name="時長", value=duration)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    view = MusicControls()

    if control_message:
        try:
            await control_message.edit(embed=embed, view=view)
            return
        except Exception:
            control_message = None

    control_message = await interaction.channel.send(embed=embed, view=view)

def format_duration(seconds):
    """將秒數轉換為 時:分:秒 或 分:秒 的格式"""
    if seconds is None:
        return "未知"
    
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    remaining_seconds = seconds % 60
    
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
    else:
        return f"{minutes:02d}:{remaining_seconds:02d}"

# --- 輔助函式 ---

def format_duration_extended(total_seconds):
    """
    將總秒數轉換為「X天 Y小時 Z分鐘 W秒」的格式
    """
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    
    parts = []
    if days > 0:
        parts.append(f"{days}天")
    if hours > 0:
        parts.append(f"{hours}小時")
    if minutes > 0:
        parts.append(f"{minutes}分鐘")
    if seconds > 0 or not parts:
        parts.append(f"{seconds}秒")
    
    return " ".join(parts)


class YTDLSource(nextcord.PCMVolumeTransformer):
    def __init__(self, source, *, data, webpage_url):
        super().__init__(source)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.webpage_url = webpage_url
        self.duration = data.get('duration')
        self.thumbnail = data.get('thumbnail')

    @classmethod
    async def from_url(cls, url, *, stream=True):
        loop = asyncio.get_event_loop()
        try:
            # 確保 yt_dlp 的阻塞式操作在一個獨立的執行緒中運行
            info = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

            if not info:
                raise ValueError("無法從提供的連結中找到任何資訊，可能因影片不存在或地區限制。")
            
            if 'entries' in info:
                # 播放清單或頻道連結，只取第一個
                if not info['entries']:
                    raise ValueError("播放清單中沒有找到任何影片。")
                info = info['entries'][0]

            if 'url' not in info or not info['url']:
                raise ValueError("無法從提供的連結中找到有效的音頻 URL。")
            
            webpage_url = info.get('webpage_url', url)
            
            return cls(nextcord.FFmpegPCMAudio(info['url'], **FFMPEG_OPTIONS), data=info, webpage_url=webpage_url)

        except Exception as e:
            logging.error(f"Error extracting info from URL {url}: {e}")
            raise

def is_spotify_playlist(url):
    return 'playlist' in url

async def get_tracks_from_playlist(playlist_url):
    """
    從 Spotify 播放清單獲取所有歌曲連結，並使用異步執行器。
    """
    if not spotify:
        logging.error("Spotify 客戶端未初始化。")
        return []
        
    playlist_id = playlist_url.split('/')[-1].split('?')[0]
    tracks = []
    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, lambda: spotify.playlist_tracks(playlist_id))
        while results:
            tracks.extend([track['track']['external_urls']['spotify'] for track in results['items'] if track and track['track']])
            if results['next']:
                results = await loop.run_in_executor(None, lambda: spotify.next(results))
            else:
                results = None
    except Exception as e:
        logging.error(f"Error fetching tracks from Spotify playlist: {e}")
    return tracks

async def get_youtube_url_from_spotify(track_url):
    """
    從 Spotify 歌曲連結獲取對應的 YouTube 網址，並使用快取機制。
    """
    global spotify_cache
    if track_url in spotify_cache:
        logging.info("從快取中讀取 YouTube URL。")
        return spotify_cache[track_url]

    if not spotify:
        logging.error("Spotify 客戶端未初始化。")
        return None
        
    try:
        loop = asyncio.get_event_loop()
        track_info = await loop.run_in_executor(None, lambda: spotify.track(track_url))
        if not track_info:
            return None
        query = f"{track_info['name']} {track_info['artists'][0]['name']}"
        results = await loop.run_in_executor(None, lambda: ytdl.extract_info(f"ytsearch:{query}", download=False))
        if results and results.get('entries'):
            youtube_url = results['entries'][0]['webpage_url']
            spotify_cache[track_url] = youtube_url
            return youtube_url
    except Exception as e:
        logging.error(f"YouTube search for Spotify track failed: {e}")
    return None

async def on_song_finish(interaction, error):
    global current_song
    if error:
        logging.error(f"Playback error: {error}")
    
    if current_song:
        history_queue.append(current_song)
    current_song = None

    if song_queue:
        await play_next_in_queue(interaction)

async def play_next_in_queue(interaction):
    global current_song
    voice = interaction.guild.voice_client
    if not voice:
        return

    if not song_queue:
        await interaction.channel.send("佇列已空，停止播放。")
        return
    
    if voice.is_playing():
        voice.stop()
        await asyncio.sleep(0.5)

    try:
        new_song_data = song_queue.popleft()
        
        if not new_song_data or not isinstance(new_song_data, dict) or 'webpage_url' not in new_song_data:
            logging.error("從佇列中取得無效的歌曲資料，將跳過。")
            await interaction.channel.send("從佇列中取得無效的歌曲資料，將跳過並播放下一首。")
            await play_next_in_queue(interaction)
            return

        new_source = await YTDLSource.from_url(new_song_data['webpage_url'], stream=True)
        
        voice.play(new_source, after=lambda e: asyncio.run_coroutine_threadsafe(on_song_finish(interaction, e), bot.loop))
        
        current_song = {
            'title': new_source.title,
            'source': new_source,
            'webpage_url': new_source.webpage_url,
            'duration': format_duration(new_source.duration),
            'thumbnail': new_source.thumbnail
        }

        await update_music_panel(
            interaction,
            title=current_song['title'],
            duration=current_song['duration'],
            webpage_url=current_song['webpage_url'],
            thumbnail=current_song['thumbnail']
        )
        await interaction.channel.send(f"正在播放下一首：**{current_song['title']}**")
    except Exception as e:
        logging.error(f"Error re-creating source for next song in queue: {e}")
        await interaction.channel.send(f"播放下一首歌曲時發生錯誤：{e}")
        current_song = None
        await play_next_in_queue(interaction)
        return

async def queue_processor():
    """
    獨立的背景任務，負責從 request_queue 處理歌曲並添加到 song_queue。
    這個任務確保所有阻塞式 I/O (yt-dlp, spotipy) 都在背景執行，不影響機器人響應。
    """
    while True:
        interaction, url, is_playlist, is_spotify = await request_queue.get()
        added_count = 0
        
        try:
            status_message = await interaction.channel.send("您的請求已收到，正在處理中... 請稍候。")
        except nextcord.InteractionResponded:
            status_message = await interaction.channel.send("您的請求已收到，正在處理中... 請稍候。")
        
        try:
            if is_spotify:
                tracks = await get_tracks_from_playlist(url) if is_spotify_playlist(url) else [url]
                if not tracks:
                    await status_message.edit(content="沒有找到任何歌曲。")
                    continue
                
                for i, track_url in enumerate(tracks):
                    await status_message.edit(content=f"正在處理 Spotify 歌曲... ({i+1}/{len(tracks)})")
                    youtube_url = await get_youtube_url_from_spotify(track_url)
                    if not youtube_url:
                        logging.warning(f"Skipping track due to no valid URL found for {track_url}")
                        continue
                    try:
                        source_data = await YTDLSource.from_url(youtube_url, stream=True)
                        song_queue.append({
                            'title': source_data.title,
                            'webpage_url': source_data.webpage_url,
                            'duration': format_duration(source_data.duration),
                            'thumbnail': source_data.thumbnail
                        })
                        added_count += 1
                    except Exception as e:
                        logging.warning(f"Skipping track due to error while creating source: {e}")
            
            elif is_playlist:
                loop = asyncio.get_event_loop()
                info = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
                if not info or 'entries' not in info:
                    await status_message.edit(content="播放清單中沒有找到任何影片。")
                    continue
                
                entries = info['entries']
                for i, entry in enumerate(entries):
                    if not entry or 'webpage_url' not in entry:
                        continue
                    await status_message.edit(content=f"正在處理播放清單... ({i+1}/{len(entries)})")
                    try:
                        source_data = await YTDLSource.from_url(entry['webpage_url'], stream=True)
                        song_queue.append({
                            'title': source_data.title,
                            'webpage_url': source_data.webpage_url,
                            'duration': format_duration(source_data.duration),
                            'thumbnail': source_data.thumbnail
                        })
                        added_count += 1
                    except Exception as e:
                        logging.warning(f"Skipping invalid entry from playlist: {e}")
            else: # 處理單一歌曲
                source_data = await YTDLSource.from_url(url, stream=True)
                song_queue.append({
                    'title': source_data.title,
                    'webpage_url': source_data.webpage_url,
                    'duration': format_duration(source_data.duration),
                    'thumbnail': source_data.thumbnail
                })
                added_count = 1

            if added_count > 0:
                await status_message.edit(content=f"已將 {added_count} 首歌曲加入播放佇列。")
            else:
                await status_message.edit(content="沒有找到任何有效的歌曲可以加入。")

            voice_client = interaction.guild.voice_client
            if voice_client and not voice_client.is_playing() and song_queue:
                await play_next_in_queue(interaction)

        except Exception as e:
            logging.error(f"Error in processing URL: {e}")
            try:
                await status_message.edit(content=f"處理您的連結時發生錯誤：{e}")
            except nextcord.NotFound:
                pass
        finally:
            request_queue.task_done()

async def draw_winners(interaction, item, winner_count):
    """
    抽獎結束後，從參加者中隨機選出獲獎者。
    """
    global raffle_in_progress, raffle_entries
    raffle_in_progress = False
    
    if not raffle_entries:
        await interaction.channel.send("抽獎結束了！但沒有人參加，所以沒有獲獎者。")
        return

    participants = list(raffle_entries.values())
    
    actual_winners_count = min(winner_count, len(participants))
    
    winners = random.sample(participants, actual_winners_count)
    
    winner_mentions = " ".join([winner.mention for winner in winners])
    
    embed = Embed(
        title=f"🎉 {item} 抽獎結果！🎉",
        description="恭喜以下獲獎者！",
        color=nextcord.Color.gold()
    )
    embed.add_field(name="恭喜獲獎者！", value=winner_mentions, inline=False)
    embed.set_footer(text=f"總共有 {len(participants)} 位參加者。")

    await interaction.channel.send(content=f"抽獎結束！{winner_mentions} 恭喜！", embed=embed)
    logging.info(f"Raffle for '{item}' ended. Winners: {[w.name for w in winners]}")

    raffle_entries.clear()
    
# --- Bot 事件和指令 ---

@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user.name} ({bot.user.id})")
    bot.loop.create_task(queue_processor())

@bot.event
async def on_application_command_error(interaction, error):
    logging.error(f"Command error: {error}")
    if not interaction.response.is_done():
        await interaction.response.send_message("發生錯誤。", ephemeral=True)

@bot.slash_command(name='join', description="加入你的語音頻道")
async def join(interaction):
    if interaction.user.voice:
        await interaction.user.voice.channel.connect()
        await interaction.response.send_message("已加入語音頻道。", ephemeral=True)
    else:
        await interaction.response.send_message("你必須在語音頻道中才能使用此指令。", ephemeral=True)

@bot.slash_command(name='leave', description="離開語音頻道")
async def leave(interaction):
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("已斷開連線。", ephemeral=True)
    else:
        await interaction.response.send_message("我沒有連線到任何語音頻道。", ephemeral=True)

@bot.slash_command(name='play_spotify', description="播放 Spotify 歌曲或播放清單")
async def play_spotify(interaction: nextcord.Interaction, spotify_url: str):
    voice_client = interaction.guild.voice_client
    if not voice_client:
        if interaction.user.voice:
            voice_client = await interaction.user.voice.channel.connect()
        else:
            await interaction.response.send_message("你必須在語音頻道中才能使用此指令。", ephemeral=True)
            return
    
    await interaction.response.defer(ephemeral=False)
    await request_queue.put((interaction, spotify_url, False, True))

@bot.slash_command(name='play_youtube', description="播放 YouTube 影片或音樂")
async def play_youtube(interaction: nextcord.Interaction, youtube_url: str):
    voice_client = interaction.guild.voice_client
    if not voice_client:
        if interaction.user.voice:
            channel = interaction.user.voice.channel
            voice_client = await channel.connect()
        else:
            await interaction.response.send_message("你沒有連線到語音頻道。", ephemeral=True)
            return

    await interaction.response.defer(ephemeral=False)
    await request_queue.put((interaction, youtube_url, False, False))

@bot.slash_command(name='play_youtube_playlist', description="播放整個 YouTube 播放清單")
async def play_youtube_playlist(interaction: nextcord.Interaction, youtube_playlist_url: str):
    voice_client = interaction.guild.voice_client
    if not voice_client:
        if interaction.user.voice:
            channel = interaction.user.voice.channel
            voice_client = await channel.connect()
        else:
            await interaction.response.send_message("你沒有連線到語音頻道。", ephemeral=True)
            return
    
    await interaction.response.defer(ephemeral=False)
    await request_queue.put((interaction, youtube_playlist_url, True, False))

@bot.slash_command(name='skip', description="跳過當前歌曲")
async def skip(interaction: nextcord.Interaction):
    await interaction.response.defer(ephemeral=True)
    vc = interaction.guild.voice_client
    
    if not vc:
        await interaction.followup.send("我沒有連線到任何語音頻道。", ephemeral=True)
        return

    if not song_queue and not vc.is_playing():
        await interaction.followup.send("目前沒有歌曲在播放或佇列中。", ephemeral=True)
        return

    if vc.is_playing():
        vc.stop()
    else:
        await play_next_in_queue(interaction)
    
    await interaction.followup.send("已跳過。", ephemeral=True)

@bot.slash_command(name='skip_all', description="清空所有歌曲並跳過當前歌曲")
async def skip_all(interaction):
    global song_queue, history_queue, current_song
    song_queue.clear()
    history_queue.clear()
    current_song = None
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
    await interaction.response.send_message("佇列已清空。", ephemeral=True)

@bot.slash_command(name='panel', description="顯示音樂控制面板")
async def panel(interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing() and current_song:
        await update_music_panel(
            interaction,
            title=current_song['title'],
            duration=current_song['duration'],
            webpage_url=current_song['webpage_url'],
            thumbnail=current_song['thumbnail']
        )
        await interaction.response.send_message("音樂控制面板已顯示。", ephemeral=True)
    else:
        await interaction.response.send_message("目前沒有歌曲在播放。", ephemeral=True)

@bot.slash_command(name='ping', description="檢查機器人延遲")
async def ping(interaction):
    await interaction.response.send_message(f"Pong! {bot.latency * 1000:.2f}ms", ephemeral=True)
    
@bot.slash_command(name='queue_status', description="查看待處理和播放佇列狀態")
async def queue_status(interaction):
    """
    查看背景任務處理佇列和準備播放佇列的狀態
    """
    request_queue_size = request_queue.qsize()
    song_queue_size = len(song_queue)
    
    embed = Embed(
        title="佇列狀態",
        description="以下是待處理和播放佇列的即時狀態。",
        color=nextcord.Color.blue()
    )
    embed.add_field(name="待處理請求數", value=f"{request_queue_size} 個", inline=False)
    embed.add_field(name="準備播放歌曲數", value=f"{song_queue_size} 首", inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.slash_command(name="raffle", description="舉辦一個抽獎活動！")
async def raffle(interaction: nextcord.Interaction, 
                 item: str, 
                 winners: int = 1, 
                 duration_days: int = 0,
                 duration_hours: int = 0,
                 duration_minutes: int = 0,
                 duration_seconds: int = 30):
    """
    舉辦一個抽獎活動
    Parameters:
    ----------
    item:
        抽獎的獎品
    winners:
        要抽出多少位獲獎者
    duration_days:
        抽獎持續的天數
    duration_hours:
        抽獎持續的小時數
    duration_minutes:
        抽獎持續的分鐘數
    duration_seconds:
        抽獎持續的秒數
    """
    global raffle_in_progress, raffle_message, raffle_entries
    
    if raffle_in_progress:
        await interaction.response.send_message("目前已有抽獎正在進行中！", ephemeral=True)
        return

    if winners <= 0:
        await interaction.response.send_message("獲獎者人數必須大於 0。", ephemeral=True)
        return

    total_duration_seconds = (duration_days * 86400) + (duration_hours * 3600) + \
                             (duration_minutes * 60) + duration_seconds
    
    if total_duration_seconds <= 0:
        await interaction.response.send_message("抽獎持續時間必須大於 0。", ephemeral=True)
        return

    raffle_in_progress = True
    raffle_entries.clear()

    embed = Embed(
        title=f"🎉 {item} 抽獎活動！🎉",
        description=f"點擊下面的按鈕來參加抽獎！\n\n**獲獎人數：** {winners} 位",
        color=nextcord.Color.blue()
    )
    embed.add_field(name="倒數計時", value=format_duration_extended(total_duration_seconds), inline=False)
    embed.set_footer(text="祝您好運！")

    raffle_message = await interaction.channel.send(embed=embed, view=RaffleView(item, winners))
    await interaction.response.send_message("抽獎已開始！", ephemeral=True)

    remaining_time = total_duration_seconds
    while remaining_time > 0:
        if not raffle_in_progress:
            return
        
        update_interval = min(5, remaining_time)
        await asyncio.sleep(update_interval)
        remaining_time -= update_interval

        embed.set_field_at(0, name="倒數計時", value=format_duration_extended(remaining_time), inline=False)
        try:
            await raffle_message.edit(embed=embed)
        except nextcord.NotFound:
            return
    
    await draw_winners(interaction, item, winners)


bot.run(DISCORD_BOT_TOKEN)
