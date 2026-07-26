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
import io
from typing import TypeVar, Callable, Awaitable, cast

logger = logging.getLogger(__name__)

T = TypeVar('T')



class ModMail(commands.Cog):
    # Session format per user_id (best-effort; older persisted schemas may exist):
    # { 'thread_id': int, 'last_activity': ISO8601 timestamp, 'state': 'open'|'closed'|'resolved' }
    _session_locks: Dict[int, asyncio.Lock] = {}
    SESSIONS_FILE = Path("data/modmail_sessions.json")

    def __init__(self, bot: commands.Bot, config: Config):
        self.bot = bot
        self.config = config
        self.modmail_channel_id: Optional[int] = getattr(config, 'modmail_channel_id', None)
        self.modmail_sessions: Dict[int, Dict[str, Any]] = {}
        self._dm_semaphore: asyncio.Semaphore = asyncio.Semaphore(10) # Simultaneous DMs
        self._dm_channel_cache: Dict[int, discord.DMChannel] = {}
        self._webhook: Optional[discord.Webhook] = None
        
        # Per-user lock to ensure logical consistency
        self._user_locks: Dict[int, asyncio.Lock] = {}
        # Reverse lookup so thread events can be resolved without scanning every session.
        self._thread_to_user: Dict[int, int] = {}
        
        # Anti-Spam: 1 message every 2 seconds per user bucket
        self.spam_control = commands.CooldownMapping.from_cooldown(1, 2.0, commands.BucketType.user)

        # DM confirmation state:
        # When a user DMs without an active session, we ask them to confirm
        # before creating a new modmail thread.
        # { user_id: { 'created_at': iso, 'prompt_message_id': int, 'messages': [ {content, attachments, created_at} ] } }
        self._pending_confirmations: Dict[int, Dict[str, Any]] = {}

        # Limits for queued messages while awaiting confirmation.
        self._max_pending_messages: int = 5
        # Store up to 8 MiB of attachment bytes per user while pending.
        self._max_pending_attachment_bytes: int = 8 * 1024 * 1024

    def _confirm_timeout_seconds(self) -> int:
        try:
            value = int(getattr(self.config, 'modmail_confirm_timeout_seconds', 300) or 300)
        except Exception:
            value = 300
        return max(30, value)

    def _pending_is_expired(self, pending: Dict[str, Any]) -> bool:
        created_at = pending.get('created_at')
        if not created_at:
            return True
        try:
            created_dt = datetime.fromisoformat(str(created_at))
        except Exception:
            return True
        return (datetime.utcnow() - created_dt) > timedelta(seconds=self._confirm_timeout_seconds())

    async def _queue_pending_message(self, user_id: int, message: discord.Message):
        pending = self._pending_confirmations.setdefault(
            user_id,
            {'created_at': datetime.utcnow().isoformat(), 'messages': []},
        )

        messages = pending.setdefault('messages', [])
        if not isinstance(messages, list):
            messages = []
            pending['messages'] = messages

        # Enforce max queued messages by dropping oldest.
        while len(messages) >= self._max_pending_messages:
            messages.pop(0)

        attachment_payloads = []
        # Only read attachments if the total size stays within budget.
        existing_bytes = 0
        for queued in messages:
            for a in (queued.get('attachments') or []):
                existing_bytes += int(a.get('size', 0) or 0)

        to_read = []
        total_new_bytes = 0
        for att in message.attachments:
            size = int(getattr(att, 'size', 0) or 0)
            if size <= 0:
                continue
            total_new_bytes += size
            to_read.append(att)

        if (existing_bytes + total_new_bytes) <= self._max_pending_attachment_bytes:
            for att in to_read:
                try:
                    data = await att.read()
                    attachment_payloads.append({
                        'filename': att.filename,
                        'data': data,
                        'size': len(data),
                    })
                except Exception:
                    logger.exception("modmail: failed to read attachment while pending confirmation")
        else:
            # Skip storing large attachments; user can resend after confirming.
            if to_read:
                attachment_payloads.append({
                    'filename': None,
                    'data': None,
                    'size': 0,
                    'skipped': True,
                })

        messages.append({
            'content': message.content or '',
            'attachments': attachment_payloads,
            'source_message_id': message.id,
            'source_channel_id': message.channel.id,
            'created_at': datetime.utcnow().isoformat(),
        })

    async def _react_dm_message_success(self, channel_id: int, message_id: int):
        try:
            ch = self.bot.get_channel(int(channel_id))
            if ch is None:
                ch = await self.bot.fetch_channel(int(channel_id))
            if not isinstance(ch, (discord.DMChannel, discord.PartialMessageable)):
                return
            msg = await ch.fetch_message(int(message_id))
            await msg.add_reaction("✅")
        except Exception:
            # Best-effort only.
            return

    def _pending_to_discord_files(self, queued_attachments: list[dict]) -> tuple[list[discord.File], bool]:
        files: list[discord.File] = []
        had_skipped = False
        for payload in queued_attachments or []:
            if payload.get('skipped'):
                had_skipped = True
                continue
            data = payload.get('data')
            filename = payload.get('filename')
            if not data or not filename:
                continue
            bio = io.BytesIO(data)
            files.append(discord.File(bio, filename=filename))
        return files, had_skipped

    def _register_session(self, user_id: int, session: Dict[str, Any]) -> None:
        self.modmail_sessions[user_id] = session
        thread_id = session.get('thread_id')
        if thread_id is None:
            return
        try:
            self._thread_to_user[int(thread_id)] = int(user_id)
        except Exception:
            logger.exception("modmail: failed to register thread mapping")

    def _clear_session(self, user_id: int) -> None:
        session = self.modmail_sessions.pop(user_id, None)
        if not session:
            return
        thread_id = session.get('thread_id')
        if thread_id is None:
            return
        try:
            self._thread_to_user.pop(int(thread_id), None)
        except Exception:
            logger.exception("modmail: failed to clear thread mapping")

    def _rebuild_thread_index(self) -> None:
        self._thread_to_user.clear()
        for user_id, session in self.modmail_sessions.items():
            thread_id = session.get('thread_id')
            if thread_id is None:
                continue
            try:
                self._thread_to_user[int(thread_id)] = int(user_id)
            except Exception:
                continue

    async def _send_modmail_confirmation_prompt(self, user: Union[discord.User, discord.Member]) -> discord.Message:
        prompt = await self._send_dm_safe(
            user,
            embed=discord.Embed(
                title="Start ModMail?",
                description=(
                    "I can open a ModMail thread so moderators can see your messages.\n\n"
                    "React with ✅ to start, or ❌ to cancel."
                ),
                color=discord.Color.blurple(),
            ),
        )
        try:
            await prompt.add_reaction("✅")
            await prompt.add_reaction("❌")
        except Exception:
            # Best-effort; if reactions fail, user can DM again.
            logger.exception("modmail: failed to add reactions to confirmation prompt")
        return prompt

    async def _cancel_pending_confirmation(self, user: Union[discord.User, discord.Member]):
        self._pending_confirmations.pop(user.id, None)
        await self._send_dm_safe(
            user,
            content="Okay — I won’t start a modmail thread. If you change your mind, DM me again.",
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # Reaction-based confirmation only applies in DMs
        if payload.user_id == getattr(self.bot.user, 'id', None):
            return

        if payload.guild_id is not None:
            return

        user_id = int(payload.user_id)
        pending = self._pending_confirmations.get(user_id)
        if not pending:
            return

        if self._pending_is_expired(pending):
            self._pending_confirmations.pop(user_id, None)
            return

        prompt_message_id = pending.get('prompt_message_id')
        if not prompt_message_id or int(prompt_message_id) != int(payload.message_id):
            return

        emoji = str(payload.emoji)
        if emoji not in {"✅", "❌"}:
            return

        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()

        async with self._user_locks[user_id]:
            # Re-check within lock
            pending = self._pending_confirmations.get(user_id)
            if not pending:
                return
            if self._pending_is_expired(pending):
                self._pending_confirmations.pop(user_id, None)
                return
            if int(pending.get('prompt_message_id') or 0) != int(payload.message_id):
                return

            if emoji == "❌":
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await self._cancel_pending_confirmation(user)
                return

            # : start session and flush queued messages
            if not self.modmail_channel_id:
                self._pending_confirmations.pop(user_id, None)
                return

            main_channel = self.bot.get_channel(self.modmail_channel_id)
            if not main_channel or not isinstance(main_channel, discord.TextChannel):
                self._pending_confirmations.pop(user_id, None)
                return

            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            webhook = await self._get_or_create_webhook(main_channel)

            try:
                await self._start_new_session_and_flush_pending(
                    user=user,
                    main_channel=main_channel,
                    webhook=webhook,
                    pending=pending,
                )
            except Exception:
                logger.exception("modmail: failed to create session after reaction confirm")
                # Keep pending so user can retry reacting.
                return

            self._pending_confirmations.pop(user_id, None)

    async def _start_new_session_and_flush_pending(
        self,
        user: Union[discord.User, discord.Member],
        main_channel: discord.TextChannel,
        webhook: discord.Webhook,
        pending: Dict[str, Any],
    ):
        user_id = user.id
        # Log to main channel first
        log_embed = discord.Embed(
            title="📨 New ModMail Created",
            description=f"**User:** {user.mention} (`{user_id}`)",
            color=discord.Color.gold(),
            timestamp=datetime.utcnow(),
        )
        log_embed.set_thumbnail(url=user.display_avatar.url)
        starter_msg = await main_channel.send(content="@here", embed=log_embed)
        thread = await starter_msg.create_thread(name=f"ModMail - {user.name} ({user_id})")

        await self._send_dm_safe(
            user,
            embed=discord.Embed(
                title="ModMail Started",
                description=(
                    "A modmail session is now open. Messages you send here will be forwarded to the moderators."
                ),
                color=discord.Color.from_str("#00ff00"),
            ),
        )

        had_skipped_any = False
        queued_messages = pending.get('messages') or []
        if not isinstance(queued_messages, list):
            queued_messages = []

        for qm in queued_messages:
            content = qm.get('content') or ''
            files, had_skipped = self._pending_to_discord_files(qm.get('attachments') or [])
            had_skipped_any = had_skipped_any or had_skipped
            try:
                if not content and not files:
                    continue
                send_kwargs: Dict[str, Any] = {
                    'username': user.name,
                    'avatar_url': user.display_avatar.url,
                    'thread': thread,
                }
                if content:
                    send_kwargs['content'] = content
                if files:
                    send_kwargs['files'] = files
                await webhook.send(**send_kwargs)

                # React to the original DM message once forwarded.
                src_mid = qm.get('source_message_id')
                src_cid = qm.get('source_channel_id')
                if src_mid and src_cid:
                    await self._react_dm_message_success(int(src_cid), int(src_mid))
            except Exception as e:
                await thread.send(f"Failed to relay queued message from user: {e}")
                raise

        if had_skipped_any:
            try:
                await self._send_dm_safe(
                    user,
                    content=(
                        "Some attachments were too large to hold while waiting for confirmation. "
                        "If needed, please resend them now that the session is open."
                    ),
                )
            except Exception:
                pass

        self._register_session(user_id, {
            'thread_id': thread.id,
            'last_activity': datetime.utcnow().isoformat(),
            'state': 'open',
        })
        await self._persist_sessions_to_file()


    async def cog_load(self):
        try:
            await self._load_sessions_from_file()
        except Exception:
            logger.exception("modmail: failed to load persisted sessions")

    def cog_unload(self):
        pass

    async def _send_with_retry(
        self,
        send_func: Callable[..., Awaitable[T]],
        *args,
        max_retries: int = 3,
        **kwargs,
    ) -> T:
        last_exc: Optional[BaseException] = None
        for attempt in range(max_retries):
            try:
                return await send_func(*args, **kwargs)
            except discord.errors.HTTPException as e:
                last_exc = e
                if e.status == 429 and attempt < max_retries - 1:
                    retry_after = getattr(e, 'retry_after', None) or (2 ** attempt) + random.uniform(0, 1)
                    await asyncio.sleep(retry_after)
                else:
                    raise
            except Exception as e:
                last_exc = e
                raise

        # Should be unreachable, but keeps type-checkers happy.
        raise RuntimeError("send_with_retry exhausted retries") from last_exc
    
    async def _send_dm_safe(self, user: Union[discord.User, discord.Member], **kwargs) -> discord.Message:
        async with self._dm_semaphore:
            dm_channel = self._dm_channel_cache.get(user.id)
            if dm_channel is None:
                if isinstance(user, discord.Member):
                    actual_user = user._user
                else:
                    actual_user = user
                dm_channel = await actual_user.create_dm()
                self._dm_channel_cache[user.id] = dm_channel
            
            # discord.py DMChannel.send returns discord.Message
            result = await self._send_with_retry(dm_channel.send, **kwargs)
            return cast(discord.Message, result)

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
            self.modmail_sessions.clear()
            async with aiofiles.open(self.SESSIONS_FILE, "r", encoding="utf-8") as fh:
                content = await fh.read()
                if not content.strip():
                    return
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    logger.warning("modmail: sessions file is not valid JSON; ignoring")
                    return
            for k, v in data.items():
                try:
                    self.modmail_sessions[int(k)] = v
                except Exception:
                    logger.exception(f"modmail: failed to load session for key {k}")
            self._rebuild_thread_index()
        except Exception:
            logger.exception("modmail: error reading sessions file")

    async def _persist_sessions_to_file(self):
        try:
            self.SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            dumpable = {str(k): v for k, v in self.modmail_sessions.items()}
            tmp_path = self.SESSIONS_FILE.with_suffix(self.SESSIONS_FILE.suffix + ".tmp")
            async with aiofiles.open(tmp_path, "w", encoding="utf-8") as fh:
                await fh.write(json.dumps(dumpable))
            tmp_path.replace(self.SESSIONS_FILE)
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

    async def _get_thread_from_session(
        self,
        session: Dict[str, Any],
        main_channel: discord.TextChannel,
    ) -> Optional[discord.Thread]:
        thread_id = session.get('thread_id')
        if not thread_id:
            return None
        try:
            thread_id_int = int(thread_id)
        except Exception:
            return None

        # First check the parent channel cache, then the global channel cache,
        # and finally fetch by ID so a valid open thread is not lost to a cache miss.
        thread = main_channel.get_thread(thread_id_int)
        if not thread:
            cached = self.bot.get_channel(thread_id_int)
            if isinstance(cached, discord.Thread):
                thread = cached
        if not thread:
            try:
                fetched = await self.bot.fetch_channel(thread_id_int)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
            if isinstance(fetched, discord.Thread):
                thread = fetched
        if not thread:
            return None
        if getattr(thread, 'parent_id', None) not in {None, main_channel.id}:
            return None
        if getattr(thread, 'archived', False) or getattr(thread, 'locked', False):
            return None
        return thread

    def _get_session_user_id_for_thread(self, thread_id: int) -> Optional[int]:
        user_id = self._thread_to_user.get(int(thread_id))
        if user_id is not None:
            return user_id

        for uid, data in self.modmail_sessions.items():
            try:
                if int(data.get('thread_id') or 0) == int(thread_id):
                    self._thread_to_user[int(thread_id)] = int(uid)
                    return int(uid)
            except Exception:
                continue
        return None

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
                        thread = await self._get_thread_from_session(session, main_channel)
                        session_active = thread is not None

                if not session_active:
                    # Ask for confirmation before creating a new session.
                    pending = self._pending_confirmations.get(user_id)
                    if pending and self._pending_is_expired(pending):
                        self._pending_confirmations.pop(user_id, None)
                        pending = None

                    if pending is None:
                        # First DM (or after close/expiry): queue this message and ask.
                        await self._queue_pending_message(user_id, message)
                        prompt = await self._send_modmail_confirmation_prompt(message.author)
                        self._pending_confirmations[user_id]['prompt_message_id'] = prompt.id
                        return

                    # Pending exists: queue the message and remind the user to react on the prompt.
                    await self._queue_pending_message(user_id, message)
                    await self._send_dm_safe(
                        message.author,
                        content="React on the prompt message with ✅ to start or ❌ to cancel.",
                    )
                    return
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
                        try:
                            await message.add_reaction("✅")
                        except Exception:
                            pass
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
                await message.channel.send(f"An internal error occurred: {str(e)}")
            except:
                pass

    async def handle_thread_reply(self, message: discord.Message):
        # Find user_id from thread_id
        session_user_id = self._get_session_user_id_for_thread(message.channel.id)
        
        if session_user_id is None:
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
            await message.channel.send("User cannot be found (might have left shared servers).")
            return

        try:
             files = [await f.to_file() for f in message.attachments]
             embed = discord.Embed(
                 title="A Moderator has Replied",
                 description=message.content, 
                 color=discord.Color.purple()
             )
             # embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
             await self._send_dm_safe(user, embed=embed, files=files)
             
             self.modmail_sessions[session_user_id]['last_activity'] = datetime.utcnow().isoformat()
             await self._persist_sessions_to_file()
             # Optional: React to confirm sent
             await message.add_reaction("✅")
        except Exception as e:
            await message.channel.send(f"Failed to send to user: {e}")

    @commands.command(name="close", aliases=["mclose"])
    async def close_session(self, ctx):
        if not isinstance(ctx.channel, discord.Thread):
             return

        session_user_id = None
        session_user_id = self._get_session_user_id_for_thread(ctx.channel.id)

        if session_user_id is None:
            await ctx.send("This is not a active modmail thread.")
            return

        # Close session
        self._clear_session(session_user_id)
        await self._persist_sessions_to_file()
        
        user = self.bot.get_user(session_user_id)
        if user:
            try:
                await user.send(embed=discord.Embed(
                    title="Session Closed", 
                    description="This modmail session has been closed by a moderator.",
                    color=discord.Color.from_str("#ff0000")
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
