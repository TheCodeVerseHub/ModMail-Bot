"""
Modmail cog: Users DM the bot, messages are forwarded to a modmail channel. Mods can reply from the channel.
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands
from utils.config import Config
from typing import Optional, Dict, Any, Union
import asyncio
import json
from pathlib import Path
from datetime import datetime, timedelta
import logging
import random

logger = logging.getLogger(__name__)



class ModMail(commands.Cog):
    # Session format per user_id:
    # { 'thread_id': int, 'last_activity': ISO8601 timestamp }
    modmail_sessions: Dict[int, Dict[str, Any]] = {}
    _session_locks: Dict[int, asyncio.Lock] = {}
    SESSIONS_FILE = Path("data/modmail_sessions.json")

    def __init__(self, bot: commands.Bot, config: Config):
        self.bot = bot
        self.config = config
        self.modmail_channel_id: Optional[int] = getattr(config, 'modmail_channel_id', None)
        self.RESET_DELAY_SECONDS: int = getattr(config, 'modmail_reset_seconds', 600)
        self._dm_semaphore: asyncio.Semaphore = asyncio.Semaphore(2)
        self._dm_channel_cache: Dict[int, discord.DMChannel] = {}
        self._webhook: Optional[discord.Webhook] = None
        
        try:
            self._load_sessions_from_file()
        except Exception:
            logger.exception("modmail: failed to load persisted sessions")
        
        self.cleanup_inactive_sessions.start()

    def cog_unload(self):
        self.cleanup_inactive_sessions.cancel()

    async def _send_with_retry(self, send_func, *args, max_retries=3, **kwargs):
        for attempt in range(max_retries):
            try:
                return await send_func(*args, **kwargs)
            except discord.errors.HTTPException as e:
                if e.status == 429 and attempt < max_retries - 1:
                    retry_after = getattr(e, 'retry_after', None) or (2 ** attempt) + random.uniform(0, 1)
                    await asyncio.sleep(retry_after)
                else:
                    raise
            except Exception:
                raise
    
    async def _send_dm_safe(self, user: Union[discord.User, discord.Member], **kwargs):
        async with self._dm_semaphore:
            dm_channel = self._dm_channel_cache.get(user.id)
            if dm_channel is None:
                if isinstance(user, discord.Member):
                    actual_user = user._user
                else:
                    actual_user = user
                dm_channel = await actual_user.create_dm()
                self._dm_channel_cache[user.id] = dm_channel
            
            return await self._send_with_retry(dm_channel.send, **kwargs)

    async def _get_or_create_webhook(self, channel: discord.TextChannel) -> discord.Webhook:
        if self._webhook:
            return self._webhook
        
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.token: # Ensure we can use it
                self._webhook = wh
                return wh
        
        self._webhook = await channel.create_webhook(name="ModMail Relay")
        return self._webhook

    def _load_sessions_from_file(self):
        if not self.SESSIONS_FILE.exists():
            return
        try:
            with self.SESSIONS_FILE.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            for k, v in data.items():
                try:
                    self.modmail_sessions[int(k)] = v
                except Exception:
                    logger.exception(f"modmail: failed to load session for key {k}")
        except Exception:
            logger.exception("modmail: error reading sessions file")

    def _persist_sessions_to_file(self):
        try:
            self.SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            dumpable = {str(k): v for k, v in self.modmail_sessions.items()}
            with self.SESSIONS_FILE.open("w", encoding="utf-8") as fh:
                json.dump(dumpable, fh)
        except Exception:
            logger.exception("modmail: failed to persist sessions to file")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Handle DM -> ModMail Thread
        if isinstance(message.channel, discord.DMChannel):
            await self.handle_dm_message(message)
            return

        # Handle Thread -> DM (Mod Reply)
        if isinstance(message.channel, discord.Thread):
            await self.handle_thread_reply(message)

    async def handle_dm_message(self, message: discord.Message):
        try:
            user_id = message.author.id
            session = self.modmail_sessions.get(user_id)
            
            if not self.modmail_channel_id:
                 await message.channel.send("ModMail system is currently disabled (Channel not set).")
                 return
                 
            main_channel = self.bot.get_channel(self.modmail_channel_id)
            if not main_channel or not isinstance(main_channel, discord.TextChannel):
                 await message.channel.send("ModMail system is unavailable (Invalid channel configuration).")
                 return

            webhook = await self._get_or_create_webhook(main_channel)

            if not session:
                # Create new session
                try:
                    thread = await main_channel.create_thread(name=f"ModMail - {message.author.name}", type=discord.ChannelType.private_thread)
                except discord.HTTPException:
                    # Fallback to public thread if private threads fail (e.g. no boost)
                    thread = await main_channel.create_thread(name=f"ModMail - {message.author.name}")

                # Log to main channel
                try:
                    log_embed = discord.Embed(
                        title="📨 New ModMail Created",
                        description=f"**User:** {message.author.mention} (`{message.author.id}`)\n**Thread:** {thread.mention}",
                        color=discord.Color.gold(),
                        timestamp=datetime.utcnow()
                    )
                    log_embed.set_thumbnail(url=message.author.display_avatar.url)
                    await main_channel.send(content="@here", embed=log_embed)
                except Exception as e:
                    logger.error(f"Failed to send modmail log: {e}")

                # Notify user
                await self._send_dm_safe(message.author, embed=discord.Embed(
                    title="ModMail Started", 
                    description="A session has been started with the moderators. Messages you send here will be forwarded to them.",
                    color=discord.Color.default()
                ))

                # Send initial message via webhook
                files = [await f.to_file() for f in message.attachments]
                try:
                    await webhook.send(
                        content=message.content,
                        username=message.author.name,
                        avatar_url=message.author.display_avatar.url,
                        thread=thread,
                        files=files
                    )
                except Exception as e:
                    await thread.send(f"Failed to relay message from user: {e}")
                    raise e
                
                self.modmail_sessions[user_id] = {
                    'thread_id': thread.id,
                    'last_activity': datetime.utcnow().isoformat()
                }
            else:
                # Continue session
                thread_id = session.get('thread_id')
                thread = None
                if thread_id:
                    thread = main_channel.get_thread(int(thread_id))

                if not thread:
                     # Thread deleted manually? Re-create
                     try:
                        thread = await main_channel.create_thread(name=f"ModMail - {message.author.name}", type=discord.ChannelType.private_thread)
                     except discord.HTTPException:
                        thread = await main_channel.create_thread(name=f"ModMail - {message.author.name}")

                     session['thread_id'] = thread.id
                     # Optionally notify mods that user is back but thread was lost
                     await thread.send(f"Wait, previous thread was lost. Resuming session for {message.author.mention}.")

                files = [await f.to_file() for f in message.attachments]
                try:
                    await webhook.send(
                        content=message.content,
                        username=message.author.name,
                        avatar_url=message.author.display_avatar.url,
                        thread=thread,
                        files=files
                    )
                except Exception as e:
                     await thread.send(f"Failed to relay message from user: {e}")
                     raise e
                session['last_activity'] = datetime.utcnow().isoformat()

            self._persist_sessions_to_file()
        except Exception as e:
            logger.exception(f"Error handling DM message from {message.author.id}")
            try:
                await message.channel.send(f"❌ An internal error occurred: {str(e)}")
            except:
                pass

    async def handle_thread_reply(self, message: discord.Message):
        # Find user_id from thread_id
        session_user_id = None
        for uid, data in self.modmail_sessions.items():
            if data.get('thread_id') == message.channel.id:
                session_user_id = uid
                break
        
        if not session_user_id:
            return # Not a modmail thread

        # Ignore commands
        prefixes = await self.bot.get_prefix(message)
        if isinstance(prefixes, str):
            prefixes = [prefixes]
            
        if message.content.startswith(tuple([p + "close" for p in prefixes])):
             return
        if message.content.startswith("!!close"):
             return

        user = self.bot.get_user(session_user_id)
        if not user:
            await message.channel.send("⚠️ User cannot be found (might have left shared servers).")
            return

        try:
             files = [await f.to_file() for f in message.attachments]
             embed = discord.Embed(
                 title="A Moderator has Replied",
                 description="> "+message.content, 
                 color=discord.Color.from_str("#00ff00")
             )
             # embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
             await self._send_dm_safe(user, embed=embed, files=files)
             
             self.modmail_sessions[session_user_id]['last_activity'] = datetime.utcnow().isoformat()
             self._persist_sessions_to_file()
             # Optional: React to confirm sent
             await message.add_reaction("✅")
        except Exception as e:
            await message.channel.send(f"❌ Failed to send to user: {e}")

    @commands.command(name="close", aliases=["mclose"])
    async def close_session(self, ctx):
        if not isinstance(ctx.channel, discord.Thread):
             return

        session_user_id = None
        for uid, data in self.modmail_sessions.items():
            if data.get('thread_id') == ctx.channel.id:
                session_user_id = uid
                break

        if not session_user_id:
            await ctx.send("This is not a active modmail thread.")
            return

        # Close session
        del self.modmail_sessions[session_user_id]
        self._persist_sessions_to_file()
        
        user = self.bot.get_user(session_user_id)
        if user:
            try:
                await user.send(embed=discord.Embed(
                    title="Session Closed", 
                    description="This modmail session has been closed by a moderator.",
                    color=discord.Color.default()
                ))
            except:
                pass

        # Log closure to main channel
        if self.modmail_channel_id:
             main_channel = self.bot.get_channel(self.modmail_channel_id)
             if main_channel and isinstance(main_channel, discord.TextChannel):
                 try:
                    log_embed = discord.Embed(
                        title="📪 ModMail Closed",
                        description=f"**User:** <@{session_user_id}> (`{session_user_id}`)\n**Thread:** {ctx.channel.mention}\n**Closed By:** {ctx.author.mention}",
                        color=discord.Color.from_str("#ff0000"),
                        timestamp=datetime.utcnow()
                    )
                    await main_channel.send(embed=log_embed)
                 except Exception as e:
                    logger.error(f"Failed to send modmail close log: {e}")
        
        await ctx.send("Session closed. Archiving thread...")
        
        new_name = f"🔒 {ctx.channel.name}"
        if len(new_name) > 100:
            new_name = new_name[:100]
            
        await ctx.channel.edit(name=new_name, archived=True, locked=True)

    @tasks.loop(minutes=1)
    async def cleanup_inactive_sessions(self):
        now = datetime.utcnow()
        to_remove = []
        
        for user_id, session in self.modmail_sessions.items():
            last_activity_str = session.get('last_activity')
            if not last_activity_str:
                continue
                
            last_activity = datetime.fromisoformat(last_activity_str)
            if (now - last_activity).total_seconds() > self.RESET_DELAY_SECONDS:
                to_remove.append(user_id)

        for user_id in to_remove:
            session = self.modmail_sessions.pop(user_id)
            thread_id = session.get('thread_id')
            
            # Close thread
            if self.modmail_channel_id:
                channel = self.bot.get_channel(self.modmail_channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    thread = None
                    if thread_id:
                        thread = channel.get_thread(int(thread_id))
                    if thread:
                         try:
                             await thread.send("Session timed out due to inactivity.")
                             await thread.edit(archived=True, locked=True)
                         except:
                             pass
            
            # Notify User
            user = self.bot.get_user(user_id)
            if user:
                 try:
                     await user.send(embed=discord.Embed(
                         title="Session Closed",
                         description="Modmail session timed out due to inactivity.",
                         color=discord.Color.default()
                     ))
                 except:
                     pass
        
        if to_remove:
            self._persist_sessions_to_file()

    @commands.command(name="set_modmail_channel")
    @commands.has_permissions(administrator=True)
    async def set_modmail_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        if not channel:
            channel = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
        if not channel:
            await ctx.send("Please mention a text channel or use this command in a text channel.")
            return
        self.modmail_channel_id = channel.id
        await ctx.send(f"Modmail channel set to {channel.mention}.")

    @app_commands.command(name="set_modmail_channel", description="Set the modmail channel (admin only)")
    @app_commands.describe(channel="Channel to set as modmail")
    async def set_modmail_channel_slash(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        member = None
        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
        if not (member and member.guild_permissions.administrator):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
            return
        if not channel:
            if isinstance(interaction.channel, discord.TextChannel):
                channel = interaction.channel
            else:
                await interaction.response.send_message("Please specify a text channel or use this in a text channel.", ephemeral=True)
                return
        self.modmail_channel_id = channel.id
        await interaction.response.send_message(f"Modmail channel set to {channel.mention}.", ephemeral=True)

async def setup(bot):
    config = getattr(bot, 'config', None)
    if config is None:
        raise RuntimeError("Bot config is missing. Cannot load ModMail cog.")
    await bot.add_cog(ModMail(bot, config))
