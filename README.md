# ModMail Bot

A simple and efficient ModMail bot for Discord, built with `discord.py`. This bot allows users to contact server moderators via DM, creating a thread in a designated channel for moderators to reply.

## Features

- **DM to ModMail**: Users can DM the bot to open a ticket.
- **Moderator Replies**: Moderators can reply directly from the modmail channel.
- **Slash Commands**: Includes slash commands for managing modmail.
- **Session Management**: Handles user sessions with timeouts and locking.
- **Logging**: Logs events to a file and console.

## Setup

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/TheCodeVerseHub/ModMail-Bot.git
    cd ModMail-Bot
    ```

2.  **Set up a virtual environment (optional but recommended):**
    ```bash
    python -m venv .venv
    # On Windows
    .venv\Scripts\activate
    # On Linux/macOS
    source .venv/bin/activate
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configuration:**
    Create a `.env` file in the root directory with the following content:
    ```dotenv
    DISCORD_TOKEN=your_discord_bot_token
    GUILD_ID=your_guild_id
    MODMAIL_CHANNEL_ID=your_modmail_channel_id
    LOG_LEVEL=INFO
    MODMAIL_RESET_SECONDS=600
    ```

5.  **Run the bot:**
    ```bash
    python bot.py
    ```

## Usage

- **Users**: DM the bot to start a conversation with moderators.
- **Moderators**:
    - Use `!reply_modmail <user_id> <message>` (prefix command) or `/reply_modmail` (slash command) to reply to a user.
    - Use `!set_modmail_channel` or `/set_modmail_channel` to change the log channel.

## License

This project is open source.
