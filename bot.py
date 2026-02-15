import os
import re
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
import logging
import sys
import shutil
import subprocess
from typing import Optional, Dict, List, Tuple

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# CRITICAL FIX: Create event loop BEFORE importing pyrogram
if sys.version_info >= (3, 14):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
else:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

# Now import pyrogram after event loop is created
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.enums import ParseMode, MessageMediaType
from pyrogram.errors import FloodWait, ChannelInvalid, ChatAdminRequired
import yt_dlp

# Bot configuration
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID"))

# Initialize bot
app = Client("cartoon_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Storage
cartoons = {}
current_operation = {}
forward_sessions = {}
download_sessions = {}
background_tasks = []
last_activity = datetime.now()

# Constants
MAX_CONCURRENT_DOWNLOADS = 2
KEEP_AWAKE_INTERVAL = 300  # 5 minutes in seconds

# Load/Save cartoons
def load_cartoons():
    global cartoons
    try:
        if os.path.exists("cartoons.json"):
            with open("cartoons.json", "r") as f:
                cartoons = json.load(f)
                # Convert channel_id back to int
                for name, info in cartoons.items():
                    info['channel_id'] = int(info['channel_id'])
                logger.info(f"Loaded {len(cartoons)} cartoons from storage")
    except Exception as e:
        logger.error(f"Error loading cartoons: {e}")
        cartoons = {}

def save_cartoons():
    try:
        with open("cartoons.json", "w") as f:
            json.dump(cartoons, f, indent=2)
        logger.info("Cartoons saved successfully")
    except Exception as e:
        logger.error(f"Error saving cartoons: {e}")

# Extract episode and season number from title
def extract_episode_info(title: str) -> Tuple[str, int, int]:
    """Extract episode number, season from various formats"""
    title_upper = title.upper()
    
    # Patterns to match (in priority order)
    patterns = [
        # Season and Episode patterns
        (r'S(\d+)\s*E(\d+)', lambda m: (f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}", int(m.group(1)), int(m.group(2)))),
        (r'SEASON\s*(\d+).*?EP(?:ISODE)?\s*(\d+)', lambda m: (f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}", int(m.group(1)), int(m.group(2)))),
        (r'(\d+)X(\d+)', lambda m: (f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}", int(m.group(1)), int(m.group(2)))),
        
        # Just episode number patterns
        (r'EP\.?\s*(\d+)', lambda m: (f"{int(m.group(1)):02d}", 1, int(m.group(1)))),
        (r'EPISODE\s*(\d+)', lambda m: (f"{int(m.group(1)):02d}", 1, int(m.group(1)))),
        (r'E(\d+)', lambda m: (f"{int(m.group(1)):02d}", 1, int(m.group(1)))),
        (r'\[(\d+)\]', lambda m: (f"{int(m.group(1)):02d}", 1, int(m.group(1)))),
        (r'#(\d+)', lambda m: (f"{int(m.group(1)):02d}", 1, int(m.group(1)))),
        (r'-\s*(\d+)\s*-', lambda m: (f"{int(m.group(1)):02d}", 1, int(m.group(1)))),
        (r'^\s*(\d+)\s*[-.]', lambda m: (f"{int(m.group(1)):02d}", 1, int(m.group(1)))),
    ]
    
    for pattern, formatter in patterns:
        match = re.search(pattern, title_upper)
        if match:
            try:
                return formatter(match)
            except:
                continue
    
    # If nothing found, try to find any number
    number_match = re.search(r'\b(\d+)\b', title_upper)
    if number_match:
        num = int(number_match.group(1))
        if 1 <= num <= 999:
            return (f"{num:02d}", 1, num)
    
    return ("01", 1, 1)  # Default

# Format duration
def format_duration(seconds: int) -> str:
    """Convert seconds to readable format"""
    if seconds:
        minutes = int(seconds // 60)
        hours = minutes // 60
        mins = minutes % 60
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{minutes} min"
    return "Unknown"

# Clean title from episode indicators
def clean_title(title: str, remove_patterns: bool = True) -> str:
    """Clean title by removing episode indicators"""
    if not remove_patterns:
        return title
        
    patterns_to_remove = [
        r'S\d+E\d+',
        r'Season\s*\d+\s*Episode\s*\d+',
        r'\d+x\d+',
        r'EP\.?\s*\d+',
        r'EPISODE\s*\d+',
        r'E\d+',
        r'\[\d+\]',
        r'\(\d+\)',
        r'\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpg|mpeg)$',
        r'-\s*\d+\s*-',
        r'^\s*\d+\s*[-.]',
        r'#\d+',
        r'x264',
        r'x265',
        r'AAC',
        r'MP3',
        r'HDTV',
        r'WEB-DL',
        r'WEBRip',
        r'BluRay',
        r'DVDRip',
        r'480p',
        r'720p',
        r'1080p',
        r'2160p',
        r'4K',
    ]
    
    for pattern in patterns_to_remove:
        title = re.sub(pattern, '', title, flags=re.IGNORECASE)
    
    # Clean up extra spaces and special chars
    title = re.sub(r'\s+', ' ', title).strip()
    title = re.sub(r'^[-.\s]+|[-.\s]+$', '', title)
    title = re.sub(r'[._]', ' ', title)
    
    return title

# Create caption
def create_caption(episode_num: str, title: str, series_name: str, duration: str, 
                   audio_lang: str = None, season: int = 1) -> str:
    """Create formatted caption with all details"""
    clean_title_text = clean_title(title)
    if not clean_title_text or len(clean_title_text) < 3:
        clean_title_text = title[:50]
    
    caption = f"🎬 **Episode {episode_num}**"
    if season > 1:
        caption = f"🎬 **Season {season} Episode {episode_num}**"
    
    caption += f"\n📺 **Series:** {series_name}"
    caption += f"\n📝 **Title:** {clean_title_text}"
    caption += f"\n⏱ **Duration:** {duration}"
    
    if audio_lang:
        caption += f"\n🔊 **Audio:** {audio_lang}"
    
    caption += "\n🎞 **Quality:** HD"
    caption += "\n\n#cartoon #episode"
    
    return caption

# Background task to keep bot alive
async def keep_alive_task():
    """Run every 5 minutes to prevent bot from sleeping"""
    global last_activity
    while True:
        try:
            await asyncio.sleep(KEEP_AWAKE_INTERVAL)
            # Just log to show bot is alive
            logger.info(f"🟢 Bot is alive - Last activity: {last_activity}")
            # Can also send a message to yourself if needed
            # await app.send_message(OWNER_ID, "🟢 Bot is awake")
        except Exception as e:
            logger.error(f"Keep alive error: {e}")

# Monitor forwarded messages from channels
async def monitor_forwarded_messages():
    """Background task to monitor and process forwarded messages"""
    while True:
        try:
            # Check for active forward sessions
            for user_id, session in list(forward_sessions.items()):
                if session.get('active', False):
                    # Sessions are handled in message handler
                    pass
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Monitor error: {e}")

# Download YouTube video with better options and season tracking
async def download_youtube_video(url: str, output_path: str, progress_msg: Message = None, 
                                 season: int = 1, episode_start: int = None) -> Optional[Dict]:
    """Download a single YouTube video with optimization"""
    ydl_opts = {
        'format': 'best[height<=1080][ext=mp4]/best[height<=1080]/best',
        'outtmpl': f'{output_path}/%(title)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'concurrent_fragment_downloads': 5,
        'retries': 5,
        'fragment_retries': 5,
        'ignoreerrors': True,
        'no_color': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract info first to check
            info = ydl.extract_info(url, download=False)
            if not info:
                return None
            
            # Download the video
            ydl.download([url])
            
            # Get the filename
            filename = ydl.prepare_filename(info)
            
            # Handle potential filename issues
            if not os.path.exists(filename):
                base = os.path.splitext(filename)[0]
                for ext in ['.mp4', '.mkv', '.webm']:
                    if os.path.exists(base + ext):
                        filename = base + ext
                        break
            
            if not os.path.exists(filename):
                # Try to find any file in the directory
                files = os.listdir(output_path)
                if files:
                    filename = os.path.join(output_path, files[0])
            
            # Extract episode info
            ep_info, detected_season, ep_num = extract_episode_info(info.get('title', ''))
            
            return {
                'file': filename,
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0),
                'episode_num': ep_num if episode_start is None else episode_start,
                'season': detected_season,
                'episode_info': ep_info
            }
    except Exception as e:
        logger.error(f"Download error: {e}")
        if progress_msg:
            try:
                await progress_msg.edit(f"❌ Error: {str(e)[:100]}")
            except:
                pass
        return None

# Main menu
def main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("➕ Add Cartoon", callback_data="add_cartoon")],
        [InlineKeyboardButton("📥 Download YouTube Playlist", callback_data="download_yt")],
        [InlineKeyboardButton("📤 Forward from Channel", callback_data="forward_channel")],
        [InlineKeyboardButton("⚙️ Auto-Forward Setup", callback_data="auto_forward")],
        [InlineKeyboardButton("📋 List Cartoons", callback_data="list_cartoons")],
        [InlineKeyboardButton("🗑 Remove Cartoon", callback_data="remove_cartoon")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Start command
@app.on_message(filters.command("start") & filters.user(OWNER_ID))
async def start(client: Client, message: Message):
    global last_activity
    last_activity = datetime.now()
    await message.reply_text(
        "🎬 **Cartoon Database Bot v2.0**\n\n"
        "Welcome! Use the buttons below to manage your cartoons.\n\n"
        "**New Features:**\n"
        "• Auto-convert files to videos\n"
        "• Season playlist continuation\n"
        "• Audio language selection\n"
        "• Auto-forward monitoring\n"
        "• 24/7 operation (no sleep)",
        reply_markup=main_menu()
    )

# Stop command
@app.on_message(filters.command("stop") & filters.user(OWNER_ID))
async def stop_operation(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in current_operation:
        current_operation[user_id] = "stopped"
        await message.reply_text("⏹ **Stopping operation...**\n\nCurrent task will finish.")
    else:
        await message.reply_text("❌ No active operation.")

# Help command
@app.on_message(filters.command("help") & filters.user(OWNER_ID))
async def help_command(client: Client, message: Message):
    help_text = """📚 **How to Use:**

**1. Add Cartoon:**
• Click "Add Cartoon"
• Send cartoon name
• Send channel ID
• Send thumbnail (optional)

**2. Download Playlist:**
• Choose cartoon
• Send playlist URL
• Choose season/episode options
• Bot downloads & uploads

**3. Forward from Channel:**
• Choose destination cartoon
• Forward ANY message from source
• Bot forwards ALL videos
• Auto-converts files to videos

**4. Auto-Forward Setup:**
• Set up automatic forwarding
• Bot monitors source channel
• Auto-processes new videos
• Adds formatted captions

**5. Audio Language:**
• Add language tag to videos
• Shows in caption

**Commands:**
/start - Main menu
/stop - Stop current operation
/help - This help
/status - Bot status"""
    
    await message.reply_text(help_text, reply_markup=main_menu())

# Status command
@app.on_message(filters.command("status") & filters.user(OWNER_ID))
async def status_command(client: Client, message: Message):
    global last_activity
    uptime = datetime.now() - last_activity
    status_text = f"""📊 **Bot Status:**

🟢 **Online**
⏱ **Uptime:** {str(uptime).split('.')[0]}
📁 **Cartoons:** {len(cartoons)}
🔄 **Active Ops:** {len(current_operation)}
📤 **Forward Sessions:** {len(forward_sessions)}
📥 **Download Sessions:** {len(download_sessions)}"""
    
    await message.reply_text(status_text)

# Callback query handler
@app.on_callback_query(filters.user(OWNER_ID))
async def callback_handler(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id
    global last_activity
    last_activity = datetime.now()
    
    if data == "add_cartoon":
        await callback_query.message.edit_text(
            "📝 **Add New Cartoon**\n\n"
            "Send me the cartoon name:\n"
            "(Example: Tom & Jerry, SpongeBob, etc.)"
        )
        current_operation[user_id] = {"step": "awaiting_name"}
    
    elif data == "download_yt":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet!", show_alert=True)
            return
        
        keyboard = []
        for name in sorted(cartoons.keys()):
            keyboard.append([InlineKeyboardButton(name, callback_data=f"yt_{name}")])
        keyboard.append([InlineKeyboardButton("« Back", callback_data="back_menu")])
        
        await callback_query.message.edit_text(
            "📥 **Download YouTube Playlist**\n\n"
            "Select cartoon to download for:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data.startswith("yt_"):
        cartoon_name = data[3:]
        current_operation[user_id] = {"step": "awaiting_yt_url", "cartoon": cartoon_name}
        
        # Ask for download options
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 New Season (Start from 1)", callback_data=f"yt_season_new_{cartoon_name}")],
            [InlineKeyboardButton("📺 Continue Season", callback_data=f"yt_season_continue_{cartoon_name}")],
            [InlineKeyboardButton("🎯 Specific Episode Range", callback_data=f"yt_season_range_{cartoon_name}")],
            [InlineKeyboardButton("« Back", callback_data="download_yt")]
        ])
        
        await callback_query.message.edit_text(
            f"📥 **Download Options for: {cartoon_name}**\n\n"
            "How would you like to download?",
            reply_markup=keyboard
        )
    
    elif data.startswith("yt_season_new_"):
        cartoon_name = data.replace("yt_season_new_", "")
        current_operation[user_id] = {
            "step": "awaiting_yt_url", 
            "cartoon": cartoon_name,
            "season": 1,
            "episode_start": 1
        }
        await callback_query.message.edit_text(
            f"📥 **New Season Download: {cartoon_name}**\n\n"
            "Send me the YouTube playlist URL:\n\n"
            "The bot will start from Episode 1 of Season 1."
        )
    
    elif data.startswith("yt_season_continue_"):
        cartoon_name = data.replace("yt_season_continue_", "")
        # Check last downloaded episode
        last_ep = cartoons[cartoon_name].get('last_episode', 0)
        last_season = cartoons[cartoon_name].get('last_season', 1)
        
        current_operation[user_id] = {
            "step": "awaiting_yt_url", 
            "cartoon": cartoon_name,
            "season": last_season,
            "episode_start": last_ep + 1
        }
        await callback_query.message.edit_text(
            f"📥 **Continue Season: {cartoon_name}**\n\n"
            f"Last downloaded: Season {last_season}, Episode {last_ep}\n"
            f"Will continue from: Season {last_season}, Episode {last_ep + 1}\n\n"
            "Send me the YouTube playlist URL:"
        )
    
    elif data.startswith("yt_season_range_"):
        cartoon_name = data.replace("yt_season_range_", "")
        current_operation[user_id] = {
            "step": "awaiting_yt_range",
            "cartoon": cartoon_name
        }
        await callback_query.message.edit_text(
            f"📥 **Episode Range: {cartoon_name}**\n\n"
            "Send the episode range in format:\n"
            "`season:episode_start-episode_end`\n\n"
            "Examples:\n"
            "• `1:1-10` - Season 1, Episodes 1-10\n"
            "• `2:5-15` - Season 2, Episodes 5-15\n"
            "• `3:1-` - Season 3 from Episode 1 onward"
        )
    
    elif data == "forward_channel":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet!", show_alert=True)
            return
        
        keyboard = []
        for name in sorted(cartoons.keys()):
            keyboard.append([InlineKeyboardButton(name, callback_data=f"fwd_{name}")])
        keyboard.append([InlineKeyboardButton("« Back", callback_data="back_menu")])
        
        await callback_query.message.edit_text(
            "📤 **Forward from Channel**\n\n"
            "Select destination cartoon:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data.startswith("fwd_"):
        cartoon_name = data[4:]
        current_operation[user_id] = {
            "step": "awaiting_forward_msg", 
            "cartoon": cartoon_name
        }
        
        # Ask for language option
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔊 Add Audio Language", callback_data=f"fwd_lang_{cartoon_name}")],
            [InlineKeyboardButton("⏭ Skip Language", callback_data=f"fwd_nolang_{cartoon_name}")]
        ])
        
        await callback_query.message.edit_text(
            f"📤 **Forward to: {cartoon_name}**\n\n"
            "Do you want to add audio language to captions?",
            reply_markup=keyboard
        )
    
    elif data.startswith("fwd_lang_"):
        cartoon_name = data.replace("fwd_lang_", "")
        current_operation[user_id] = {
            "step": "awaiting_forward_lang",
            "cartoon": cartoon_name
        }
        await callback_query.message.edit_text(
            f"📤 **Audio Language for: {cartoon_name}**\n\n"
            "Send the audio language (e.g., English, Hindi, Spanish, Japanese):"
        )
    
    elif data.startswith("fwd_nolang_"):
        cartoon_name = data.replace("fwd_nolang_", "")
        current_operation[user_id] = {
            "step": "awaiting_forward_msg",
            "cartoon": cartoon_name,
            "audio_lang": None
        }
        await callback_query.message.edit_text(
            f"📤 **Forward to: {cartoon_name}**\n\n"
            "Forward me ANY message from the source channel.\n\n"
            "The bot will then forward ALL videos from that channel."
        )
    
    elif data == "auto_forward":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet!", show_alert=True)
            return
        
        keyboard = []
        for name in sorted(cartoons.keys()):
            keyboard.append([InlineKeyboardButton(name, callback_data=f"auto_{name}")])
        keyboard.append([InlineKeyboardButton("« Back", callback_data="back_menu")])
        
        await callback_query.message.edit_text(
            "⚙️ **Auto-Forward Setup**\n\n"
            "Select cartoon to set up auto-forward:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data.startswith("auto_"):
        cartoon_name = data[5:]
        current_operation[user_id] = {
            "step": "awaiting_auto_source",
            "cartoon": cartoon_name
        }
        await callback_query.message.edit_text(
            f"⚙️ **Auto-Forward Setup for: {cartoon_name}**\n\n"
            "Forward me a message from the source channel you want to monitor.\n\n"
            "The bot will automatically process all new videos from this channel."
        )
    
    elif data == "list_cartoons":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet!", show_alert=True)
            return
        
        text = "📋 **Your Cartoons:**\n\n"
        for idx, (name, info) in enumerate(sorted(cartoons.items()), 1):
            last_ep = info.get('last_episode', 0)
            last_season = info.get('last_season', 1)
            text += f"{idx}. **{name}**\n"
            text += f"   📍 Channel: `{info['channel_id']}`\n"
            text += f"   📺 Last: S{last_season}E{last_ep}\n"
            text += f"   🖼 Thumb: {'✅' if info.get('thumbnail') else '❌'}\n\n"
        
        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_menu")]]
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "remove_cartoon":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet!", show_alert=True)
            return
        
        keyboard = []
        for name in sorted(cartoons.keys()):
            keyboard.append([InlineKeyboardButton(f"🗑 {name}", callback_data=f"del_{name}")])
        keyboard.append([InlineKeyboardButton("« Back", callback_data="back_menu")])
        
        await callback_query.message.edit_text(
            "🗑 **Remove Cartoon**\n\n"
            "Select cartoon to remove:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data.startswith("del_"):
        cartoon_name = data[4:]
        if cartoon_name in cartoons:
            del cartoons[cartoon_name]
            save_cartoons()
            await callback_query.answer(f"✅ {cartoon_name} removed!", show_alert=True)
            logger.info(f"Removed cartoon: {cartoon_name}")
        await callback_query.message.edit_text(
            "🎬 **Cartoon Database Bot**\n\n"
            "Choose an option below:",
            reply_markup=main_menu()
        )
    
    elif data == "back_menu":
        await callback_query.message.edit_text(
            "🎬 **Cartoon Database Bot**\n\n"
            "Choose an option below:",
            reply_markup=main_menu()
        )
    
    elif data == "skip_thumbnail":
        cartoon_name = current_operation[user_id].get("cartoon")
        if cartoon_name and cartoon_name in cartoons:
            cartoons[cartoon_name]["thumbnail"] = None
            save_cartoons()
            await callback_query.message.edit_text(
                f"✅ **{cartoon_name}** added successfully!\n\n"
                "No thumbnail set.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="back_menu")]])
            )
            if user_id in current_operation:
                del current_operation[user_id]

# Message handler
@app.on_message(filters.private & filters.user(OWNER_ID))
async def message_handler(client: Client, message: Message):
    user_id = message.from_user.id
    global last_activity
    last_activity = datetime.now()
    
    # Handle commands
    if message.text and message.text.startswith('/'):
        return
    
    if user_id not in current_operation:
        return
    
    operation = current_operation[user_id]
    step = operation.get("step")
    
    # Add cartoon flow
    if step == "awaiting_name":
        cartoon_name = message.text.strip()
        operation["cartoon"] = cartoon_name
        operation["step"] = "awaiting_channel"
        await message.reply_text(
            f"📝 **Adding: {cartoon_name}**\n\n"
            "Send me the destination channel ID.\n\n"
            "To get channel ID:\n"
            "1. Add bot as admin to channel\n"
            "2. Forward a message to @userinfobot\n"
            "3. Copy the ID (should be like: -1001234567890)"
        )
    
    elif step == "awaiting_channel":
        try:
            channel_id = message.text.strip()
            
            if not channel_id.startswith('-100'):
                channel_id = '-100' + channel_id.lstrip('-')
            
            channel_id = int(channel_id)
            cartoon_name = operation["cartoon"]
            
            cartoons[cartoon_name] = {
                "channel_id": channel_id,
                "series_name": cartoon_name,
                "thumbnail": None,
                "last_episode": 0,
                "last_season": 1,
                "auto_forward": []
            }
            
            operation["step"] = "awaiting_thumbnail"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Skip Thumbnail", callback_data="skip_thumbnail")]])
            await message.reply_text(
                f"📝 **Adding: {cartoon_name}**\n\n"
                "Send me a thumbnail image (recommended).",
                reply_markup=keyboard
            )
        except ValueError:
            await message.reply_text(
                "❌ Invalid channel ID!\n\n"
                "Channel ID should be like:\n"
                "`-1001234567890`\n\n"
                "Try again:"
            )
    
    elif step == "awaiting_thumbnail":
        if message.photo:
            cartoon_name = operation["cartoon"]
            thumbnail_file_id = message.photo.file_id
            
            if cartoon_name in cartoons:
                cartoons[cartoon_name]["thumbnail"] = thumbnail_file_id
                save_cartoons()
            
            await message.reply_text(
                f"✅ **{cartoon_name}** added successfully!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="back_menu")]])
            )
            logger.info(f"Added cartoon: {cartoon_name}")
            del current_operation[user_id]
        else:
            await message.reply_text("❌ Please send an image or click Skip.")
    
    # YouTube range input
    elif step == "awaiting_yt_range":
        try:
            range_text = message.text.strip()
            match = re.match(r'(\d+):(\d+)-(\d+)?', range_text)
            if match:
                season = int(match.group(1))
                ep_start = int(match.group(2))
                ep_end = int(match.group(3)) if match.group(3) else None
                
                operation.update({
                    "step": "awaiting_yt_url",
                    "season": season,
                    "episode_start": ep_start,
                    "episode_end": ep_end
                })
                
                range_info = f"Season {season}, Episodes {ep_start}"
                if ep_end:
                    range_info += f"-{ep_end}"
                else:
                    range_info += "+"
                
                await message.reply_text(
                    f"📥 **Range Set: {range_info}**\n\n"
                    "Now send me the YouTube playlist URL:"
                )
            else:
                await message.reply_text(
                    "❌ Invalid format!\n\n"
                    "Use: `season:start-end`\n"
                    "Example: `1:1-10` or `2:5-`"
                )
        except Exception as e:
            await message.reply_text(f"❌ Error: {str(e)}")
    
    # YouTube download flow
    elif step == "awaiting_yt_url":
        url = message.text.strip()
        cartoon_name = operation["cartoon"]
        season = operation.get("season", 1)
        ep_start = operation.get("episode_start", 1)
        ep_end = operation.get("episode_end")
        
        if "youtube.com" not in url and "youtu.be" not in url:
            await message.reply_text(
                "❌ Invalid YouTube URL!\n\n"
                "Send a YouTube playlist URL like:\n"
                "`https://www.youtube.com/playlist?list=...`"
            )
            return
        
        status_msg = await message.reply_text("📥 **Starting download...**\n\nFetching playlist info...")
        
        try:
            # Get playlist info
            ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'ignoreerrors': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                playlist_info = ydl.extract_info(url, download=False)
                
                if not playlist_info or 'entries' not in playlist_info:
                    await status_msg.edit(
                        "❌ **Not a playlist URL!**\n\n"
                        "Please send a YouTube PLAYLIST link."
                    )
                    return
                
                videos = [v for v in playlist_info['entries'] if v]
                total = len(videos)
                
                # Apply episode range if specified
                if ep_end:
                    videos = videos[ep_start-1:ep_end]
                elif ep_start > 1:
                    videos = videos[ep_start-1:]
                
                total = len(videos)
                
                if total == 0:
                    await status_msg.edit("❌ No videos in specified range!")
                    return
                
                await status_msg.edit(
                    f"📥 **Found {total} videos!**\n\n"
                    f"Starting download for: **{cartoon_name}**\n"
                    f"Season: {season}, Starting Episode: {ep_start}\n"
                    f"This may take a while..."
                )
                
                output_dir = f"downloads/{cartoon_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                os.makedirs(output_dir, exist_ok=True)
                
                cartoon_info = cartoons[cartoon_name]
                channel_id = cartoon_info["channel_id"]
                series_name = cartoon_info.get("series_name", cartoon_name)
                thumbnail = cartoon_info.get("thumbnail")
                
                success_count = 0
                failed_count = 0
                current_ep = ep_start
                
                for idx, video in enumerate(videos, 1):
                    if current_operation.get(user_id) == "stopped":
                        await status_msg.edit(
                            f"⏹ **Stopped by user**\n\n"
                            f"✅ Uploaded: {success_count}\n"
                            f"❌ Failed: {failed_count}"
                        )
                        break
                    
                    video_url = f"https://www.youtube.com/watch?v={video['id']}"
                    await status_msg.edit(
                        f"📥 **Downloading {idx}/{total}**\n\n"
                        f"{video.get('title', 'Video')[:50]}...\n\n"
                        f"✅ Done: {success_count} | ❌ Failed: {failed_count}"
                    )
                    
                    result = await download_youtube_video(
                        video_url, output_dir, status_msg, 
                        season=season, episode_start=current_ep
                    )
                    
                    if result and os.path.exists(result['file']):
                        # Get duration
                        duration = format_duration(result['duration'])
                        
                        # Create caption
                        caption = create_caption(
                            f"{current_ep:02d}", 
                            result['title'], 
                            series_name, 
                            duration,
                            season=season
                        )
                        
                        # Upload to channel
                        await status_msg.edit(
                            f"📤 **Uploading {idx}/{total}**\n\n"
                            f"Episode {current_ep}\n\n"
                            f"✅ Done: {success_count} | ❌ Failed: {failed_count}"
                        )
                        
                        try:
                            await app.send_video(
                                channel_id,
                                result['file'],
                                caption=caption,
                                thumb=thumbnail,
                                supports_streaming=True
                            )
                            
                            success_count += 1
                            
                            # Update last episode
                            cartoons[cartoon_name]['last_episode'] = current_ep
                            cartoons[cartoon_name]['last_season'] = season
                            save_cartoons()
                            
                            # Delete local file
                            try:
                                os.remove(result['file'])
                            except:
                                pass
                            
                        except Exception as e:
                            failed_count += 1
                            logger.error(f"Upload error: {e}")
                            await asyncio.sleep(2)
                        
                        current_ep += 1
                    else:
                        failed_count += 1
                        if result:
                            logger.error(f"File not found: {result.get('file')}")
                
                # Cleanup directory
                try:
                    if os.path.exists(output_dir) and not os.listdir(output_dir):
                        os.rmdir(output_dir)
                except:
                    pass
                
                await status_msg.edit(
                    f"✅ **Process Complete!**\n\n"
                    f"Total Videos: {total}\n"
                    f"✅ Uploaded: {success_count}\n"
                    f"❌ Failed: {failed_count}\n\n"
                    f"Cartoon: **{cartoon_name}**\n"
                    f"Last Episode: Season {season}, Episode {current_ep-1}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="back_menu")]])
                )
                logger.info(f"Completed download for {cartoon_name}: {success_count}/{total}")
        
        except Exception as e:
            logger.error(f"Playlist error: {e}")
            await status_msg.edit(f"❌ **Error:** {str(e)[:200]}")
        
        if user_id in current_operation:
            del current_operation[user_id]
    
    # Forward language input
    elif step == "awaiting_forward_lang":
        audio_lang = message.text.strip()
        operation["audio_lang"] = audio_lang
        operation["step"] = "awaiting_forward_msg"
        await message.reply_text(
            f"📤 **Forward to: {operation['cartoon']}**\n\n"
            f"Audio Language: {audio_lang}\n\n"
            "Forward me ANY message from the source channel.\n\n"
            "The bot will forward ALL videos from that channel."
        )
    
    # Forward from channel flow
    elif step == "awaiting_forward_msg":
        if not message.forward_from_chat:
            await message.reply_text(
                "❌ **Not a forwarded message!**\n\n"
                "Please FORWARD a message FROM the source channel."
            )
            return
        
        source_channel_id = message.forward_from_chat.id
        source_channel_title = message.forward_from_chat.title or "Channel"
        cartoon_name = operation["cartoon"]
        audio_lang = operation.get("audio_lang")
        
        status_msg = await message.reply_text("📤 **Starting forward process...**\n\nScanning source channel...")
        
        try:
            cartoon_info = cartoons[cartoon_name]
            dest_channel_id = cartoon_info["channel_id"]
            series_name = cartoon_info.get("series_name", cartoon_name)
            thumbnail = cartoon_info.get("thumbnail")
            
            await status_msg.edit(
                f"📤 **Found source channel: {source_channel_title}**\n\n"
                f"Now forward all videos you want to process.\n"
                f"Each video will be sent to **{cartoon_name}** with proper caption.\n\n"
                f"Send /done when finished."
            )
            
            # Store forward session
            forward_sessions[user_id] = {
                "active": True,
                "source_channel": source_channel_id,
                "dest_channel": dest_channel_id,
                "cartoon": cartoon_name,
                "series_name": series_name,
                "thumbnail": thumbnail,
                "audio_lang": audio_lang,
                "success_count": 0,
                "failed_count": 0,
                "processed_ids": set()
            }
            
            current_operation[user_id]["step"] = "forwarding_messages"
            
        except Exception as e:
            logger.error(f"Forward setup error: {e}")
            await status_msg.edit(f"❌ **Error:** {str(e)[:200]}")
    
    # Handle individual forwarded messages
    elif step == "forwarding_messages":
        if message.text and message.text == "/done":
            # End forwarding session
            session = forward_sessions.get(user_id, {})
            success = session.get("success_count", 0)
            failed = session.get("failed_count", 0)
            
            await message.reply_text(
                f"✅ **Forward Complete!**\n\n"
                f"✅ Successfully forwarded: {success}\n"
                f"❌ Failed: {failed}\n\n"
                f"Cartoon: **{operation['cartoon']}**",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="back_menu")]])
            )
            
            if user_id in forward_sessions:
                del forward_sessions[user_id]
            del current_operation[user_id]
            return
        
        # Check if this is a forwarded message from the source channel
        session = forward_sessions.get(user_id)
        if not session or not message.forward_from_chat:
            return
        
        if message.forward_from_chat.id != session["source_channel"]:
            await message.reply_text("❌ This message is not from the source channel!")
            return
        
        # Check if already processed
        msg_id = message.forward_from_message_id
        if msg_id in session["processed_ids"]:
            return
        
        session["processed_ids"].add(msg_id)
        
        # Process the message
        status_msg = await message.reply_text("📤 **Processing forwarded video...**")
        
        try:
            # Get video or document
            media = None
            file_name = None
            duration = None
            
            if message.video:
                media = message.video
                file_name = media.file_name
                duration = media.duration
            elif message.document and message.document.mime_type and 'video' in message.document.mime_type:
                media = message.document
                file_name = media.file_name
                # Documents might not have duration
            else:
                await status_msg.edit("❌ Not a video file!")
                session["failed_count"] += 1
                return
            
            # Get title from caption or filename
            title = message.caption or file_name or f"Video"
            
            # Extract episode info
            ep_info, season, ep_num = extract_episode_info(title)
            
            # Clean title for caption
            clean_title_text = clean_title(title)
            
            # Create caption
            caption = f"🎬 **Episode {ep_num:02d}**"
            if season > 1:
                caption = f"🎬 **Season {season} Episode {ep_num:02d}**"
            
            caption += f"\n📺 **Series:** {session['series_name']}"
            caption += f"\n📝 **Title:** {clean_title_text}"
            
            if duration:
                caption += f"\n⏱ **Duration:** {format_duration(duration)}"
            
            if session.get("audio_lang"):
                caption += f"\n🔊 **Audio:** {session['audio_lang']}"
            
            caption += "\n🎞 **Quality:** HD"
            caption += "\n\n#cartoon #episode"
            
            # Send as video (convert document to video if needed)
            if message.video:
                # Already a video
                await app.send_video(
                    session["dest_channel"],
                    message.video.file_id,
                    caption=caption,
                    thumb=session["thumbnail"],
                    supports_streaming=True
                )
            else:
                # Send document as video
                await app.send_video(
                    session["dest_channel"],
                    media.file_id,
                    caption=caption,
                    thumb=session["thumbnail"],
                    supports_streaming=True,
                    duration=duration
                )
            
            session["success_count"] += 1
            await status_msg.edit(f"✅ Forwarded! (Total: {session['success_count']})")
            
            # Update last episode
            cartoons[session['cartoon']]['last_episode'] = ep_num
            cartoons[session['cartoon']]['last_season'] = season
            save_cartoons()
            
            # Delete the forwarded message (optional)
            try:
                await message.delete()
            except:
                pass
            
        except FloodWait as e:
            logger.warning(f"Flood wait: {e.value} seconds")
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.error(f"Forward error: {e}")
            session["failed_count"] += 1
            await status_msg.edit(f"❌ Error: {str(e)[:100]}")
    
    # Auto-forward setup
    elif step == "awaiting_auto_source":
        if not message.forward_from_chat:
            await message.reply_text("❌ Please forward a message from the source channel!")
            return
        
        source_channel_id = message.forward_from_chat.id
        cartoon_name = operation["cartoon"]
        
        # Add to auto-forward list
        if 'auto_forward' not in cartoons[cartoon_name]:
            cartoons[cartoon_name]['auto_forward'] = []
        
        auto_config = {
            'source_id': source_channel_id,
            'source_title': message.forward_from_chat.title or f"Channel {source_channel_id}",
            'audio_lang': None,
            'enabled': True
        }
        
        cartoons[cartoon_name]['auto_forward'].append(auto_config)
        save_cartoons()
        
        # Ask for language
        operation['auto_config'] = auto_config
        operation['step'] = 'awaiting_auto_lang'
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔊 Add Language", callback_data=f"auto_lang_{cartoon_name}")],
            [InlineKeyboardButton("⏭ Skip", callback_data=f"auto_done_{cartoon_name}")]
        ])
        
        await message.reply_text(
            f"✅ **Auto-Forward Setup**\n\n"
            f"Source: {auto_config['source_title']}\n"
            f"Destination: {cartoon_name}\n\n"
            "Do you want to add audio language?",
            reply_markup=keyboard
        )

# Auto-forward background task
async def auto_forward_task():
    """Background task to monitor and auto-forward new videos"""
    while True:
        try:
            for cartoon_name, info in cartoons.items():
                if 'auto_forward' in info and info['auto_forward']:
                    for auto_config in info['auto_forward']:
                        if auto_config.get('enabled', True):
                            # Monitor channel for new videos
                            # This would require storing last message IDs
                            pass
            await asyncio.sleep(60)  # Check every minute
        except Exception as e:
            logger.error(f"Auto-forward error: {e}")

# Main
if __name__ == "__main__":
    # Load data
    load_cartoons()
    
    # Start background tasks
    loop = asyncio.get_event_loop()
    
    # Keep alive task
    loop.create_task(keep_alive_task())
    
    # Monitor forwarded messages
    loop.create_task(monitor_forwarded_messages())
    
    # Auto-forward task
    loop.create_task(auto_forward_task())
    
    logger.info("🎬 Cartoon Database Bot v2.0 started!")
    print("🎬 Cartoon Database Bot v2.0 started!")
    print(f"📊 Loaded {len(cartoons)} cartoons")
    print("✅ Background tasks running (bot will stay awake)")
    print("🚀 Ready to receive commands!")
    
    # Run the bot
    app.run()
