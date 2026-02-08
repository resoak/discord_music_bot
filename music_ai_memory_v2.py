import os
import asyncio
import logging
import uuid
import requests
import warnings
from datetime import datetime
from collections import deque
from typing import Dict, Optional

# 將 Nextcord 替換為 Disnake
import disnake
from disnake.ext import commands, tasks
from disnake import Embed, ApplicationCommandInteraction
from qdrant_client import QdrantClient, models
from yt_dlp import YoutubeDL
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

# --- 0. 系統初始化 ---
load_dotenv()
warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger("MegaBot")

# --- 1. 全域配置 ---
CONFIG = {
    "TOKEN": os.getenv('DISCORD_BOT_TOKEN'),
    "FFMPEG": os.getenv('FFMPEG_PATH', 'ffmpeg'),
    "QDRANT": os.getenv('QDRANT_URL', "http://localhost:6333"),
    "EMBED_API": "https://ws-04.wade0426.me/embed",
    "LLM_API": "https://ws-02.wade0426.me/v1",
    "MEMORY_COLLECTION": "mega_bot_memory_v2026_final",
}

FFMPEG_OPTS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

# --- 2. 外部服務初始化 ---
class ServiceManager:
    def __init__(self):
        self.ytdl = YoutubeDL({'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True})
        self.qdrant = QdrantClient(url=CONFIG["QDRANT"])
        self.llm = ChatOpenAI(
            base_url=CONFIG["LLM_API"],
            api_key="none",
            model="google/gemma-3-27b-it",
            temperature=0
        )
        try:
            auth = SpotifyClientCredentials(
                client_id=os.getenv('SPOTIPY_CLIENT_ID'), 
                client_secret=os.getenv('SPOTIPY_CLIENT_SECRET')
            )
            self.spotify = spotipy.Spotify(auth_manager=auth)
        except: self.spotify = None

services = ServiceManager()

# --- 3. 語音與播放隊列狀態 ---
class VoiceState:
    def __init__(self):
        self.queue = deque()
        self.current = None

# --- 4. 機器人核心類別 (使用 Disnake) ---
class MegaBot(commands.InteractionBot):
    def __init__(self):
        # Disnake 預設啟用更多底層優化
        super().__init__(intents=disnake.Intents.all())
        self.states: Dict[int, VoiceState] = {}
        self.request_queue = asyncio.Queue()

    async def get_embedding(self, text):
        try:
            r = requests.post(CONFIG["EMBED_API"], json={"texts": [text], "normalize": True}, timeout=3)
            return r.json()['embeddings'][0]
        except Exception as e:
            logger.error(f"Embedding 錯誤: {e}")
            return None

    async def on_ready(self):
        if not self.worker_task.is_running():
            self.worker_task.start()
        logger.info(f"🚀 Disnake 穩定版已啟動：{self.user}")

    async def ensure_voice(self, inter: ApplicationCommandInteraction) -> Optional[disnake.VoiceClient]:
        """
        使用 Disnake 內建的 v8 協議處理機制。
        """
        if not inter.author.voice:
            await inter.edit_original_message(content="❌ 你必須先進入語音頻道！")
            return None

        target_channel = inter.author.voice.channel
        
        # 如果已經在其他頻道，先移動過去
        if inter.guild.voice_client:
            if inter.guild.voice_client.channel.id != target_channel.id:
                await inter.guild.voice_client.move_to(target_channel)
            return inter.guild.voice_client

        try:
            logger.info(f"正在嘗試連線至 {target_channel.name}...")
            # Disnake 處理了 4006 與 IP Discovery Bug，直接 connect 即可
            vc = await target_channel.connect(timeout=20.0, reconnect=True)
            return vc
        except Exception as e:
            logger.error(f"語音連線失敗: {e}")
            await inter.edit_original_message(content="⚠️ 語音連線失敗，可能是 Discord 節點問題。")
            return None

    @tasks.loop(seconds=1)
    async def worker_task(self):
        if self.request_queue.empty(): return
        inter, query = await self.request_queue.get()
        gid = inter.guild.id
        if gid not in self.states: self.states[gid] = VoiceState()
        
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, lambda: services.ytdl.extract_info(f"ytsearch:{query}", download=False))
            if 'entries' in data: data = data['entries'][0]
            
            self.states[gid].queue.append(data)
            vc = inter.guild.voice_client
            if vc and not vc.is_playing():
                await self.play_next(gid, inter.channel)
            await inter.channel.send(f"✅ **{data.get('title')}** 加入隊列")
        except Exception as e:
            logger.error(f"解析錯誤: {e}")
        finally:
            self.request_queue.task_done()

    async def play_next(self, gid, channel):
        state = self.states[gid]
        if not state.queue: return
        vc = self.get_guild(gid).voice_client
        if not vc: return

        track = state.queue.popleft()
        
        try:
            res = await services.llm.ainvoke([
                SystemMessage(content="你是專業DJ。用10字內介紹這首歌。"),
                HumanMessage(content=track.get('title'))
            ])
            comment = res.content
        except: comment = "Enjoy!"

        def after_playing(e):
            if e: logger.error(f"播放異常: {e}")
            self.loop.create_task(self.play_next(gid, channel))

        # Disnake 的 FFmpegPCMAudio 參數與 Nextcord 一致
        audio = disnake.FFmpegPCMAudio(track['url'], executable=CONFIG["FFMPEG"], **FFMPEG_OPTS)
        vc.play(audio, after=after_playing)
        
        await channel.send(embed=Embed(
            title="🎶 正在播放", 
            description=f"**{track.get('title')}**\n🎙️ AI DJ: *{comment}*", 
            color=0x1DB954
        ))

# --- 5. 指令定義 ---
bot = MegaBot()

@bot.slash_command(name="play", description="播放 YouTube 音樂")
async def play(inter: ApplicationCommandInteraction, query: str):
    await inter.response.defer()
    vc = await bot.ensure_voice(inter)
    if vc:
        await bot.request_queue.put((inter, query))
        await inter.edit_original_message(content=f"🔎 搜尋中：`{query}`")

@bot.slash_command(name="chat", description="AI 對話")
async def chat(inter: ApplicationCommandInteraction, message: str):
    await inter.response.defer()
    res = await services.llm.ainvoke([
        SystemMessage(content="你是具備記憶的助手。"),
        HumanMessage(content=message)
    ])
    await inter.edit_original_message(content=f"🤖 | {res.content}")

    async def _save():
        v = await bot.get_embedding(f"Q:{message} A:{res.content}")
        if v:
            services.qdrant.upsert(
                CONFIG["MEMORY_COLLECTION"], 
                points=[models.PointStruct(id=uuid.uuid4().hex, vector=v, payload={"m": f"Q:{message} A:{res.content}"})]
            )
    asyncio.create_task(_save())

if __name__ == "__main__":
    bot.run(CONFIG["TOKEN"])