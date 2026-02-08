import os
import re
import urllib.parse
import asyncio
import logging
import uuid
import requests
import warnings
import shutil
from datetime import datetime
from collections import deque
from typing import Dict, Optional, List

import disnake
from disnake.ext import commands, tasks
from disnake import Embed, ApplicationCommandInteraction, ButtonStyle
from disnake.ui import View, Button
from qdrant_client import QdrantClient, models
from yt_dlp import YoutubeDL
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_text_splitters import RecursiveCharacterTextSplitter

# --- 0. 配置與初始化 ---
load_dotenv()
warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger("MegaBot")

FFMPEG_PATH = shutil.which(os.getenv('FFMPEG_PATH', 'ffmpeg')) or 'ffmpeg'

CONFIG = {
    "TOKEN": os.getenv('DISCORD_BOT_TOKEN'),
    "QDRANT": os.getenv('QDRANT_URL', "http://localhost:6333"),
    "EMBED_API": os.getenv('EMBED_API'),
    "LLM_API": os.getenv('LLM_API'),
    "CHAT_COL": "mega_chat_v2026",
    "MUSIC_COL": "mega_music_v2026"
}

FFMPEG_OPTS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 10M -analyzeduration 10M',
    'options': '-vn -loglevel panic -map 0:a:0',
}

text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=600,
    chunk_overlap=60,
    separators=["\n\n", "\n", "。", "！", "？", " ", ""]
)

# --- 1. 精確化提示詞庫 ---
class PromptLibrary:
    @staticmethod
    def get_music_refine_prompt(user_query: str):
        return f"""[TASK] Convert user music request into ONE precise search keyword for YouTube.
[RULE] Output ONLY the keyword. No explanations. No quotes.
[USER REQUEST] {user_query}
[OUTPUT]"""

    @staticmethod
    def get_dj_commentary_prompt(track_title: str):
        return f"""[ROLE] You are MegaBot DJ, a futuristic AI.
[TASK] Write a 15-word cool intro for: {track_title}.
[STYLE] Cyberpunk, professional, direct. No greetings.
[OUTPUT]"""

    @staticmethod
    def get_chat_system_prompt(context: str):
        return f"""[ROLE] MegaBot 2026 AI.
[CONTEXT] {context}
[INSTRUCTION] 
1. Answer concisely and wittily based on memory.
2. Provide a DIRECT, CLICKABLE URL related to the topic:
   - If it's a YouTuber/Creator, provide their YouTube channel link.
   - If it's a product, provide a Shopee search link: https://shopee.tw/search?keyword=[KEYWORD]
   - If it's a general topic/person, provide a Wikipedia or Official site link.
3. IMPORTANT: Output the URL as RAW plain text (NO Markdown like [text](url)).
4. DO NOT provide a shopping link if the topic is not a product."""

# --- 2. 服務管理員 ---
class ServiceManager:
    def __init__(self):
        self.ytdl = YoutubeDL({
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'default_search': 'ytsearch',
            'extract_flat': False,
            'nocheckcertificate': True,
            'ignoreerrors': True,
            'format_sort': ['hasaud', 'ext', 'downloader_key'],
        })
        self.qdrant = QdrantClient(url=CONFIG["QDRANT"])
        
        self.llm = ChatOpenAI(
            base_url=CONFIG["LLM_API"], 
            api_key="none", 
            model="google/gemma-3-27b-it", 
            temperature=0
        )
        self.vector_dim = None

    async def probe_and_init(self):
        try:
            r = await asyncio.to_thread(requests.post, CONFIG["EMBED_API"], json={"texts": ["init"], "normalize": True}, timeout=5)
            self.vector_dim = len(r.json()['embeddings'][0])
        except Exception as e:
            logger.warning(f"Embedding API 異常: {e}")
            self.vector_dim = 4096
            
        for col in [CONFIG["CHAT_COL"], CONFIG["MUSIC_COL"]]:
            try:
                self.qdrant.get_collection(col)
            except:
                self.qdrant.create_collection(col, models.VectorParams(size=self.vector_dim, distance=models.Distance.COSINE))

services = ServiceManager()

# --- 3. 機器人核心組件 ---
class MusicView(View):
    def __init__(self, bot, gid):
        super().__init__(timeout=None)
        self.bot, self.gid = bot, gid

    @disnake.ui.button(label="⏮️ Previous", style=ButtonStyle.secondary)
    async def prev(self, _, inter): await inter.response.defer(); await self.bot.play_previous(self.gid)
    
    @disnake.ui.button(label="⏯️ Pause/Resume", style=ButtonStyle.success)
    async def pr(self, _, inter):
        vc = inter.guild.voice_client
        if vc: (vc.pause() if vc.is_playing() else vc.resume())
        await inter.response.defer()
        
    @disnake.ui.button(label="⏭️ Skip", style=ButtonStyle.primary)
    async def skip(self, _, inter): 
        if inter.guild.voice_client: inter.guild.voice_client.stop()
        await inter.response.defer()

class VoiceState:
    def __init__(self):
        self.queue = deque()
        self.history = deque(maxlen=20)
        self.current = None

class MegaBot(commands.InteractionBot):
    def __init__(self):
        super().__init__(intents=disnake.Intents.all())
        self.states: Dict[int, VoiceState] = {}
        self.req_queue = asyncio.Queue()

    async def on_ready(self):
        await services.probe_and_init()
        if not self.worker.is_running(): self.worker.start()
        logger.info(f"🚀 MegaBot 2026 核心啟動 | 全領域智慧連結模式")

    async def get_vec(self, text):
        try:
            r = await asyncio.to_thread(requests.post, CONFIG["EMBED_API"], json={"texts": [text], "normalize": True}, timeout=3)
            return r.json()['embeddings'][0]
        except: return None

    @tasks.loop(seconds=1)
    async def worker(self):
        if self.req_queue.empty(): return
        inter, raw_query = await self.req_queue.get()
        gid = inter.guild.id
        state = self.states.setdefault(gid, VoiceState())

        try:
            if raw_query.strip().startswith("http"):
                target, tag, refined_q = raw_query.strip(), "🔗 網址串流", "Direct Link"
            else:
                refine_res = await services.llm.ainvoke([HumanMessage(content=PromptLibrary.get_music_refine_prompt(raw_query))])
                v = await self.get_vec(raw_query)
                refined_q = refine_res.content.strip()
                target, tag = f"ytsearch:{refined_q}", "🔍 AI 搜尋"
                if v:
                    hits = services.qdrant.query_points(CONFIG["MUSIC_COL"], query=v, limit=1, score_threshold=0.88).points
                    if hits: target, tag = hits[0].payload['url'], "💎 記憶匹配"

            data = await self.loop.run_in_executor(None, lambda: services.ytdl.extract_info(target, download=False))
            entries = data.get('entries', [data]) if 'entries' in data else [data]
            
            for entry in entries:
                if not entry: continue
                comment_res = await services.llm.ainvoke([HumanMessage(content=PromptLibrary.get_dj_commentary_prompt(entry['title']))])
                track = {
                    'title': entry['title'], 
                    'url': entry['url'], 
                    'webpage_url': entry.get('webpage_url') or target, 
                    'dj_words': comment_res.content.strip()
                }
                state.queue.append(track)
                if tag == "🔍 AI 搜尋": asyncio.create_task(self._save_music(raw_query, track))

            if inter.guild.voice_client and not inter.guild.voice_client.is_playing():
                await self.play_next(gid, inter.channel)
            await inter.channel.send(f"📦 **{tag}** 已就緒: `{refined_q}`")
        except Exception as e: logger.error(f"Worker Error: {e}")
        finally: self.req_queue.task_done()

    async def play_next(self, gid, channel):
        state = self.states.get(gid)
        if not state or not state.queue: return
        vc = self.get_guild(gid).voice_client
        if not vc: return
        
        track = state.queue.popleft()
        if state.current: state.history.append(state.current)
        state.current = track

        source = disnake.FFmpegPCMAudio(track['url'], executable=FFMPEG_PATH, **FFMPEG_OPTS)
        vc.play(disnake.PCMVolumeTransformer(source, volume=0.8), after=lambda e: self.loop.create_task(self.play_next(gid, channel)))
        
        embed = Embed(title="📻 MegaBot On Air", description=f"🎵 **{track['title']}**", color=0x5865F2)
        embed.add_field(name="🎙️ AI DJ Commentary", value=f"*{track['dj_words']}*", inline=False)
        await channel.send(embed=embed, view=MusicView(self, gid))

    async def play_previous(self, gid):
        state = self.states.get(gid)
        if state and state.history:
            prev = state.history.pop()
            if state.current: state.queue.appendleft(state.current)
            state.queue.appendleft(prev)
            if self.get_guild(gid).voice_client: self.get_guild(gid).voice_client.stop()

    async def _save_music(self, q, t):
        v = await self.get_vec(f"{q} {t['title']}")
        if v: services.qdrant.upsert(CONFIG["MUSIC_COL"], points=[models.PointStruct(id=uuid.uuid4().hex, vector=v, payload={"title": t['title'], "url": t['webpage_url']})])

    async def _save_chat_memory(self, q, a):
        combined_text = f"Q:{q} A:{a}"
        chunks = text_splitter.split_text(combined_text)
        points = []
        for chunk in chunks:
            v = await self.get_vec(chunk)
            if v:
                points.append(models.PointStruct(
                    id=uuid.uuid4().hex, 
                    vector=v, 
                    payload={"m": chunk}
                ))
        if points:
            services.qdrant.upsert(CONFIG["CHAT_COL"], points=points)

# --- 4. 指令集 ---
bot = MegaBot()

@bot.slash_command(name="play", description="播放音樂 (連結或搜尋文字)")
async def play(inter: ApplicationCommandInteraction, query: str):
    await inter.response.defer()
    if not inter.author.voice: return await inter.edit_original_message(content="❌ 你必須先進入語音頻道")
    if not inter.guild.voice_client: await inter.author.voice.channel.connect()
    await bot.req_queue.put((inter, query))
    await inter.edit_original_message(content=f"🔍 接收請求: `{query[:50]}`")

@bot.slash_command(name="skip", description="跳過當前歌曲")
async def skip(inter: ApplicationCommandInteraction):
    if inter.guild.voice_client:
        inter.guild.voice_client.stop()
        await inter.response.send_message("⏭️ 已跳過當前歌曲")
    else: await inter.response.send_message("❌ 目前無播放內容", ephemeral=True)

@bot.slash_command(name="pause", description="暫停播放")
async def pause(inter: ApplicationCommandInteraction):
    if inter.guild.voice_client and inter.guild.voice_client.is_playing():
        inter.guild.voice_client.pause()
        await inter.response.send_message("⏸️ 已暫停")
    else: await inter.response.send_message("❌ 無法暫停", ephemeral=True)

@bot.slash_command(name="resume", description="恢復播放")
async def resume(inter: ApplicationCommandInteraction):
    if inter.guild.voice_client and inter.guild.voice_client.is_paused():
        inter.guild.voice_client.resume()
        await inter.response.send_message("▶️ 恢復播放")
    else: await inter.response.send_message("❌ 音樂並未暫停", ephemeral=True)

@bot.slash_command(name="queue", description="查看待播放隊列")
async def queue(inter: ApplicationCommandInteraction):
    state = bot.states.get(inter.guild.id)
    if not state or not (state.queue or state.current):
        return await inter.response.send_message("📭 目前隊列空空如也")
    q_list = "\n".join([f"**{i+1}.** {t['title']}" for i, t in enumerate(list(state.queue)[:10])])
    embed = Embed(title="📜 播放隊列", color=0x2ECC71)
    embed.add_field(name="Now Playing", value=state.current['title'], inline=False)
    embed.add_field(name="Up Next", value=q_list or "無後續歌曲", inline=False)
    await inter.response.send_message(embed=embed)

@bot.slash_command(name="history", description="查看最近播放紀錄")
async def history(inter: ApplicationCommandInteraction):
    state = bot.states.get(inter.guild.id)
    if not state or not state.history:
        return await inter.response.send_message("📜 尚無播放紀錄")
    h_list = "\n".join([f"• {t['title']}" for t in reversed(list(state.history))])
    await inter.response.send_message(embed=Embed(title="🕒 播放歷史", description=h_list, color=0x95A5A6))

@bot.slash_command(name="stop", description="停止播放並清空隊列")
async def stop(inter: ApplicationCommandInteraction):
    state = bot.states.get(inter.guild.id)
    if state: state.queue.clear()
    if inter.guild.voice_client:
        await inter.guild.voice_client.disconnect()
        await inter.response.send_message("⏹️ 停止播放並離開頻道")

@bot.slash_command(name="chat", description="AI 對話模式 (全領域連結導航)")
async def chat(inter: ApplicationCommandInteraction, message: str):
    await inter.response.defer()
    v = await bot.get_vec(message)
    context = ""
    if v:
        hits = services.qdrant.query_points(CONFIG["CHAT_COL"], query=v, limit=5).points
        context = "\n".join([h.payload['m'] for h in hits])
    
    res = await services.llm.ainvoke([
        SystemMessage(content=PromptLibrary.get_chat_system_prompt(context)), 
        HumanMessage(content=message)
    ])
    
    reply = res.content
    # 🔍 正則表達式抓取所有網址
    found_urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', reply)
    
    for url in found_urls:
        try:
            # 確保網址中的中文與特殊字元被編碼，這能解決 Discord 無法點擊的問題
            parsed = urllib.parse.urlparse(url)
            # 編碼 Path 與 Query
            safe_path = urllib.parse.quote(parsed.path)
            safe_query = urllib.parse.quote(parsed.query, safe='=&')
            
            safe_url = urllib.parse.urlunparse(
                parsed._replace(path=safe_path, query=safe_query)
            )
            reply = reply.replace(url, safe_url)
        except:
            continue

    await inter.edit_original_message(content=f"🤖 | {reply}")
    asyncio.create_task(bot._save_chat_memory(message, res.content))

if __name__ == "__main__":
    bot.run(CONFIG["TOKEN"])