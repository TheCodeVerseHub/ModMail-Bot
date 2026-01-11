import asyncio
import logging
import os
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv
from utils.config import Config

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO')),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('modmail_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class ModMailBot(commands.Bot):
    """ModMail bot class."""

    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.presences = True

        super().__init__(
            command_prefix='m!',
            intents=intents,
            help_command=None,
            owner_id=config.owner_id
        )

        self.config = config

    async def setup_hook(self) -> None:
        """Setup hook called before the bot starts."""
        # Load all cogs
        if os.path.exists('./cogs'):
            for filename in os.listdir('./cogs'):
                if filename.endswith('.py') and filename != '__init__.py':
                    extension_name = f'cogs.{filename[:-3]}'
                    try:
                        await self.load_extension(extension_name)
                        logger.info(f'Loaded {extension_name}')
                    except Exception as e:
                        logger.error(f'Failed to load {extension_name}: {e}')
        else:
             logger.warning("No cogs directory found.")

        # Sync slash commands
        try:
            if self.config.guild_id:
                guild = discord.Object(id=self.config.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info(f"✅ Synced {len(synced)} slash commands to guild {self.config.guild_id}")
            else:
                synced = await self.tree.sync()
                logger.info(f"✅ Synced {len(synced)} slash commands globally")
        except Exception as e:
            logger.error(f"❌ Failed to sync slash commands: {e}")

    async def on_ready(self):
        """Called when the bot is ready."""
        user = self.user
        if user is None:
            logger.info('Logged in but bot user is not available yet.')
        else:
            logger.info(f'Logged in as {user} (ID: {user.id})')

async def main():
    """Main function to run the bot."""
    config = Config()

    if not config.discord_token:
        logger.error("DISCORD_TOKEN not found in environment variables.")
        return

    bot = ModMailBot(config)

    try:
        await bot.start(config.discord_token)
    except KeyboardInterrupt:
        logger.info("Bot shutdown requested.")
    except Exception as e:
        logger.error(f"Bot encountered an error: {e}")
    finally:
        await bot.close()

if __name__ == '__main__':
    asyncio.run(main())
