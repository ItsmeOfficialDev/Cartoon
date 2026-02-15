import os
import re
import asyncio
import json
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.enums import ParseMode
import yt_dlp
from pathlib import Path

# Bot configuration
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID"))  # Your Telegram user ID

# Initialize bot
app = Client("cartoon_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Storage for cartoons (cartoon_name: {channel_id, thumbnail_file_id, language, series_name})
cartoons = {}
current_operation = {}  # To track ongoing operations for stop functionality
download_queue = asyncio.Queue()
MAX_CONCURRENT_DOWNLOADS = 1  # Start with 1, can increase if speed is good

# Load cartoons from JSON if exists
def load_cartoons():
    global cartoons
    if os.path.exists("cartoons.json"):
        with open("cartoons.json", "r") as f:
            cartoons = json.load(f)

def save_cartoons():
    with open("cartoons.json", "w") as f:
        json.dump(cartoons, f, indent=2)

# Extract episode number from title
def extract_episode_number(title):
    """Extract episode number from various formats"""
    title = title.upper()
    
    # Patterns to match
    patterns = [
        r'S(\d+)E(\d+)',  # S01E04
        r'SEASON\s*(\d+).*?EPISODE\s*(\d+)',  # Season 1 Episode 4
        r'EP\.?\s*(\d+)',  # Ep. 1 or Ep 1
        r'EPISODE\s*(\d+)',  # Episode 1
        r'E(\d+)',  # E04
        r'#(\d+)',  # #1
        r'\b(\d+)\b',  # Just a number
    ]
    
    for pattern in patterns:
        match = re.search(pattern, title)
        if match:
            groups = match.groups()
            if len(groups) == 2:  # Season and episode
                return f"{int(groups[0]):02d}E{int(groups[1]):02d}"
            elif len(groups) == 1:  # Just episode
                return f"{int(groups[0]):02d}"
    
    return "01"  # Default if nothing found

# Format duration
def format_duration(seconds):
    """Convert seconds to readable format"""
    if seconds:
        minutes = int(seconds // 60)
        return f"{minutes} min"
    return "Unknown"

# Create caption
def create_caption(episode_num, title, series_name, duration):
    """Create formatted caption"""
    # Clean title - remove episode numbers and extra info
    clean_title = re.sub(r'(S\d+E\d+|EP\.?\s*\d+|EPISODE\s*\d+|E\d+|\[\d+\]|\(\d+\))', '', title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    
    if not clean_title:
        clean_title = title
    
    caption = f"""🎬 Episode {episode_num} – {clean_title}
📺 Series: {series_name}
🕒 Duration: {duration}
🎞 Quality: HD"""
    
    return caption

# Download YouTube video
async def download_youtube_video(url, output_path, progress_msg=None):
    """Download a single YouTube video"""
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': f'{output_path}/%(title)s.%(ext)s',
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            return {
                'file': filename,
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0)
            }
    except Exception as e:
        if progress_msg:
            await progress_msg.edit(f"❌ Error downloading: {str(e)}")
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
        "Choose an option below:",
        reply_markup=main_menu()
    )

# Stop command
@app.on_message(filters.command("stop") & filters.user(OWNER_ID))
async def stop_operation(client, message):
    user_id = message.from_user.id
    if user_id in current_operation:
        current_operation[user_id] = "stopped"
        await message.reply_text("⏹ Stopping current operation...")
    else:
        await message.reply_text("No ongoing operation to stop.")

# Callback query handler
@app.on_callback_query(filters.user(OWNER_ID))
async def callback_handler(client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    if data == "add_cartoon":
        await callback_query.message.edit_text(
            "📝 **Add New Cartoon**\n\n"
            "Please send the cartoon name:"
        )
        current_operation[user_id] = {"step": "awaiting_name"}
    
    elif data == "download_yt":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet!", show_alert=True)
            return
        
        keyboard = []
        for name in cartoons.keys():
            keyboard.append([InlineKeyboardButton(name, callback_data=f"yt_{name}")])
        keyboard.append([InlineKeyboardButton("« Back", callback_data="back_menu")])
        
        await callback_query.message.edit_text(
            "📥 **Download YouTube Playlist**\n\n"
            "Select cartoon:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data.startswith("yt_"):
        cartoon_name = data[3:]
        current_operation[user_id] = {"step": "awaiting_yt_url", "cartoon": cartoon_name}
        await callback_query.message.edit_text(
            f"📥 **Download Playlist for {cartoon_name}**\n\n"
            "Send me the YouTube playlist URL:"
        )
    
    elif data == "forward_channel":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet!", show_alert=True)
            return
        
        keyboard = []
        for name in cartoons.keys():
            keyboard.append([InlineKeyboardButton(name, callback_data=f"fwd_{name}")])
        keyboard.append([InlineKeyboardButton("« Back", callback_data="back_menu")])
        
        await callback_query.message.edit_text(
            "📤 **Forward from Channel**\n\n"
            "Select cartoon:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data.startswith("fwd_"):
        cartoon_name = data[4:]
        current_operation[user_id] = {"step": "awaiting_forward_msg", "cartoon": cartoon_name}
        await callback_query.message.edit_text(
            f"📤 **Forward to {cartoon_name}**\n\n"
            "Forward me ANY message from the source channel:"
        )
    
    elif data == "list_cartoons":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet!", show_alert=True)
            return
        
        text = "📋 **Your Cartoons:**\n\n"
        for name, info in cartoons.items():
            text += f"• **{name}**\n"
            text += f"  Channel: `{info['channel_id']}`\n"
            text += f"  Series Name: {info.get('series_name', name)}\n"
            text += f"  Thumbnail: {'✅ Set' if info.get('thumbnail') else '❌ Not set'}\n\n"
        
        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_menu")]]
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "remove_cartoon":
        if not cartoons:
            await callback_query.answer("❌ No cartoons added yet!", show_alert=True)
            return
        
        keyboard = []
        for name in cartoons.keys():
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
            "Send me the destination channel ID (example: -1001234567890):"
        )
    
    elif step == "awaiting_channel":
        try:
            channel_id = int(message.text.strip())
            cartoon_name = operation["cartoon"]
            
            cartoons[cartoon_name] = {
                "channel_id": channel_id,
                "series_name": cartoon_name,
                "thumbnail": None
            }
            
            operation["step"] = "awaiting_thumbnail"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Skip Thumbnail", callback_data="skip_thumbnail")]])
            await message.reply_text(
                f"📝 **Adding: {cartoon_name}**\n\n"
                "Send me a thumbnail image (or skip):",
                reply_markup=keyboard
            )
        except ValueError:
            await message.reply_text("❌ Invalid channel ID. Please send a valid number.")
    
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
            del current_operation[user_id]
        else:
            await message.reply_text("❌ Please send an image or click Skip.")
    
    # YouTube download flow
    elif step == "awaiting_yt_url":
        url = message.text.strip()
        cartoon_name = operation["cartoon"]
        
        if "youtube.com" not in url and "youtu.be" not in url:
            await message.reply_text("❌ Please send a valid YouTube URL.")
            return
        
        status_msg = await message.reply_text("📥 Starting download... Please wait.")
        
        try:
            # Get playlist info
            ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                playlist_info = ydl.extract_info(url, download=False)
                
                if 'entries' in playlist_info:
                    videos = playlist_info['entries']
                    total = len(videos)
                    
                    await status_msg.edit(f"📥 Found {total} videos in playlist. Starting download...")
                    
                    output_dir = f"downloads/{cartoon_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    os.makedirs(output_dir, exist_ok=True)
                    
                    cartoon_info = cartoons[cartoon_name]
                    channel_id = cartoon_info["channel_id"]
                    series_name = cartoon_info.get("series_name", cartoon_name)
                    thumbnail = cartoon_info.get("thumbnail")
                    
                    for idx, video in enumerate(videos, 1):
                        if current_operation.get(user_id) == "stopped":
                            await status_msg.edit("⏹ Download stopped by user.")
                            break
                        
                        video_url = f"https://www.youtube.com/watch?v={video['id']}"
                        await status_msg.edit(f"📥 Downloading {idx}/{total}: {video.get('title', 'Video')[:50]}...")
                        
                        result = await download_youtube_video(video_url, output_dir, status_msg)
                        
                        if result:
                            # Extract episode number
                            ep_num = extract_episode_number(result['title'])
                            duration = format_duration(result['duration'])
                            
                            # Create caption
                            caption = create_caption(ep_num, result['title'], series_name, duration)
                            
                            # Upload to channel
                            await status_msg.edit(f"📤 Uploading {idx}/{total}...")
                            
                            try:
                                if thumbnail:
                                    await app.send_video(
                                        channel_id,
                                        result['file'],
                                        caption=caption,
                                        thumb=thumbnail,
                                        supports_streaming=True
                                    )
                                else:
                                    await app.send_video(
                                        channel_id,
                                        result['file'],
                                        caption=caption,
                                        supports_streaming=True
                                    )
                                
                                # Delete local file
                                os.remove(result['file'])
                                
                            except Exception as e:
                                await status_msg.edit(f"❌ Upload error for video {idx}: {str(e)}")
                                await asyncio.sleep(2)
                    
                    # Cleanup
                    if os.path.exists(output_dir):
                        try:
                            os.rmdir(output_dir)
                        except:
                            pass
                    
                    await status_msg.edit(
                        f"✅ Completed! Processed {total} videos.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="back_menu")]])
                    )
                else:
                    await status_msg.edit("❌ Not a playlist URL. Please send a playlist URL.")
        
        except Exception as e:
            await status_msg.edit(f"❌ Error: {str(e)}")
        
        if user_id in current_operation:
            del current_operation[user_id]
    
    # Forward from channel flow
    elif step == "awaiting_forward_msg":
        if message.forward_from_chat:
            source_channel_id = message.forward_from_chat.id
            cartoon_name = operation["cartoon"]
            
            status_msg = await message.reply_text("📤 Starting to forward messages...")
            
            try:
                cartoon_info = cartoons[cartoon_name]
                dest_channel_id = cartoon_info["channel_id"]
                series_name = cartoon_info.get("series_name", cartoon_name)
                thumbnail = cartoon_info.get("thumbnail")
                
                # Get all messages from source channel
                messages_to_forward = []
                async for msg in app.get_chat_history(source_channel_id, limit=1000):
                    if msg.video or msg.document:
                        messages_to_forward.append(msg)
                
                messages_to_forward.reverse()  # Process from oldest to newest
                
                total = len(messages_to_forward)
                await status_msg.edit(f"📤 Found {total} video/file messages. Starting forward...")
                
                for idx, msg in enumerate(messages_to_forward, 1):
                    if current_operation.get(user_id) == "stopped":
                        await status_msg.edit("⏹ Forward stopped by user.")
                        break
                    
                    await status_msg.edit(f"📤 Processing {idx}/{total}...")
                    
                    # Get title from caption or filename
                    title = ""
                    if msg.caption:
                        title = msg.caption
                    elif msg.video:
                        title = msg.video.file_name or "Video"
                    elif msg.document:
                        title = msg.document.file_name or "File"
                    
                    # Extract episode number
                    ep_num = extract_episode_number(title)
                    
                    # Get duration
                    duration = "Unknown"
                    if msg.video and msg.video.duration:
                        duration = format_duration(msg.video.duration)
                    
                    # Create caption
                    caption = create_caption(ep_num, title, series_name, duration)
                    
                    try:
                        if msg.video:
                            # Forward as video with new caption
                            file_id = msg.video.file_id
                            if thumbnail:
                                await app.send_video(
                                    dest_channel_id,
                                    file_id,
                                    caption=caption,
                                    thumb=thumbnail,
                                    supports_streaming=True
                                )
                            else:
                                await app.send_video(
                                    dest_channel_id,
                                    file_id,
                                    caption=caption,
                                    supports_streaming=True
                                )
                        elif msg.document:
                            # Download and re-upload as video if it's a video file
                            file_id = msg.document.file_id
                            if thumbnail:
                                await app.send_video(
                                    dest_channel_id,
                                    file_id,
                                    caption=caption,
                                    thumb=thumbnail,
                                    supports_streaming=True
                                )
                            else:
                                await app.send_video(
                                    dest_channel_id,
                                    file_id,
                                    caption=caption,
                                    supports_streaming=True
                                )
                        
                        await asyncio.sleep(1)  # Small delay to avoid flood
                    
                    except Exception as e:
                        await status_msg.edit(f"❌ Error forwarding message {idx}: {str(e)}")
                        await asyncio.sleep(2)
                
                await status_msg.edit(
                    f"✅ Completed! Forwarded {total} messages.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="back_menu")]])
                )
            
            except Exception as e:
                await status_msg.edit(f"❌ Error: {str(e)}")
            
            if user_id in current_operation:
                del current_operation[user_id]
        else:
            await message.reply_text("❌ Please forward a message FROM the source channel.")

# Main
if __name__ == "__main__":
    load_cartoons()
    print("🎬 Cartoon Database Bot started!")
    app.run()
