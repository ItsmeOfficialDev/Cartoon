import os
import re
import asyncio
import json
from datetime import datetime
from pathlib import Path
import logging
import sys

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Fix for Python 3.14 event loop issue
if sys.version_info >= (3, 14):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

# Now import pyrogram
from pyrogram import Client, filters
from pyrogram.types import Message
import yt_dlp

# Bot configuration
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID"))  # Single channel for all uploads

# Initialize bot
app = Client("yt_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Store current downloads
current_downloads = {}

# Download YouTube video
async def download_video(url, download_path):
    """Download a single YouTube video"""
    ydl_opts = {
        'format': 'best[height<=1080][ext=mp4]/best[height<=1080]',
        'outtmpl': f'{download_path}/%(title)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'concurrent_fragment_downloads': 5,
        'retries': 3,
        'ignoreerrors': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # Find actual file if extension changed
            if not os.path.exists(filename):
                base = os.path.splitext(filename)[0]
                for ext in ['.mp4', '.mkv', '.webm']:
                    if os.path.exists(base + ext):
                        filename = base + ext
                        break
            
            return {
                'path': filename,
                'title': info.get('title', 'Video'),
                'duration': info.get('duration', 0)
            }
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None

# Start command
@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply_text(
        "🎬 **YouTube Playlist Downloader Bot**\n\n"
        "Send me a YouTube playlist URL and I'll download all videos to the channel."
    )

# Help command
@app.on_message(filters.command("help"))
async def help_cmd(client, message):
    await message.reply_text(
        "📥 **How to use:**\n\n"
        "1. Send a YouTube playlist URL\n"
        "2. Bot downloads all videos\n"
        "3. Videos are uploaded to the channel\n\n"
        "Example:\n"
        "`https://www.youtube.com/playlist?list=...`"
    )

# Message handler for URLs
@app.on_message(filters.private & filters.text)
async def handle_url(client, message):
    url = message.text.strip()
    user_id = message.from_user.id
    
    # Check if it's a YouTube URL
    if "youtube.com" not in url and "youtu.be" not in url:
        await message.reply_text("❌ Please send a valid YouTube URL!")
        return
    
    # Check if user already has a download running
    if user_id in current_downloads:
        await message.reply_text("⏳ You already have a download in progress. Please wait.")
        return
    
    status_msg = await message.reply_text("🔍 Fetching playlist information...")
    
    try:
        # Get playlist info first
        ydl_opts = {'quiet': True, 'extract_flat': True, 'ignoreerrors': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Check if it's a playlist
            if 'entries' not in info:
                # Single video
                videos = [info]
                playlist_title = "Single Video"
            else:
                # Playlist
                videos = [v for v in info['entries'] if v]
                playlist_title = info.get('title', 'Playlist')
            
            total = len(videos)
            
            if total == 0:
                await status_msg.edit("❌ No videos found!")
                return
            
            await status_msg.edit(f"📥 Found {total} videos. Starting download...")
            
            # Create download folder
            folder = f"downloads/{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            os.makedirs(folder, exist_ok=True)
            
            # Track progress
            current_downloads[user_id] = {
                'total': total,
                'completed': 0,
                'failed': 0
            }
            
            success = 0
            failed = 0
            
            for idx, video in enumerate(videos, 1):
                # Check if user stopped
                if user_id in current_downloads and current_downloads[user_id].get('stopped'):
                    await status_msg.edit("⏹ Download stopped by user.")
                    break
                
                # Get video URL
                if 'entries' in info:
                    video_url = f"https://youtube.com/watch?v={video['id']}"
                else:
                    video_url = url  # Single video
                
                # Update status
                await status_msg.edit(
                    f"📥 Downloading {idx}/{total}\n"
                    f"✅ Done: {success} | ❌ Failed: {failed}"
                )
                
                # Download video
                result = await download_video(video_url, folder)
                
                if result and os.path.exists(result['path']):
                    # Upload to channel with just the title
                    try:
                        caption = f"🎬 {result['title']}"
                        
                        await client.send_video(
                            CHANNEL_ID,
                            result['path'],
                            caption=caption,
                            supports_streaming=True
                        )
                        
                        success += 1
                        
                        # Update progress
                        if user_id in current_downloads:
                            current_downloads[user_id]['completed'] = success
                        
                        # Delete local file
                        try:
                            os.remove(result['path'])
                        except:
                            pass
                        
                    except Exception as e:
                        failed += 1
                        logger.error(f"Upload error: {e}")
                else:
                    failed += 1
                
                # Small delay to avoid flooding
                await asyncio.sleep(1)
                
                # If single video, break after first
                if 'entries' not in info:
                    break
            
            # Cleanup folder
            try:
                shutil.rmtree(folder)
            except:
                pass
            
            # Final status
            await status_msg.edit(
                f"✅ **Complete!**\n\n"
                f"Total: {total}\n"
                f"✅ Uploaded: {success}\n"
                f"❌ Failed: {failed}"
            )
            
            # Remove from current downloads
            if user_id in current_downloads:
                del current_downloads[user_id]
    
    except Exception as e:
        await status_msg.edit(f"❌ Error: {str(e)[:200]}")
        if user_id in current_downloads:
            del current_downloads[user_id]

# Stop command
@app.on_message(filters.command("stop"))
async def stop_download(client, message):
    user_id = message.from_user.id
    if user_id in current_downloads:
        current_downloads[user_id]['stopped'] = True
        await message.reply_text("⏹ Stopping download after current video...")
    else:
        await message.reply_text("❌ No active download to stop.")

# Status command
@app.on_message(filters.command("status"))
async def check_status(client, message):
    user_id = message.from_user.id
    if user_id in current_downloads:
        d = current_downloads[user_id]
        await message.reply_text(
            f"📊 **Download Status:**\n\n"
            f"Total: {d['total']}\n"
            f"Completed: {d['completed']}\n"
            f"Failed: {d['failed']}"
        )
    else:
        await message.reply_text("ℹ️ No active download.")

# Keep bot alive with simple ping
async def keep_alive():
    while True:
        await asyncio.sleep(300)  # 5 minutes
        logger.info("Bot is alive")

# Main
if __name__ == "__main__":
    # Create downloads folder if it doesn't exist
    os.makedirs("downloads", exist_ok=True)
    
    # Start keep alive task
    loop = asyncio.get_event_loop()
    loop.create_task(keep_alive())
    
    print("🎬 YouTube Playlist Downloader Bot Started!")
    print(f"📤 Uploading to channel: {CHANNEL_ID}")
    print("✅ Ready to receive URLs!")
    
    app.run()
