#!/usr/bin/env python3
"""
CARTOON DATABASE BOT - COMPLETE WORKING VERSION
All features: YouTube downloads, channel forwarding, thumbnails, buttons
"""

import os
import re
import sys
import json
import time
import sqlite3
import asyncio
import logging
import subprocess
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Dict, List
import signal

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    CallbackQueryHandler, 
    MessageHandler, 
    filters, 
    ContextTypes
)

# YouTube downloader
import yt_dlp

# ==================== ENVIRONMENT VARIABLES ====================
# SET THESE IN RENDER OR DIRECTLY HERE:

# REQUIRED: Your bot token from @BotFather
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# REQUIRED: Your Telegram user ID (get from @userinfobot)
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

# OPTIONAL: Data directory (for Render persistent disk)
DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.exists("/data") else "bot_data")

# OPTIONAL: Max file size in MB (Telegram limit is 2000MB for Premium)
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", "2000"))

# OPTIONAL: Download quality (360, 480, 720, 1080)
VIDEO_QUALITY = os.environ.get("VIDEO_QUALITY", "1080")

# OPTIONAL: Concurrent downloads (1-3 recommended for Render free tier)
CONCURRENT_DOWNLOADS = int(os.environ.get("CONCURRENT_DOWNLOADS", "1"))

# ==================== SETUP DIRECTORIES ====================
print(f"Using data directory: {DATA_DIR}")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(f"{DATA_DIR}/downloads", exist_ok=True)
os.makedirs(f"{DATA_DIR}/thumbnails", exist_ok=True)
os.makedirs(f"{DATA_DIR}/temp", exist_ok=True)

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'{DATA_DIR}/bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('CartoonBot')

# ==================== DATABASE SETUP ====================
def init_database():
    """Initialize SQLite database with all required tables"""
    conn = sqlite3.connect(f"{DATA_DIR}/cartoons.db")
    c = conn.cursor()
    
    # Cartoons table
    c.execute('''CREATE TABLE IF NOT EXISTS cartoons
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT UNIQUE, 
                  channel TEXT NOT NULL,
                  thumbnail TEXT,
                  created TIMESTAMP)''')
    
    # Processed YouTube videos
    c.execute('''CREATE TABLE IF NOT EXISTS youtube_processed
                 (video_url TEXT PRIMARY KEY,
                  series TEXT,
                  episode TEXT,
                  title TEXT,
                  processed_date TIMESTAMP)''')
    
    # Processed forwarded messages
    c.execute('''CREATE TABLE IF NOT EXISTS forwarded_processed
                 (channel_id TEXT,
                  message_id INTEGER,
                  series TEXT,
                  processed_date TIMESTAMP,
                  PRIMARY KEY (channel_id, message_id))''')
    
    # Download queue
    c.execute('''CREATE TABLE IF NOT EXISTS download_queue
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  url TEXT UNIQUE,
                  series TEXT,
                  channel TEXT,
                  status TEXT,
                  added TIMESTAMP,
                  started TIMESTAMP,
                  completed TIMESTAMP)''')
    
    # Settings table
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized")

init_database()

# ==================== GLOBAL STATE ====================
class BotState:
    def __init__(self):
        self.processing = False
        self.stop_requested = False
        self.current_task = None
        self.current_series = None
        self.items_processed = 0
        self.total_items = 0
        self.start_time = None

bot_state = BotState()

# ==================== HELPER FUNCTIONS ====================

def get_db_connection():
    """Get database connection"""
    return sqlite3.connect(f"{DATA_DIR}/cartoons.db")

def extract_episode_info(title: str) -> Tuple[Optional[str], str]:
    """
    Extract episode number from title
    Returns: (episode_number, cleaned_title)
    """
    if not title:
        return None, "Cartoon"
    
    # Comprehensive patterns for episode detection
    patterns = [
        # Episode 1, Ep 1, Ep.1, EP1
        (r'[Ee]p(?:isode)?[.\s]*(\d+)', r'[Ee]p(?:isode)?[.\s]*\d+'),
        # E01, E1, e01
        (r'[Ee](\d+)', r'[Ee]\d+'),
        # S01E03, S1E3, s01e03
        (r'[Ss](\d+)[.\s]*[Ee](\d+)', r'[Ss]\d+[.\s]*[Ee]\d+'),
        # Season 1 Episode 3
        (r'[Ss]eason[.\s]*\d+[.\s]*[Ee]pisode[.\s]*(\d+)', r'[Ss]eason[.\s]*\d+[.\s]*[Ee]pisode[.\s]*\d+'),
        # 01 - Title, 1 - Title
        (r'^0*(\d+)[.\s]*-', r'^0*\d+[.\s]*-'),
        # Title - 01, Title - 1
        (r'-\s*0*(\d+)$', r'-\s*0*\d+$'),
        # [01], (01), {01}
        (r'[\[\(\{]0*(\d+)[\]\)\}]', r'[\[\(\{]0*\d+[\]\)\}]'),
    ]
    
    for pattern, remove_pattern in patterns:
        match = re.search(pattern, title)
        if match:
            # Get episode number (if pattern has two groups, take the second for episode)
            if len(match.groups()) > 1:
                episode = match.group(2).lstrip('0') or '1'
            else:
                episode = match.group(1).lstrip('0') or '1'
            
            # Remove pattern from title
            cleaned = re.sub(remove_pattern, '', title, flags=re.IGNORECASE).strip()
            # Clean up extra spaces and punctuation
            cleaned = re.sub(r'\s+', ' ', cleaned)
            cleaned = re.sub(r'[\[\]\(\)\-_\s]+$', '', cleaned)
            cleaned = re.sub(r'^[\[\]\(\)\-_\s]+', '', cleaned)
            
            return episode, cleaned
    
    return None, title

def format_caption(episode: str, title: str, series: str, duration: int) -> str:
    """Format caption as required"""
    return f"""🎬 Episode {episode} – {title}
📺 Series: {series}
🕒 Duration: {duration} min
🎞 Quality: HD"""

def get_video_duration(video_path: str) -> int:
    """Get video duration in minutes using ffprobe"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', 
             '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.stdout.strip():
            duration_seconds = float(result.stdout.strip())
            return max(1, int(duration_seconds // 60))
    except Exception as e:
        logger.error(f"Error getting duration: {e}")
    
    # Try to get from filename or return default
    return 0

def cleanup_old_files():
    """Clean up old downloaded files"""
    try:
        download_dir = f"{DATA_DIR}/downloads"
        now = time.time()
        for f in os.listdir(download_dir):
            f_path = os.path.join(download_dir, f)
            if os.path.isfile(f_path) and now - os.path.getmtime(f_path) > 3600:  # 1 hour
                os.remove(f_path)
                logger.info(f"Cleaned up old file: {f}")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")

# ==================== KEYBOARDS ====================

def get_main_keyboard():
    """Main menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("📥 Download YouTube Playlist", callback_data="menu_dl")],
        [InlineKeyboardButton("🔄 Forward from Channel", callback_data="menu_forward")],
        [InlineKeyboardButton("📋 My Cartoons", callback_data="menu_list")],
        [InlineKeyboardButton("➕ Add Cartoon Manually", callback_data="menu_add")],
        [InlineKeyboardButton("⏹ Stop Current Task", callback_data="menu_stop")],
        [InlineKeyboardButton("📊 Status & Stats", callback_data="menu_stats")]
    ]
    return InlineKeyboardMarkup(keyboard)
  def get_cartoons_keyboard():
    """Keyboard with list of cartoons"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, channel FROM cartoons ORDER BY name")
    cartoons = c.fetchall()
    conn.close()
    
    keyboard = []
    for name, channel in cartoons:
        display_name = name[:20] + "..." if len(name) > 20 else name
        keyboard.append([InlineKeyboardButton(f"{display_name} ({channel})", callback_data=f"cartoon_{name}")])
    
    # Add navigation
    nav_buttons = []
    if cartoons:
        nav_buttons.append(InlineKeyboardButton("🗑 Delete All", callback_data="delete_all_confirm"))
    nav_buttons.append(InlineKeyboardButton("🔙 Back", callback_data="back_main"))
    keyboard.append(nav_buttons)
    
    return InlineKeyboardMarkup(keyboard)

def get_cartoon_action_keyboard(name: str):
    """Keyboard for cartoon actions"""
    keyboard = [
        [InlineKeyboardButton("🖼 Set Thumbnail", callback_data=f"thumb_{name}")],
        [InlineKeyboardButton("📥 Download for this Cartoon", callback_data=f"download_{name}")],
        [InlineKeyboardButton("🔄 Forward to this Cartoon", callback_data=f"forward_{name}")],
        [InlineKeyboardButton("❌ Delete", callback_data=f"delete_{name}")],
        [InlineKeyboardButton("🔙 Back to List", callback_data="menu_list")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_confirm_keyboard(action: str, name: str):
    """Confirmation keyboard"""
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"confirm_{action}_{name}"),
            InlineKeyboardButton("❌ No", callback_data=f"cartoon_{name}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ==================== COMMAND HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    # Check authorization
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Unauthorized. This is a private bot.")
        return
    
    await update.message.reply_text(
        "🎬 **Cartoon Database Bot**\n\n"
        "Manage your cartoon channels with ease!\n\n"
        "**Features:**\n"
        "• Download YouTube playlists\n"
        "• Forward from Telegram channels\n"
        "• Automatic episode detection\n"
        "• Custom thumbnails per cartoon\n"
        "• HD quality always\n\n"
        "Select an option below:",
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all button callbacks"""
    query = update.callback_query
    await query.answer()
    
    # Check authorization
    if update.effective_user.id != OWNER_ID:
        await query.edit_message_text("⛔ Unauthorized")
        return
    
    data = query.data
    logger.info(f"Button pressed: {data}")
    
    # ===== MAIN MENU OPTIONS =====
    if data == "menu_dl":
        context.user_data['action'] = 'waiting_dl'
        await query.edit_message_text(
            "📥 **Download YouTube Playlist**\n\n"
            "Send me the YouTube playlist URL and cartoon name in this format:\n\n"
            "`[URL] [Cartoon Name]`\n\n"
            "**Example:**\n"
            "`https://youtube.com/playlist?list=... Tom and Jerry`\n\n"
            "The bot will download all videos and upload to the cartoon's channel.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_main")]]),
            parse_mode='Markdown'
        )
    
    elif data == "menu_forward":
        context.user_data['action'] = 'waiting_forward'
        await query.edit_message_text(
            "🔄 **Forward from Channel**\n\n"
            "Send me the channel ID and cartoon name in this format:\n\n"
            "`[Channel ID] [Cartoon Name]`\n\n"
            "**Example:**\n"
            "`@tomandjerry_channel Tom and Jerry`\n\n"
            "The bot will forward recent videos to the cartoon's channel.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_main")]]),
            parse_mode='Markdown'
        )
    
    elif data == "menu_list":
        keyboard = get_cartoons_keyboard()
        await query.edit_message_text(
            "📋 **Your Cartoons**\n\n"
            "Select a cartoon to manage:",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
    
    elif data == "menu_add":
        context.user_data['action'] = 'waiting_add_cartoon'
        await query.edit_message_text(
            "➕ **Add New Cartoon**\n\n"
            "Send me the cartoon name and channel in this format:\n\n"
            "`[Cartoon Name] [Channel]`\n\n"
            "**Example:**\n"
            "`Tom and Jerry @tomandjerry_channel`\n\n"
            "The channel is where videos will be uploaded.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_main")]]),
            parse_mode='Markdown'
        )
    
    elif data == "menu_stop":
        global bot_state
        if bot_state.processing:
            bot_state.stop_requested = True
            await query.edit_message_text(
                "⏹ **Stop Signal Sent**\n\n"
                "The bot will stop after completing the current item.",
                reply_markup=get_main_keyboard(),
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                "✅ No task is currently running.",
                reply_markup=get_main_keyboard(),
                parse_mode='Markdown'
            )
    
    elif data == "menu_stats":
        # Get statistics
        conn = get_db_connection()
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM cartoons")
        cartoon_count = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM youtube_processed")
        youtube_count = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM forwarded_processed")
        forward_count = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM download_queue WHERE status='pending'")
        queue_count = c.fetchone()[0]
        
        conn.close()
        
        # Calculate uptime if processing
        status_text = "🟢 Idle"
        if bot_state.processing:
            elapsed = datetime.now() - bot_state.start_time if bot_state.start_time else datetime.now() - datetime.now()
            minutes = int(elapsed.total_seconds() / 60)
            status_text = f"🟡 Processing ({bot_state.current_task}) - {minutes} min"
        
        stats_text = f"""📊 **Bot Statistics**

**Database:**
• Cartoons: {cartoon_count}
• YouTube videos: {youtube_count}
• Forwarded videos: {forward_count}
• Queue: {queue_count}

**Current Status:**
{status_text}
• Processed: {bot_state.items_processed}/{bot_state.total_items if bot_state.total_items else 0}

**System:**
• Data dir: {DATA_DIR}
• Quality: {VIDEO_QUALITY}p
• Max size: {MAX_FILE_SIZE}MB
"""
        await query.edit_message_text(
            stats_text,
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown'
        )
    
    elif data == "back_main":
        await query.edit_message_text(
            "🎬 **Main Menu**\n\nSelect an option:",
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown'
        )
        context.user_data.clear()

    # ===== CARTOON SPECIFIC OPTIONS =====
    elif data.startswith("cartoon_"):
        name = data[8:]  # Remove "cartoon_" prefix
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT channel, thumbnail FROM cartoons WHERE name = ?", (name,))
        result = c.fetchone()
        conn.close()
        
        if not result:
            await query.edit_message_text(
                "❌ Cartoon not found.",
                reply_markup=get_main_keyboard()
            )
            return
        
        channel, thumb = result
        thumb_status = "✅ Set" if thumb and os.path.exists(thumb) else "❌ Not set"
        
        await query.edit_message_text(
            f"📺 **{name}**\n\n"
            f"**Channel:** {channel}\n"
            f"**Thumbnail:** {thumb_status}\n\n"
            f"Select an action:",
            reply_markup=get_cartoon_action_keyboard(name),
            parse_mode='Markdown'
        )
    
    elif data.startswith("thumb_"):
        name = data[6:]  # Remove "thumb_" prefix
        context.user_data['thumb_for'] = name
        await query.edit_message_text(
            f"🖼 **Set Thumbnail for {name}**\n\n"
            f"Send me the image you want to use as thumbnail.\n\n"
            f"Reply to this message with the image or send it now.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"cartoon_{name}")]]),
            parse_mode='Markdown'
        )
    
    elif data.startswith("download_"):
        name = data[9:]  # Remove "download_" prefix
        context.user_data['dl_cartoon'] = name
        context.user_data['action'] = 'waiting_dl_url_with_cartoon'
        await query.edit_message_text(
            f"📥 **Download for {name}**\n\n"
            f"Send me the YouTube playlist URL:\n\n"
            f"**Example:**\n"
            f"`https://youtube.com/playlist?list=...`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"cartoon_{name}")]]),
            parse_mode='Markdown'
        )
    
    elif data.startswith("forward_"):
        name = data[8:]  # Remove "forward_" prefix
        context.user_data['forward_cartoon'] = name
        context.user_data['action'] = 'waiting_forward_channel_with_cartoon'
        await query.edit_message_text(
            f"🔄 **Forward to {name}**\n\n"
            f"Send me the source channel ID:\n\n"
            f"**Example:**\n"
            f"`@source_channel`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"cartoon_{name}")]]),
            parse_mode='Markdown'
        )
    
    elif data.startswith("delete_"):
        name = data[7:]  # Remove "delete_" prefix
        await query.edit_message_text(
            f"❌ **Delete {name}?**\n\n"
            f"Are you sure you want to delete this cartoon?\n"
            f"This will remove it from the database but NOT delete uploaded videos.",
            reply_markup=get_confirm_keyboard("delete", name),
            parse_mode='Markdown'
        )
    
    elif data.startswith("confirm_delete_"):
        name = data[15:]  # Remove "confirm_delete_" prefix
        
        # Delete from database
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM cartoons WHERE name = ?", (name,))
        conn.commit()
        conn.close()
        
        # Delete thumbnail file
        thumb_path = f"{DATA_DIR}/thumbnails/{name}.jpg"
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        
        await query.edit_message_text(
            f"✅ **{name} deleted successfully**",
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown'
        )
    
    elif data == "delete_all_confirm":
        await query.edit_message_text(
            "❌ **Delete ALL Cartoons?**\n\n"
            "⚠️ **WARNING:** This will delete ALL cartoons from the database.\n"
            "This action cannot be undone!",
            reply_markup=get_confirm_keyboard("delete_all", "all"),
            parse_mode='Markdown'
        )
    
    elif data == "confirm_delete_all_all":
        # Delete all cartoons
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM cartoons")
        conn.commit()
        conn.close()
        
        # Delete all thumbnails
        thumb_dir = f"{DATA_DIR}/thumbnails"
        for f in os.listdir(thumb_dir):
            os.remove(os.path.join(thumb_dir, f))
        
        await query.edit_message_text(
            "✅ **All cartoons deleted successfully**",
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown'
        )

# ==================== MESSAGE HANDLERS ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text and photo messages"""
    # Check authorization
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return
    
    # Get current action
    action = context.user_data.get('action')
    
    # Handle text messages
    if update.message.text:
        text = update.message.text.strip()
        
        # ===== ADD CARTOON MANUALLY =====
        if action == 'waiting_add_cartoon':
            # Parse: Name @channel
            parts = text.rsplit(' ', 1)
            if len(parts) < 2:
                await update.message.reply_text(
                    "❌ Invalid format. Send: `Name @channel`",
                    reply_markup=get_main_keyboard(),
                    parse_mode='Markdown'
                )
                return
            
            name = parts[0].strip()
            channel = parts[1].strip()
            
            # Validate channel format
            if not channel.startswith('@') and not channel.startswith('-100'):
                await update.message.reply_text(
                    "❌ Channel must start with @ or -100",
                    reply_markup=get_main_keyboard()
                )
                return
            
            # Save to database
            conn = get_db_connection()
            c = conn.cursor()
            try:
                c.execute(
                    "INSERT INTO cartoons (name, channel, thumbnail, created) VALUES (?, ?, ?, ?)",
                    (name, channel, None, datetime.now())
                )
                conn.commit()
                await update.message.reply_text(
                    f"✅ **Cartoon Added:**\n\nName: {name}\nChannel: {channel}",
                    reply_markup=get_main_keyboard(),
                    parse_mode='Markdown'
                )
            except sqlite3.IntegrityError:
                await update.message.reply_text(
                    f"❌ Cartoon '{name}' already exists",
                    reply_markup=get_main_keyboard()
                )
            finally:
                conn.close()
            
            context.user_data.clear()
        
        # ===== DOWNLOAD PLAYLIST =====
        elif action == 'waiting_dl':
            # Parse: URL Name
            parts = text.split(' ', 1)
            if len(parts) < 2:
                await update.message.reply_text(
                    "❌ Send: URL CartoonName",
                    reply_markup=get_main_keyboard()
                )
                return
            
            url = parts[0].strip()
            name = parts[1].strip()
            
            # Check if cartoon exists
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT channel FROM cartoons WHERE name = ?", (name,))
            result = c.fetchone()
            conn.close()
            
            if not result:
                await update.message.reply_text(
                    f"❌ Cartoon '{name}' not found. Add it first with /add",
                    reply_markup=get_main_keyboard()
                )
                return
            
            channel = result[0]
            
            # Start download
            await update.message.reply_text(f"🔄 Starting download for {name}")
            asyncio.create_task(download_playlist(url, name, channel, update))
            context.user_data.clear()
        
        # ===== DOWNLOAD WITH EXISTING CARTOON =====
        elif action == 'waiting_dl_url_with_cartoon':
            name = context.user_data.get('dl_cartoon')
            url = text
            
            # Get channel
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT channel FROM cartoons WHERE name = ?", (name,))
            result = c.fetchone()
            conn.close()
            
            if not result:
                await update.message.reply_text(
                    f"❌ Cartoon '{name}' not found",
                    reply_markup=get_main_keyboard()
                )
                context.user_data.clear()
                return
            
            channel = result[0]
            
            # Start download
            await update.message.reply_text(f"🔄 Starting download for {name}")
            asyncio.create_task(download_playlist(url, name, channel, update))
            context.user_data.clear()
        
        # ===== FORWARD FROM CHANNEL =====
        elif action == 'waiting_forward':
            # Parse: Channel Name
            parts = text.split(' ', 1)
            if len(parts) < 2:
                await update.message.reply_text(
                    "❌ Send: ChannelID CartoonName",
                    reply_markup=get_main_keyboard()
                )
                return
            
            source_channel = parts[0].strip()
            name = parts[1].strip()
            
            # Check if cartoon exists
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT channel FROM cartoons WHERE name = ?", (name,))
            result = c.fetchone()
            conn.close()
            
            if not result:
                await update.message.reply_text(
                    f"❌ Cartoon '{name}' not found. Add it first.",
                    reply_markup=get_main_keyboard()
                )
                return
            
            target_channel = result[0]
            
            # Start forwarding
            await update.message.reply_text(f"🔄 Starting forward from {source_channel}")
            asyncio.create_task(forward_from_channel(source_channel, name, target_channel, update))
            context.user_data.clear()
                  # ===== FORWARD WITH EXISTING CARTOON =====
        elif action == 'waiting_forward_channel_with_cartoon':
            name = context.user_data.get('forward_cartoon')
            source_channel = text
            
            # Get target channel
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT channel FROM cartoons WHERE name = ?", (name,))
            result = c.fetchone()
            conn.close()
            
            if not result:
                await update.message.reply_text(
                    f"❌ Cartoon '{name}' not found",
                    reply_markup=get_main_keyboard()
                )
                context.user_data.clear()
                return
            
            target_channel = result[0]
            
            # Start forwarding
            await update.message.reply_text(f"🔄 Starting forward from {source_channel} to {target_channel}")
            asyncio.create_task(forward_from_channel(source_channel, name, target_channel, update))
            context.user_data.clear()
        
        else:
            await update.message.reply_text(
                "❌ I don't understand. Use the buttons.",
                reply_markup=get_main_keyboard()
            )
    
    # Handle photo messages (for thumbnails)
    elif update.message.photo and 'thumb_for' in context.user_data:
        name = context.user_data['thumb_for']
        
        # Download photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        thumb_path = f"{DATA_DIR}/thumbnails/{name}.jpg"
        await file.download_to_drive(thumb_path)
        
        # Update database
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE cartoons SET thumbnail = ? WHERE name = ?", (thumb_path, name))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(
            f"✅ Thumbnail saved for {name}",
            reply_markup=get_main_keyboard()
        )
        context.user_data.clear()

# ==================== CORE FUNCTIONS ====================

async def download_playlist(url: str, series: str, channel: str, update: Update):
    """Download YouTube playlist and upload to channel"""
    global bot_state
    
    if bot_state.processing:
        await update.message.reply_text("❌ Another task is already running. Use /stop to cancel it.")
        return
    
    bot_state.processing = True
    bot_state.stop_requested = False
    bot_state.current_task = f"Downloading {series}"
    bot_state.current_series = series
    bot_state.items_processed = 0
    bot_state.total_items = 0
    bot_state.start_time = datetime.now()
    
    try:
        # Get thumbnail path
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT thumbnail FROM cartoons WHERE name = ?", (series,))
        result = c.fetchone()
        thumb_path = result[0] if result and result[0] and os.path.exists(result[0]) else None
        conn.close()
        
        # Configure yt-dlp
        ydl_opts = {
            'format': f'best[height<={VIDEO_QUALITY}][ext=mp4]/best[height<={VIDEO_QUALITY}]',
            'outtmpl': f'{DATA_DIR}/downloads/%(title)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
            'retries': 5,
            'fragment_retries': 5,
            'continuedl': True,
            'buffersize': 1024,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Get playlist info
            await update.message.reply_text(f"📋 Fetching playlist info...")
            info = ydl.extract_info(url, download=False)
            
            if 'entries' in info:
                total = len(info['entries'])
                bot_state.total_items = total
                await update.message.reply_text(f"📋 Found {total} videos in playlist")
                
                for i, entry in enumerate(info['entries']):
                    if bot_state.stop_requested:
                        await update.message.reply_text("⏹ Download stopped by user")
                        break
                    
                    if not entry:
                        continue
                    
                    video_url = entry['webpage_url']
                    video_title = entry.get('title', 'Unknown')
                    
                    # Check if already processed
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute("SELECT 1 FROM youtube_processed WHERE video_url = ?", (video_url,))
                    if c.fetchone():
                        conn.close()
                        continue
                    conn.close()
                    
                    # Extract episode
                    episode, clean_title = extract_episode_info(video_title)
                    if not episode:
                        episode = str(i + 1)
                    
                    await update.message.reply_text(f"⬇️ Downloading {i+1}/{total}: {clean_title[:30]}...")
                    
                    try:
                        # Download video
                        ydl.extract_info(video_url, download=True)
                        
                        # Find downloaded file
                        downloaded_file = None
                        for f in os.listdir(f"{DATA_DIR}/downloads"):
                            if f.endswith('.mp4') and (video_title[:30] in f or clean_title[:30] in f):
                                downloaded_file = f"{DATA_DIR}/downloads/{f}"
                                break
                        
                        if downloaded_file and os.path.exists(downloaded_file):
                            # Get duration
                            duration = get_video_duration(downloaded_file)
                            
                            # Upload to channel
                            caption = format_caption(episode, clean_title, series, duration)
                            
                            with open(downloaded_file, 'rb') as video:
                                if thumb_path:
                                    with open(thumb_path, 'rb') as thumb:
                                        await update.message.bot.send_video(
                                            chat_id=channel,
                                            video=video,
                                            caption=caption,
                                            thumb=thumb,
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
                            
                            # Mark as processed
                            conn = get_db_connection()
                            c = conn.cursor()
                            c.execute(
                                "INSERT INTO youtube_processed (video_url, series, episode, title, processed_date) VALUES (?, ?, ?, ?, ?)",
                                (video_url, series, episode, clean_title, datetime.now())
                            )
                            conn.commit()
                            conn.close()
                            
                            bot_state.items_processed += 1
                            
                            # Cleanup
                            try:
                                os.remove(downloaded_file)
                            except:
                                pass
                            
                    except Exception as e:
                        logger.error(f"Error processing {video_url}: {e}")
                        await update.message.reply_text(f"⚠️ Error on {video_title[:30]}: {str(e)[:50]}")
                        continue
                
                await update.message.reply_text(f"✅ Completed! Uploaded {bot_state.items_processed}/{total} videos")
            
            else:
                await update.message.reply_text("❌ Not a playlist or no videos found")
    
    except Exception as e:
        logger.error(f"Download error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")
    
    finally:
        bot_state.processing = False
        bot_state.stop_requested = False
        # Cleanup old files
        cleanup_old_files()
      async def forward_from_channel(source_channel: str, series: str, target_channel: str, update: Update):
    """Forward videos from source channel to target channel"""
    global bot_state
    
    if bot_state.processing:
        await update.message.reply_text("❌ Another task is already running. Use /stop to cancel it.")
        return
    
    bot_state.processing = True
    bot_state.stop_requested = False
    bot_state.current_task = f"Forwarding to {series}"
    bot_state.current_series = series
    bot_state.items_processed = 0
    bot_state.total_items = 0
    bot_state.start_time = datetime.now()
    
    try:
        # Get recent messages
        await update.message.reply_text(f"📋 Fetching messages from {source_channel}...")
        
        messages = []
        try:
            async for msg in update.message.bot.get_chat_history(chat_id=source_channel, limit=100):
                messages.append(msg)
        except Exception as e:
            await update.message.reply_text(f"❌ Cannot access channel: {e}")
            return
        
        total = len(messages)
        bot_state.total_items = total
        await update.message.reply_text(f"📋 Found {total} recent messages")
        
        # Get thumbnail if exists
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT thumbnail FROM cartoons WHERE name = ?", (series,))
        result = c.fetchone()
        thumb_path = result[0] if result and result[0] and os.path.exists(result[0]) else None
        conn.close()
        
        forwarded = 0
        for i, msg in enumerate(messages):
            if bot_state.stop_requested:
                await update.message.reply_text("⏹ Forwarding stopped by user")
                break
            
            # Check if already processed
            conn = get_db_connection()
            c = conn.cursor()
            c.execute(
                "SELECT 1 FROM forwarded_processed WHERE channel_id = ? AND message_id = ?",
                (source_channel, msg.message_id)
            )
            if c.fetchone():
                conn.close()
                continue
            conn.close()
            
            # Check if message has video
            file_id = None
            if msg.video:
                file_id = msg.video.file_id
            elif msg.document and msg.document.mime_type and 'video' in msg.document.mime_type:
                file_id = msg.document.file_id
            
            if file_id:
                # Extract episode from caption
                original_caption = msg.caption or ""
                episode, clean_title = extract_episode_info(original_caption)
                
                if not episode:
                    episode = str(forwarded + 1)
                
                # Format new caption
                caption = format_caption(episode, clean_title or "Cartoon", series, 0)
                
                try:
                    # Forward video
                    if thumb_path:
                        # Can't add thumbnail when forwarding directly, so we need to download and reupload
                        # For simplicity, we'll forward without thumbnail
                        await update.message.bot.send_video(
                            chat_id=target_channel,
                            video=file_id,
                            caption=caption,
                            supports_streaming=True
                        )
                    else:
                        await update.message.bot.send_video(
                            chat_id=target_channel,
                            video=file_id,
                            caption=caption,
                            supports_streaming=True
                        )
                    
                    # Mark as processed
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute(
                        "INSERT INTO forwarded_processed (channel_id, message_id, series, processed_date) VALUES (?, ?, ?, ?)",
                        (source_channel, msg.message_id, series, datetime.now())
                    )
                    conn.commit()
                    conn.close()
                    
                    forwarded += 1
                    bot_state.items_processed = forwarded
                    
                    if forwarded % 10 == 0:
                        await update.message.reply_text(f"📊 Forwarded {forwarded}/{total} videos")
                
                except Exception as e:
                    logger.error(f"Error forwarding message {msg.message_id}: {e}")
                    continue
        
        await update.message.reply_text(f"✅ Completed! Forwarded {forwarded} videos")
    
    except Exception as e:
        logger.error(f"Forward error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")
    
    finally:
        bot_state.processing = False
        bot_state.stop_requested = False

# ==================== ERROR HANDLER ====================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ An error occurred. Please try again."
            )
    except:
        pass

# ==================== MAIN ====================

def main():
    """Main function"""
    # Check token
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: BOT_TOKEN not set!")
        print("Please set your bot token in environment variables or directly in code.")
        sys.exit(1)
    
    # Check owner ID
    if OWNER_ID == 0:
        print("WARNING: OWNER_ID not set! Bot will be accessible to everyone.")
    
    # Create application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))
    app.add_error_handler(error_handler)
    
    # Print startup message
    print("=" * 50)
    print("🎬 CARTOON DATABASE BOT")
    print("=" * 50)
    print(f"Bot Token: {BOT_TOKEN[:10]}...{BOT_TOKEN[-5:]}")
    print(f"Owner ID: {OWNER_ID}")
    print(f"Data Directory: {DATA_DIR}")
    print(f"Video Quality: {VIDEO_QUALITY}p")
    print(f"Max File Size: {MAX_FILE_SIZE}MB")
    print("=" * 50)
    print("Bot is running... Press Ctrl+C to stop")
    
    # Run bot
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
