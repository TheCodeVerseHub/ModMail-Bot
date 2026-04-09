"""
Modmail cog: Users DM the bot, messages are forwarded to a modmail channel. Mods can reply from the channel.
"""
import discord
from discord.ext import commands
from discord import app_commands
from utils.config import Config
from typing import Optional, Dict, Any, Union
import asyncio
import json
import aiofiles
from pathlib import Path
from datetime import datetime, timedelta
import logging
import random

logger = logging.getLogger(__name__)



class ModMail(commands.Cog):
    # Session format per user_id (best-effort; older persisted schemas may exist):
    # { 'thread_id': int, 'last_activity': ISO8601 timestamp, 'state': 'open'|'closed'|'resolved' }
    modmail_sessions: Dict[int, Dict[str, Any]] = {}
    _session_locks: Dict[int, asyncio.Lock] = {}
    SESSIONS_FILE = Path("data/modmail_sessions.json")

    def __init__(self, bot: commands.Bot, config: Config):
        self.bot = bot
        self.config = config
        self.modmail_channel_id: Optional[int] = getattr(config, 'modmail_channel_id', None)
        self._dm_semaphore: asyncio.Semaphore = asyncio.Semaphore(10) # Simultaneous DMs
        self._dm_channel_cache: Dict[int, discord.DMChannel] = {}
        self._webhook: Optional[discord.Webhook] = None
        
        # Per-user lock to ensure logical consistency
        self._user_locks: Dict[int, asyncio.Lock] = {}
        
        # Anti-Spam: 1 message every 2 seconds per user bucket
        self.spam_control = commands.CooldownMapping.from_cooldown(1, 2.0, commands.BucketType.user)

    async def cog_load(self):
        try:
            await self._load_sessions_from_file()
        except Exception:
            logger.exception("modmail: failed to load persisted sessions")

    def cog_unload(self):
        pass

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

    async def _load_sessions_from_file(self):
        if not self.SESSIONS_FILE.exists():
            return
        try:
            async with aiofiles.open(self.SESSIONS_FILE, "r", encoding="utf-8") as fh:
                content = await fh.read()
                data = json.loads(content)
            for k, v in data.items():
                try:
                    self.modmail_sessions[int(k)] = v
                except Exception:
                    logger.exception(f"modmail: failed to load session for key {k}")
        except Exception:
            logger.exception("modmail: error reading sessions file")

    async def _persist_sessions_to_file(self):
        try:
            self.SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            dumpable = {str(k): v for k, v in self.modmail_sessions.items()}
            async with aiofiles.open(self.SESSIONS_FILE, "w", encoding="utf-8") as fh:
                await fh.write(json.dumps(dumpable))
        except Exception:
            logger.exception("modmail: failed to persist sessions to file")

    def _is_session_expired(self, session: Dict[str, Any]) -> bool:
        reset_seconds = int(getattr(self.config, 'modmail_reset_seconds', 0) or 0)
        if reset_seconds <= 0:
            return False

        last_activity = session.get('last_activity')
        if not last_activity:
            return False

        try:
            last_dt = datetime.fromisoformat(str(last_activity))
        except Exception:
            return False

        return (datetime.utcnow() - last_dt) > timedelta(seconds=reset_seconds)

    def _is_session_closed(self, session: Dict[str, Any]) -> bool:
        state = str(session.get('state') or '').lower()
        return state in {'closed', 'resolved'}

    def _get_thread_from_session(
        self,
        session: Dict[str, Any],
        main_channel: discord.TextChannel,
    ) -> Optional[discord.Thread]:
        thread_id = session.get('thread_id')
        if not thread_id:
            return None
        try:
            thread = main_channel.get_thread(int(thread_id))
        except Exception:
            return None
        if not thread:
            return None
        if getattr(thread, 'archived', False) or getattr(thread, 'locked', False):
            return None
        return thread

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
        # Spam Control
        bucket = self.spam_control.get_bucket(message)
        retry_after = bucket.update_rate_limit() if bucket else None

        if retry_after:
            # Optionally log or just return
            return

        try:
            user_id = message.author.id
            if user_id not in self._user_locks:
                self._user_locks[user_id] = asyncio.Lock()

            async with self._user_locks[user_id]:
                session = self.modmail_sessions.get(user_id)
                
                if not self.modmail_channel_id:
                     await message.channel.send("ModMail system is currently disabled (Channel not set).")
                     return
                     
                main_channel = self.bot.get_channel(self.modmail_channel_id)
                if not main_channel or not isinstance(main_channel, discord.TextChannel):
                     await message.channel.send("ModMail system is unavailable (Invalid channel configuration).")
                     return

                webhook = await self._get_or_create_webhook(main_channel)

                thread: Optional[discord.Thread] = None
                session_active = False
                if session and isinstance(session, dict):
                    if not self._is_session_closed(session) and not self._is_session_expired(session):
                        thread = self._get_thread_from_session(session, main_channel)
                        session_active = thread is not None

                if not session_active:
                    # Create new session (first-time or after closure/expiry)
                    try:
                        # Log to main channel first
                        log_embed = discord.Embed(
                            title="📨 New ModMail Created",
                            description=f"**User:** {message.author.mention} (`{message.author.id}`)",
                            color=discord.Color.gold(),
                            timestamp=datetime.utcnow()
                        )
                        log_embed.set_thumbnail(url=message.author.display_avatar.url)
                        starter_msg = await main_channel.send(content="@here", embed=log_embed)

                        # Create public thread from the log message
                        thread = await starter_msg.create_thread(name=f"ModMail - {message.author.name} ({user_id})")
                    except Exception as e:
                        logger.error(f"Failed to create modmail session: {e}")
                        await message.channel.send("An error occurred while starting the modmail session.")
                        return

                    assert thread is not None

                    # Notify user
                    await self._send_dm_safe(
                        message.author,
                        embed=discord.Embed(
                            title="ModMail Started",
                            description=(
                                "✅ Your message has been received and a new modmail session has been opened.\n"
                                "Messages you send here will be forwarded to the moderators."
                            ),
                            color=discord.Color.default(),
                        ),
                    )

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
                        if thread is not None:
                            await thread.send(f"Failed to relay message from user: {e}")
                        raise e
                    
                    self.modmail_sessions[user_id] = {
                        'thread_id': thread.id,
                        'last_activity': datetime.utcnow().isoformat(),
                        'state': 'open'
                    }
                else:
                    # Continue session
                    # `thread` is guaranteed by session_active
                    assert thread is not None
                    assert isinstance(session, dict)

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
                         if thread is not None:
                             await thread.send(f"Failed to relay message from user: {e}")
                         raise e
                    session['last_activity'] = datetime.utcnow().isoformat()
                    session.setdefault('state', 'open')

                await self._persist_sessions_to_file()
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
             await self._persist_sessions_to_file()
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
        await self._persist_sessions_to_file()
        
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
        assert channel is not None
        self.modmail_channel_id = channel.id
        await interaction.response.send_message(f"Modmail channel set to {channel.mention}.", ephemeral=True)

async def setup(bot):
    config = getattr(bot, 'config', None)
    if config is None:
        raise RuntimeError("Bot config is missing. Cannot load ModMail cog.")
    await bot.add_cog(ModMail(bot, config))
