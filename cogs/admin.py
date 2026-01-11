from discord.ext import commands
import logging
import os

logger = logging.getLogger(__name__)

class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="reload", hidden=True)
    @commands.is_owner()
    async def reload(self, ctx):
        """Reloads all cogs."""
        reload_count = 0
        error_count = 0
        
        # Use asyncio.sleep to allow the message to be sent before potentially heavy reload operations
        msg = await ctx.send("Reloading cogs...")

        if not os.path.exists('./cogs'):
             await msg.edit(content="❌ Cogs directory not found.")
             return

        for filename in os.listdir('./cogs'):
            if filename.endswith('.py') and filename != '__init__.py':
                extension_name = f'cogs.{filename[:-3]}'
                try:
                    # Check if loaded using bot.extensions keys
                    if extension_name in self.bot.extensions:
                        await self.bot.reload_extension(extension_name)
                    else:
                        await self.bot.load_extension(extension_name)
                    reload_count += 1
                    logger.info(f"Reloaded {extension_name}")
                except Exception as e:
                    error_count += 1
                    await ctx.send(f"⚠️ Failed to reload `{extension_name}`:\n```py\n{e}\n```")
                    logger.error(f"Failed to reload {extension_name}: {e}")
        
        await msg.edit(content=f"✅ Reloaded {reload_count} cogs with {error_count} errors.")

async def setup(bot):
    await bot.add_cog(Admin(bot))
