#!/usr/bin/env python3
"""
Cartoon Database Bot - Complete Version
"""

import os
import re
import json
import sqlite3
import asyncio
import subprocess
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
DATA_DIR = "/data" if os.path.exists("/data") else "bot_data"

# Setup
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(f"{DATA_DIR}/downloads", exist_ok=True)
os.makedirs(f"{DATA_DIR}/thumbnails", exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database
conn = sqlite3.connect(f"{DATA_DIR}/cartoons.db", check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS cartoons
             (name TEXT PRIMARY KEY, channel TEXT, thumbnail TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS processed
             (url TEXT PRIMARY KEY, series TEXT, date TIMESTAMP)''')
conn.commit()

# Processing state
processing = False
stop_requested = False

def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📥 Download YouTube", callback_data="menu_dl")],
        [InlineKeyboardButton("🔄 Forward Channel", callback_data="menu_forward")],
        [InlineKeyboardButton("📋 My Cartoons", callback_data="menu_list")],
        [InlineKeyboardButton("⏹ Stop Current", callback_data="menu_stop")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cartoons_keyboard():
    c.execute("SELECT name, channel FROM cartoons ORDER BY name")
    cartoons = c.fetchall()
    keyboard = []
    for name, channel in cartoons:
        keyboard.append([InlineKeyboardButton(f"{name} ({channel})", callback_data=f"cartoon_{name}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text("🎬 Cartoon Database Bot", reply_markup=get_main_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "menu_dl":
        context.user_data['action'] = 'waiting_dl_url'
        await query.edit_message_text("Send me YouTube URL and cartoon name:\nExample: https://youtube.com/... Tom & Jerry", reply_markup=get_main_keyboard())
    
    elif data == "menu_forward":
        context.user_data['action'] = 'waiting_channel'
        await query.edit_message_text("Send me channel ID and cartoon name:\nExample: @channel_id Tom & Jerry", reply_markup=get_main_keyboard())
    
    elif data == "menu_list":
        keyboard = get_cartoons_keyboard()
        await query.edit_message_text("Your Cartoons:", reply_markup=keyboard)
    
    elif data == "menu_stop":
        global stop_requested
        stop_requested = True
        await query.edit_message_text("⏹ Stop signal sent", reply_markup=get_main_keyboard())
    
    elif data.startswith("cartoon_"):
        name = data[8:]
        c.execute("SELECT channel, thumbnail FROM cartoons WHERE name = ?", (name,))
        result = c.fetchone()
        if not result:
            await query.edit_message_text("Cartoon not found", reply_markup=get_main_keyboard())
            return
        channel, thumb = result
        keyboard = [
            [InlineKeyboardButton("🖼 Set Thumbnail", callback_data=f"thumb_{name}")],
            [InlineKeyboardButton("❌ Delete", callback_data=f"delete_{name}")],
            [InlineKeyboardButton("🔙 Back", callback_data="menu_list")]
        ]
        await query.edit_message_text(f"📺 {name}\nChannel: {channel}\nThumbnail: {'✅' if thumb else '❌'}", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("thumb_"):
        name = data[6:]
        context.user_data['thumb_for'] = name
        await query.edit_message_text(f"Send me the thumbnail image for {name}", reply_markup=get_main_keyboard())
    
    elif data.startswith("delete_"):
        name = data[7:]
        c.execute("DELETE FROM cartoons WHERE name = ?", (name,))
        conn.commit()
        # Also delete thumbnail file
        thumb_path = f"{DATA_DIR}/thumbnails/{name}.jpg"
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        await query.edit_message_text(f"✅ Deleted {name}", reply_markup=get_main_keyboard())
    
    elif data == "back_main":
        await query.edit_message_text("Main Menu:", reply_markup=get_main_keyboard())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    
    text = update.message.text
    action = context.user_data.get('action')
    
    if action == 'waiting_dl_url':
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("❌ Send: URL CartoonName", reply_markup=get_main_keyboard())
            return
        url, name = parts
        context.user_data['dl_url'] = url
        context.user_data['dl_name'] = name
        context.user_data['action'] = 'waiting_dl_channel'
        await update.message.reply_text(f"Which channel for {name}? Send channel username (e.g., @channel)")
    
    elif action == 'waiting_dl_channel':
        channel = text.strip()
        url = context.user_data.get('dl_url')
        name = context.user_data.get('dl_name')
        
        if not url or not name:
            await update.message.reply_text("❌ Something went wrong, start over", reply_markup=get_main_keyboard())
            context.user_data.clear()
            return
        
        # Save cartoon
        c.execute("INSERT OR REPLACE INTO cartoons (name, channel, thumbnail) VALUES (?, ?, ?)",
                 (name, channel, None))
        conn.commit()
        
        # Start download
        await update.message.reply_text(f"🔄 Starting download of {name} to {channel}")
        asyncio.create_task(download_playlist(url, name, channel, update))
        context.user_data.clear()
    
    elif action == 'waiting_channel':
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("❌ Send: channel_id CartoonName", reply_markup=get_main_keyboard())
            return
        channel, name = parts
        
        # Save cartoon
        c.execute("INSERT OR REPLACE INTO cartoons (name, channel, thumbnail) VALUES (?, ?, ?)",
                 (name, channel, None))
        conn.commit()
        
        # Start forwarding
        await update.message.reply_text(f"🔄 Starting forward from {channel}")
        asyncio.create_task(forward_channel(channel, name, update))
        context.user_data.clear()
    
    elif update.message.photo and 'thumb_for' in context.user_data:
        name = context.user_data['thumb_for']
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        thumb_path = f"{DATA_DIR}/thumbnails/{name}.jpg"
        await file.download_to_drive(thumb_path)
        
        c.execute("UPDATE cartoons SET thumbnail = ? WHERE name = ?", (thumb_path, name))
        conn.commit()
        
        await update.message.reply_text(f"✅ Thumbnail saved for {name}", reply_markup=get_main_keyboard())
        context.user_data.clear()

def extract_episode(title):
    if not title:
        return None, "Cartoon"
    
    patterns = [
        (r'[Ee]p(?:isode)?[.\s]*(\d+)', r'[Ee]p(?:isode)?[.\s]*\d+'),
        (r'[Ee](\d+)', r'[Ee]\d+'),
        (r'[Ss]\d+[.\s]*[Ee](\d+)', r'[Ss]\d+[.\s]*[Ee]\d+'),
        (r'(\d+)[.\s]*-', r'\d+[.\s]*-'),
        (r'-\s*0*(\d+)', r'-\s*0*\d+'),
    ]
    for pattern, remove in patterns:
        match = re.search(pattern, title)
        if match:
            ep = match.group(1).lstrip('0') or '1'
            clean = re.sub(remove, '', title, flags=re.IGNORECASE).strip()
            clean = re.sub(r'\s+', ' ', clean)
            clean = re.sub(r'[\[\]\(\)\-_\s]+$', '', clean)
            return ep, clean
    return None, title

async def download_playlist(url, name, channel, update):
    global processing, stop_requested
    processing = True
    stop_requested = False
    
    try:
        # Get thumbnail
        thumb = None
        c.execute("SELECT thumbnail FROM cartoons WHERE name = ?", (name,))
        result = c.fetchone()
        if result and result[0] and os.path.exists(result[0]):
            thumb = result[0]
        
        ydl_opts = {
            'format': 'best[height<=1080][ext=mp4]/best[height<=1080]',
            'outtmpl': f'{DATA_DIR}/downloads/%(title)s.%(ext)s',
            'quiet': True,
            'ignoreerrors': True,
            'retries': 3,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'entries' in info:
                total = len(info['entries'])
                await update.message.reply_text(f"📋 Found {total} videos in playlist")
                
                for i, entry in enumerate(info['entries']):
                    if stop_requested:
                        await update.message.reply_text("⏹ Stopped by user")
                        break
                    
                    if not entry:
                        continue
                    
                    video_url = entry['webpage_url']
                    video_title = entry.get('title', 'Unknown')
                    
                    # Check if processed
                    c.execute("SELECT 1 FROM processed WHERE url = ?", (video_url,))
                    if c.fetchone():
                        continue
                    
                    ep, clean = extract_episode(video_title)
                    if not ep:
                        ep = str(i + 1)
                    
                    await update.message.reply_text(f"⬇️ Downloading {i+1}/{total}: {video_title[:30]}...")
                    
                    try:
                        # Download
                        ydl.extract_info(video_url, download=True)
                        
                        # Find downloaded file
                        file = None
                        for f in os.listdir(f"{DATA_DIR}/downloads"):
                            if video_title[:30] in f and f.endswith('.mp4'):
                                file = f"{DATA_DIR}/downloads/{f}"
                                break
                        
                        if file and os.path.exists(file):
                            # Get duration
                            duration = 0
                            try:
                                result = subprocess.run(
                                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', 
                                     '-of', 'default=noprint_wrappers=1:nokey=1', file],
                                    capture_output=True, text=True, timeout=10
                                )
                                if result.stdout.strip():
                                    duration = int(float(result.stdout.strip()) // 60)
                            except:
                                pass
                            
                            # Upload
                            caption = f"🎬 Episode {ep} – {clean}\n📺 Series: {name}\n🕒 Duration: {duration} min\n🎞 Quality: HD"
                            
                            with open(file, 'rb') as video:
                                if thumb and os.path.exists(thumb):
                                    with open(thumb, 'rb') as t:
                                        await update.message.bot.send_video(
                                            chat_id=channel, 
                                            video=video, 
                                            caption=caption, 
                                            thumb=t,
                                            supports_streaming=True,
                                            read_timeout=300,
                                            write_timeout=300
                                        )
                                else:
                                    await update.message.bot.send_video(
                                        chat_id=channel, 
                                        video=video, 
                                        caption=caption, 
                                        supports_streaming=True,
                                        read_timeout=300,
                                        write_timeout=300
                                    )
                            
                            # Mark processed
                            c.execute("INSERT INTO processed VALUES (?, ?, ?)", 
                                     (video_url, name, datetime.now()))
                            conn.commit()
                            
                            # Cleanup
                            try:
                                os.remove(file)
                            except:
                                pass
                            
                    except Exception as e:
                        logger.error(f"Error processing video: {e}")
                        continue
                
                await update.message.reply_text(f"✅ Completed! Processed {name}")
            else:
                await update.message.reply_text("❌ Not a playlist or no videos found")
    
    except Exception as e:
        logger.error(f"Playlist error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
    
    finally:
        processing = False
        stop_requested = False

async def forward_channel(channel_id, name, update):
    global processing, stop_requested
    processing = True
    stop_requested = False
    
    try:
        # Get recent messages
        messages = []
        async for msg in update.message.bot.get_chat_history(chat_id=channel_id, limit=50):
            messages.append(msg)
        
        total = len(messages)
        await update.message.reply_text(f"📋 Found {total} recent messages")
        
        forwarded = 0
        for msg in messages:
            if stop_requested:
                await update.message.reply_text("⏹ Stopped by user")
                break
            
            # Check if has video
            has_video = False
            file_id = None
            
            if msg.video:
                has_video = True
                file_id = msg.video.file_id
            elif msg.document and msg.document.mime_type and 'video' in msg.document.mime_type:
                has_video = True
                file_id = msg.document.file_id
            
            if has_video and file_id:
                # Extract episode from caption
                ep, clean = extract_episode(msg.caption or "")
                if not ep:
                    ep = str(forwarded + 1)
                
                caption = f"🎬 Episode {ep} – {clean or 'Cartoon'}\n📺 Series: {name}\n🕒 Duration: 0 min\n🎞 Quality: HD"
                
                # Forward
                await update.message.bot.send_video(
                    chat_id=channel_id,
                    video=file_id,
                    caption=caption,
                    supports_streaming=True
                )
                forwarded += 1
        
        await update.message.reply_text(f"✅ Forwarded {forwarded} videos to {channel_id}")
    
    except Exception as e:
        logger.error(f"Channel error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
    
    finally:
        processing = False
        stop_requested = False

def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN not set")
        return
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))
    
    print("🤖 Cartoon Database Bot Started")
    app.run_polling()

if __name__ == "__main__":
    main()
