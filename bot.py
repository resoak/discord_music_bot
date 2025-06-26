import os
import logging
import asyncio
from collections import deque
import nextcord
from nextcord.ext import commands
from yt_dlp import YoutubeDL
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from dotenv import load_dotenv


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()
FFMPEG_PATH = os.getenv('FFMPEG_PATH')
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
    'executable': FFMPEG_PATH
}
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'cachedir': False,
}


DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')


spotify = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=SPOTIPY_CLIENT_ID, client_secret=SPOTIPY_CLIENT_SECRET))
ytdl = YoutubeDL(YTDL_OPTIONS)


intents = nextcord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
song_queue = deque()


class YTDLSource(nextcord.PCMVolumeTransformer):
    def __init__(self, source, *, data):
        super().__init__(source)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, stream=False):
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        if 'entries' in info:
            info = info['entries'][0]
        return cls(nextcord.FFmpegPCMAudio(info['url'], **FFMPEG_OPTIONS), data=info)


def is_spotify_playlist(url):
    return 'playlist' in url

def get_tracks_from_playlist(playlist_url):
    playlist_id = playlist_url.split('/')[-1].split('?')[0]
    tracks = []
    try:
        results = spotify.playlist_tracks(playlist_id)
        while results:
            tracks.extend([track['track']['external_urls']['spotify'] for track in results['items']])
            results = spotify.next(results) if results['next'] else None
    except Exception as e:
        logging.error(f"Error fetching tracks: {e}")
    return tracks

async def get_youtube_url_from_spotify(track_url):
    try:
        track_info = spotify.track(track_url)
        query = f"{track_info['name']} {track_info['artists'][0]['name']}"
        results = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(f"ytsearch:{query}", download=False))
        if results.get('entries'):
            return results['entries'][0]['webpage_url']
    except Exception as e:
        logging.error(f"YouTube search failed: {e}")
    return None

async def play_next(interaction):
    if song_queue:
        voice = interaction.guild.voice_client
        if voice and not voice.is_playing():
            source = song_queue.popleft()
            voice.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(interaction), bot.loop))
            await interaction.followup.send(f"Now playing: {source.title}")


@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user.name} ({bot.user.id})")

@bot.event
async def on_application_command_error(interaction, error):
    logging.error(f"Command error: {error}")
    if not interaction.response.is_done():
        await interaction.response.send_message("An error occurred.")


@bot.slash_command(name='join')
async def join(interaction):
    if interaction.user.voice:
        await interaction.user.voice.channel.connect()
        await interaction.response.send_message("Joined voice channel.")
    else:
        await interaction.response.send_message("You must be in a voice channel.")

@bot.slash_command(name='leave')
async def leave(interaction):
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("Disconnected.")
    else:
        await interaction.response.send_message("Not connected.")

@bot.slash_command(name='play_spotify')
async def play_spotify(interaction: nextcord.Interaction, spotify_url: str):
    try:
        voice_client = interaction.guild.voice_client
        if not voice_client:
            if interaction.user.voice:
                voice_client = await interaction.user.voice.channel.connect()
            else:
                await interaction.response.send_message("You are not in a voice channel.")
                return

        await interaction.response.defer()
        await interaction.followup.send("Processing your request...")

        tracks = get_tracks_from_playlist(spotify_url) if is_spotify_playlist(spotify_url) else [spotify_url]
        if not tracks:
            await interaction.followup.send("No tracks found.")
            return

        for track in tracks:
            youtube_url = await get_youtube_url_from_spotify(track)
            if youtube_url:
                source = await YTDLSource.from_url(youtube_url, stream=True)
                song_queue.append(source)
                await interaction.followup.send("Added the song into the queue")

        if not voice_client.is_playing():
            await play_next(interaction)

    except Exception as e:
        logging.error(f"Error in play_spotify: {e}")
        if not interaction.response.is_done():
            await interaction.response.send_message(f"Error: {e}")
        else:
            await interaction.followup.send(f"Error: {e}")

@bot.slash_command(name='play_youtube', description="Play a YouTube video from a URL")
async def play_youtube(interaction: nextcord.Interaction, youtube_url: str):
    try:
        voice_client = interaction.guild.voice_client
        if not voice_client:
            if interaction.user.voice:
                channel = interaction.user.voice.channel
                voice_client = await channel.connect()
            else:
                await interaction.response.send_message("You are not connected to a voice channel.")
                return

        await interaction.response.defer()
        await interaction.followup.send("Processing your YouTube link...")

        info = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(youtube_url, download=False))
        if 'entries' in info:
            info = info['entries'][0]

        player = nextcord.FFmpegPCMAudio(info['url'], **FFMPEG_OPTIONS)
        song_queue.append(YTDLSource(player, data=info))
        await interaction.followup.send(f"Added to queue: {info['title']}")

        if not voice_client.is_playing():
            await play_next(interaction)

    except Exception as e:
        logging.error(f"Error in play_youtube command: {str(e)}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f'An error occurred: {str(e)}')
            else:
                await interaction.followup.send(f'An error occurred: {str(e)}')
        except Exception as inner_e:
            logging.error(f"Failed to send error message: {inner_e}")

@bot.slash_command(name='skip')
async def skip(interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("Skipped.")
    else:
        await interaction.response.send_message("Nothing is playing.")

@bot.slash_command(name='play_youtube_playlist', description="Play all songs from a YouTube playlist")
async def play_youtube_playlist(interaction: nextcord.Interaction, youtube_playlist_url: str):
    try:
        voice_client = interaction.guild.voice_client
        if not voice_client:
            if interaction.user.voice:
                channel = interaction.user.voice.channel
                voice_client = await channel.connect()
            else:
                await interaction.response.send_message("You are not connected to a voice channel.")
                return

        await interaction.response.defer()
        await interaction.followup.send("Processing YouTube playlist...")

        info = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(youtube_playlist_url, download=False))

        if 'entries' not in info:
            await interaction.followup.send("No entries found in the playlist.")
            return

        entries = info['entries']
        added = 0
        for entry in entries:
            if entry is None:
                continue
            player = nextcord.FFmpegPCMAudio(entry['url'], **FFMPEG_OPTIONS)
            song_queue.append(YTDLSource(player, data=entry))
            added += 1
            await interaction.followup.send(f"Added to queue: {entry['title']}")

        if added == 0:
            await interaction.followup.send("No valid videos were found in the playlist.")
        elif not voice_client.is_playing():
            await play_next(interaction)

    except Exception as e:
        logging.error(f"Error in play_youtube_playlist command: {str(e)}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f'An error occurred: {str(e)}')
            else:
                await interaction.followup.send(f'An error occurred: {str(e)}')
        except Exception as inner_e:
            logging.error(f"Failed to send error message: {inner_e}")

@bot.slash_command(name='skip_all')
async def skip_all(interaction):
    song_queue.clear()
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
    await interaction.response.send_message("Queue cleared.")

@bot.slash_command(name='ping')
async def ping(interaction):
    await interaction.response.send_message(f"Pong! {bot.latency * 1000:.2f}ms")

bot.run(DISCORD_BOT_TOKEN)
