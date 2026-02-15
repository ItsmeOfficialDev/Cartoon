import os
import re
import asyncio
import json
from datetime import datetime
from pathlib import Path
import logging
import sys

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
    # For older Python versions, ensure an event loop exists
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

# Now import pyrogram after event loop is created
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.enums import ParseMode
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
MAX_CONCURRENT_DOWNLOADS = 2  # Can handle 2 parallel downloads

# Load/Save cartoons
def load_cartoons():
    global cartoons
    try:
        if os.path.exists("cartoons.json"):
            with open("cartoons.json", "r") as f:
                cartoons = json.load(f)
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

# Extract episode number from title
def extract_episode_number(title):
    """Extract episode number from various formats"""
    title_upper = title.upper()
    
    # Patterns to match (in priority order)
    patterns = [
        (r'S(\d+)\s*E(\d+)', lambda m: f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"),  # S01E04
        (r'SEASON\s*(\d+).*?EP(?:ISODE)?\s*(\d+)', lambda m: f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"),  # Season 1 Episode 4
        (r'(\d+)X(\d+)', lambda m: f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"),  # 1x04
        (r'EP\.?\s*(\d+)', lambda m: f"{int(m.group(1)):02d}"),  # Ep. 1 or Ep 1
        (r'EPISODE\s*(\d+)', lambda m: f"{int(m.group(1)):02d}"),  # Episode 1
        (r'E(\d+)', lambda m: f"{int(m.group(1)):02d}"),  # E04
        (r'\[(\d+)\]', lambda m: f"{int(m.group(1)):02d}"),  # [1]
        (r'#(\d+)', lambda m: f"{int(m.group(1)):02d}"),  # #1
        (r'-\s*(\d+)\s*-', lambda m: f"{int(m.group(1)):02d}"),  # - 1 -
        (r'^\s*(\d+)\s*[-.]', lambda m: f"{int(m.group(1)):02d}"),  # 1. or 1-
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
        if 1 <= num <= 999:  # Reasonable episode range
            return f"{num:02d}"
    
    return "01"  # Default

# Format duration
def format_duration(seconds):
    """Convert seconds to readable format"""
    if seconds:
        minutes = int(seconds // 60)
        hours = minutes // 60
        mins = minutes % 60
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{minutes} min"
    return "Unknown"

# Create caption
def create_caption(episode_num, title, series_name, duration):
    """Create formatted caption"""
    # Clean title - remove episode indicators and file extensions
    clean_title = title
    
    # Remove common patterns
    patterns_to_remove = [
        r'S\d+E\d+',
        r'\d+x\d+',
        r'EP\.?\s*\d+',
        r'EPISODE\s*\d+',
        r'E\d+',
        r'\[\d+\]',
        r'\(\d+\)',
        r'\.(mp4|mkv|avi|mov|wmv|flv|webm)$',
        r'-\s*\d+\s*-',
        r'^\s*\d+\s*[-.]',
        r'#\d+',
    ]
    
    for pattern in patterns_to_remove:
        clean_title = re.sub(pattern, '', clean_title, flags=re.IGNORECASE)
    
    # Clean up extra spaces and special chars
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    clean_title = re.sub(r'^[-.\s]+|[-.\s]+$', '', clean_title)
    
    if not clean_title or len(clean_title) < 3:
        clean_title = title
    
    caption = f"""🎬 Episode {episode_num} – {clean_title}
📺 Series: {series_name}
🕒 Duration: {duration}
🎞 Quality: HD"""
    
    return caption

# Progress callback for uploads
def progress_callback(current, total, status_msg, idx, total_videos):
    """Callback for tracking upload progress"""
    try:
        percentage = (current / total) * 100
        # Update every 10%
        if int(percentage) % 10 == 0:
            asyncio.create_task(
                status_msg.edit(f"📤 Uploading {idx}/{total_videos} - {percentage:.0f}% complete")
            )
    except:
        pass

# Download YouTube video with better options
async def download_youtube_video(url, output_path, progress_msg=None):
    """Download a single YouTube video with optimization"""
    ydl_opts = {
        'format': 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': f'{output_path}/%(title)s.%(ext)s',
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'concurrent_fragment_downloads': 5,  # Speed up download
        'retries': 3,
        'fragment_retries': 3,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # Handle potential filename issues
            if not os.path.exists(filename):
                # Try to find the actual file
                base = os.path.splitext(filename)[0]
                for ext in ['.mp4', '.mkv', '.webm']:
                    if os.path.exists(base + ext):
                        filename = base + ext
                        break
            
            return {
                'file': filename,
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0)
            }
    except Exception as e:
        logger.error(f"Download error: {e}")
        if progress_msg:
            try:
                await progress_msg.edit(f"❌ Error downloading: {str(e)[:100]}")
            except:
                pass
        return None

# Main menu
def main_menu():
    keyboard = [
        [InlineKeyboardButton("➕ Add Cartoon", callback_data="add_cartoon")],
        [InlineKeyboardButton("📥 Download YouTube Playlist", callback_data="download_yt")],
        [InlineKeyboardButton("📤 Forward from Channel", callback_data="forward_channel")],
        [InlineKeyboardButton("📋 List Cartoons", callback_data="list_cartoons")],
        [InlineKeyboardButton("🗑 Remove Cartoon", callback_data="remove_cartoon")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Start command
@app.on_message(filters.command("start") & filters.user(OWNER_ID))
async def start(client, message):
    await message.reply_text(
        "🎬 **Cartoon Database Bot**\n\n"
        "Welcome! Use the buttons below to manage your cartoons.\n\n"
        "Features:\n"
        "• Download YouTube playlists in HD\n"
        "• Forward videos from channels\n"
        "• Auto-detect episode numbers\n"
        "• Custom thumbnails\n"
        "• Formatted captions",
        reply_markup=main_menu()
    )

# Stop command
@app.on_message(filters.command("stop") & filters.user(OWNER_ID))
async def stop_operation(client, message):
    user_id = message.from_user.id
    if user_id in current_operation:
        current_operation[user_id] = "stopped"
        await message.reply_text("⏹ **Stopping operation...**\n\nCurrent task will finish, then stop.")
    else:
        await message.reply_text("❌ No active operation to stop.")

# Help command
@app.on_message(filters.command("help") & filters.user(OWNER_ID))
async def help_command(client, message):
    help_text = """📚 **How to Use:**

**1. Add Cartoon:**
• Click "Add Cartoon"
• Send cartoon name
• Send channel ID (get from @userinfobot)
• Send thumbnail (optional)

**2. Download Playlist:**
• Click "Download YouTube Playlist"
• Choose cartoon
• Send playlist URL
• Wait for download & upload

**3. Forward from Channel:**
• Click "Forward from Channel"
• Choose cartoon
• Forward any message from source
• Bot forwards all videos

**4. Stop Operation:**
• Send /stop to cancel current task

**Tips:**
• Bot auto-detects episode numbers
• Supports multiple formats (S01E04, Ep1, etc.)
• Thumbnails are optional but recommended
• Channel ID should start with -100"""
    
    await message.reply_text(help_text, reply_markup=main_menu())

# Callback query handler
@app.on_callback_query(filters.user(OWNER_ID))
async def callback_handler(client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    if data == "add_cartoon":
        await callback_query.message.edit_text(
            "📝 **Add New Cartoon**\n\n"
            "Send me the cartoon name:\n"
            "(Example: Tom & Jerry, SpongeBob, etc.)"
        )
        current_operation[user_id] = {"step": "awaiting_name"}
    
    elif data == "download_yt":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet! Add one first.", show_alert=True)
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
        await callback_query.message.edit_text(
            f"📥 **Download Playlist: {cartoon_name}**\n\n"
            "Send me the YouTube playlist URL:\n\n"
            "⚠️ Make sure it's a PLAYLIST link, not a single video!"
        )
    
    elif data == "forward_channel":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet! Add one first.", show_alert=True)
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
        current_operation[user_id] = {"step": "awaiting_forward_msg", "cartoon": cartoon_name}
        await callback_query.message.edit_text(
            f"📤 **Forward to: {cartoon_name}**\n\n"
            "Forward me ANY message from the source channel.\n\n"
            "The bot will then forward ALL videos from that channel."
        )
    
    elif data == "list_cartoons":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet!", show_alert=True)
            return
        
        text = "📋 **Your Cartoons:**\n\n"
        for idx, (name, info) in enumerate(sorted(cartoons.items()), 1):
            text += f"{idx}. **{name}**\n"
            text += f"   📍 Channel: `{info['channel_id']}`\n"
            text += f"   📺 Series: {info.get('series_name', name)}\n"
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
            "⚠️ This will delete the cartoon from the bot.\n"
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
                "No thumbnail set. You can add one later by re-creating the cartoon.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="back_menu")]])
            )
            if user_id in current_operation:
                del current_operation[user_id]

# Message handler
@app.on_message(filters.private & filters.user(OWNER_ID) & ~filters.command(["start", "stop", "help"]))
async def message_handler(client, message):
    user_id = message.from_user.id
    
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
            "1. Forward a message from your channel to @userinfobot\n"
            "2. Copy the ID (should look like: -1001234567890)"
        )
    
    elif step == "awaiting_channel":
        try:
            channel_id = message.text.strip()
            
            # Try to convert to int
            if not channel_id.startswith('-'):
                channel_id = '-' + channel_id
            
            channel_id = int(channel_id)
            cartoon_name = operation["cartoon"]
            
            cartoons[cartoon_name] = {
                "channel_id": channel_id,
                "series_name": cartoon_name,
                "thumbnail": None
            }
            
            operation["step"] = "awaiting_thumbnail"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Skip Thumbnail", callback_data="skip_thumbnail")]])
            await message.reply_text(
                f"📝 **Adding: {cartoon_name}**\n\n"
                "Send me a thumbnail image (recommended).\n\n"
                "Or click Skip if you don't have one.",
                reply_markup=keyboard
            )
        except ValueError:
            await message.reply_text(
                "❌ Invalid channel ID!\n\n"
                "Channel ID should be a number like:\n"
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
                f"✅ **{cartoon_name}** added successfully!\n\n"
                f"Channel: `{cartoons[cartoon_name]['channel_id']}`\n"
                f"Thumbnail: ✅ Set",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="back_menu")]])
            )
            logger.info(f"Added cartoon: {cartoon_name}")
            del current_operation[user_id]
        else:
            await message.reply_text("❌ Please send an image or click Skip Thumbnail button.")
    
    # YouTube download flow
    elif step == "awaiting_yt_url":
        url = message.text.strip()
        cartoon_name = operation["cartoon"]
        
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
            ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                playlist_info = ydl.extract_info(url, download=False)
                
                if 'entries' not in playlist_info:
                    await status_msg.edit(
                        "❌ **Not a playlist URL!**\n\n"
                        "Please send a YouTube PLAYLIST link, not a single video."
                    )
                    return
                
                videos = [v for v in playlist_info['entries'] if v]  # Filter out None entries
                total = len(videos)
                
                if total == 0:
                    await status_msg.edit("❌ Playlist is empty!")
                    return
                
                await status_msg.edit(
                    f"📥 **Found {total} videos!**\n\n"
                    f"Starting download for: **{cartoon_name}**\n"
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
                        f"{video.get('title', 'Video')[:60]}...\n\n"
                        f"✅ Done: {success_count} | ❌ Failed: {failed_count}"
                    )
                    
                    result = await download_youtube_video(video_url, output_dir, status_msg)
                    
                    if result and os.path.exists(result['file']):
                        # Extract episode number
                        ep_num = extract_episode_number(result['title'])
                        duration = format_duration(result['duration'])
                        
                        # Create caption
                        caption = create_caption(ep_num, result['title'], series_name, duration)
                        
                        # Upload to channel
                        await status_msg.edit(
                            f"📤 **Uploading {idx}/{total}**\n\n"
                            f"Episode {ep_num}\n\n"
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
                            
                            # Delete local file
                            try:
                                os.remove(result['file'])
                            except:
                                pass
                            
                        except Exception as e:
                            failed_count += 1
                            logger.error(f"Upload error for video {idx}: {e}")
                            await asyncio.sleep(2)
                    else:
                        failed_count += 1
                        if result:
                            logger.error(f"File not found: {result.get('file')}")
                
                # Cleanup directory
                try:
                    if os.path.exists(output_dir):
                        # Check if directory is empty
                        if not os.listdir(output_dir):
                            os.rmdir(output_dir)
                except:
                    pass
                
                await status_msg.edit(
                    f"✅ **Process Complete!**\n\n"
                    f"Total Videos: {total}\n"
                    f"✅ Uploaded: {success_count}\n"
                    f"❌ Failed: {failed_count}\n\n"
                    f"Cartoon: **{cartoon_name}**",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="back_menu")]])
                )
                logger.info(f"Completed playlist download for {cartoon_name}: {success_count}/{total}")
        
        except Exception as e:
            logger.error(f"Playlist error: {e}")
            await status_msg.edit(f"❌ **Error:** {str(e)[:200]}")
        
        if user_id in current_operation:
            del current_operation[user_id]
    
    # Forward from channel flow
    elif step == "awaiting_forward_msg":
        if not message.forward_from_chat:
            await message.reply_text(
                "❌ **Not a forwarded message!**\n\n"
                "Please FORWARD a message FROM the source channel.\n"
                "(Don't just send text)"
            )
            return
        
        source_channel_id = message.forward_from_chat.id
        cartoon_name = operation["cartoon"]
        
        status_msg = await message.reply_text("📤 **Starting forward process...**\n\nScanning source channel...")
        
        try:
            cartoon_info = cartoons[cartoon_name]
            dest_channel_id = cartoon_info["channel_id"]
            series_name = cartoon_info.get("series_name", cartoon_name)
            thumbnail = cartoon_info.get("thumbnail")
            
            # Get all video messages from source channel
            messages_to_forward = []
            async for msg in app.get_chat_history(source_channel_id, limit=2000):
                if msg.video or (msg.document and msg.document.mime_type and 'video' in msg.document.mime_type):
                    messages_to_forward.append(msg)
            
            messages_to_forward.reverse()  # Process oldest to newest
            
            total = len(messages_to_forward)
            
            if total == 0:
                await status_msg.edit("❌ No video messages found in source channel!")
                return
            
            await status_msg.edit(
                f"📤 **Found {total} videos!**\n\n"
                f"Starting forward to: **{cartoon_name}**"
            )
            
            success_count = 0
            failed_count = 0
            
            for idx, msg in enumerate(messages_to_forward, 1):
                if current_operation.get(user_id) == "stopped":
                    await status_msg.edit(
                        f"⏹ **Stopped by user**\n\n"
                        f"✅ Forwarded: {success_count}\n"
                        f"❌ Failed: {failed_count}"
                    )
                    break
                
                await status_msg.edit(
                    f"📤 **Processing {idx}/{total}**\n\n"
                    f"✅ Done: {success_count} | ❌ Failed: {failed_count}"
                )
                
                # Get title from caption or filename
                title = ""
                if msg.caption:
                    title = msg.caption
                elif msg.video:
                    title = msg.video.file_name or f"Video {idx}"
                elif msg.document:
                    title = msg.document.file_name or f"Video {idx}"
                
                # Extract episode number
                ep_num = extract_episode_number(title)
                
                # Get duration
                duration = "Unknown"
                if msg.video and msg.video.duration:
                    duration = format_duration(msg.video.duration)
                elif msg.document and hasattr(msg.document, 'duration'):
                    duration = format_duration(msg.document.duration)
                
                # Create caption
                caption = create_caption(ep_num, title, series_name, duration)
                
                try:
                    file_id = msg.video.file_id if msg.video else msg.document.file_id
                    
                    await app.send_video(
                        dest_channel_id,
                        file_id,
                        caption=caption,
                        thumb=thumbnail,
                        supports_streaming=True
                    )
                    
                    success_count += 1
                    await asyncio.sleep(1)  # Prevent flood
                    
                except Exception as e:
                    failed_count += 1
                    logger.error(f"Forward error for message {idx}: {e}")
                    await asyncio.sleep(2)
            
            await status_msg.edit(
                f"✅ **Forward Complete!**\n\n"
                f"Total Videos: {total}\n"
                f"✅ Forwarded: {success_count}\n"
                f"❌ Failed: {failed_count}\n\n"
                f"Cartoon: **{cartoon_name}**",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="back_menu")]])
            )
            logger.info(f"Completed forward for {cartoon_name}: {success_count}/{total}")
        
        except Exception as e:
            logger.error(f"Forward error: {e}")
            await status_msg.edit(f"❌ **Error:** {str(e)[:200]}")
        
        if user_id in current_operation:
            del current_operation[user_id]

# Main
if __name__ == "__main__":
    load_cartoons()
    logger.info("🎬 Cartoon Database Bot started!")
    print("🎬 Cartoon Database Bot started!")
    print(f"📊 Loaded {len(cartoons)} cartoons")
    print("✅ Ready to receive commands!")
    app.run()
