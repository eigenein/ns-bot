#!/usr/bin/env python3
# coding: utf-8

"""
What can this bot do?

This bot is an RSS reader that allows you to read the most interesting articles from your feeds first.
"""

import asyncio
import contextlib
import enum
import json
import logging
import typing

import aiohttp
import aioredis
import click


# Configuration.
# ----------------------------------------------------------------------------------------------------------------------

REDIS_ADDRESS = ("localhost", 6379)
TELEGRAM_LIMIT = 10
TELEGRAM_TIMEOUT = 10


# CLI Commands.
# ----------------------------------------------------------------------------------------------------------------------

@click.group()
@click.option("-l", "--log-file", type=click.File("at", encoding="utf-8"))
@click.option("-v", "--verbose", type=bool, is_flag=True)
def main(log_file: click.File, verbose: bool):
    logging.basicConfig(
        datefmt="%Y-%m-%d %H:%M:%S",
        format="%(asctime)s (%(module)s) [%(levelname)s] %(message)s",
        level=(logging.INFO if not verbose else logging.DEBUG),
        stream=(log_file or click.get_text_stream("stderr")),
    )


@main.command(name="bot")
@click.option("-t", "--telegram-token", help="Telegram bot token.", required=True, type=str)
@click.option("-b", "--botan-token", help="Botan.io token.", required=True, type=str)
def run_bot(telegram_token: str, botan_token: str):
    """
    Run Telegram bot.
    """
    logging.info("Starting bot.")
    with contextlib.closing(Bot(telegram_token, botan_token)) as bot:
        try:
            asyncio.ensure_future(bot.run())
            asyncio.get_event_loop().run_forever()
        finally:
            bot.stop()


# Emoji.
# ----------------------------------------------------------------------------------------------------------------------

class Emoji:
    BLACK_SUN_WITH_RAYS = chr(0x2600)
    PENSIVE_FACE = chr(0x1F614)


# Bot response phrases.
# ----------------------------------------------------------------------------------------------------------------------

class Responses:
    START = (
        "*Hi {{sender[first_name]}}!* {emoji.BLACK_SUN_WITH_RAYS}\n"
        "To get started please send me some URLs of RSS feeds you’d like to read."
        " You can split them by whitespaces."
    ).format(emoji=Emoji)

    INVALID_URL = "This doesn’t look like a valid URL:\n\n{url}"

    ERROR = (
        "I’m experiencing some problems, sorry. {emoji.PENSIVE_FACE}\n"
        "Please try again."
    ).format(emoji=Emoji)


# Bot implementation.
# ----------------------------------------------------------------------------------------------------------------------

class Bot:

    _is_stopped = False

    def __init__(self, telegram_token: str, botan_token: str):
        self._telegram = Telegram(telegram_token)
        self._botan = Botan(botan_token)
        self._db = None
        self._offset = 0

    async def run(self):
        self._db = await aioredis.create_redis(REDIS_ADDRESS)
        while not self._is_stopped:
            try:
                await self._loop()
            except Exception as ex:
                logging.error("Unhandled error.", exc_info=ex)
                # await self._botan.track(None, "Error", message=str(ex))

    def stop(self):
        self._is_stopped = True

    def close(self):
        self._telegram.close()
        self._botan.close()
        self._db.close()

    async def _loop(self):
        """
        Executes one message loop iteration. Gets Telegram updates and handles them.
        """
        updates = await self._telegram.get_updates(self._offset, TELEGRAM_LIMIT, TELEGRAM_TIMEOUT)
        for update in updates:
            logging.info("Got update #%s.", update["update_id"])
            self._offset = update["update_id"] + 1
            if "message" in update:
                sender, text = update["message"]["from"], update["message"]["text"]
            elif "callback_query" in update:
                sender, text = update["callback_query"]["from"], update["callback_query"]["data"]
            else:
                continue
            try:
                await self._handle_message(sender, text)
            except Exception:
                await self._telegram.send_message(sender["id"], Responses.ERROR)
                raise

    async def _handle_message(self, sender: dict, text: str):
        """
        Handle single message from a user.
        """
        if text == "/start":
            await self._handle_start(sender)
        else:
            # Try to parse the message as a list of feed URLs.
            await self._handle_urls(sender, text)

    async def _handle_start(self, sender: dict):
        """
        Handles /start command.
        """
        await self._telegram.send_message(sender["id"], Responses.START.format(sender=sender), parse_mode=ParseMode.markdown)
        await self._botan.track(sender["id"], "Start")

    async def _handle_urls(self, sender: dict, text: str):
        """
        Handles list of feed URLs.
        """
        urls = text.split()
        for url in urls:
            if not url.startswith("http://") and not url.startswith("https://"):
                await self._telegram.send_message(sender["id"], Responses.INVALID_URL.format(url=url))
                await self._botan.track(sender["id"], "Invalid URL", url=url)
                return

        # TODO


# Telegram API.
# ----------------------------------------------------------------------------------------------------------------------

class ParseMode(enum.Enum):
    """
    Telegram message parse mode.
    """
    default = None
    markdown = "Markdown"
    html = "HTML"


class Telegram:

    _HEADERS = {"Content-Type": "application/json"}

    def __init__(self, token: str):
        self._url = "https://api.telegram.org/bot{}/{{}}".format(token)
        self._session = aiohttp.ClientSession()

    async def get_updates(self, offset: int, limit: int, timeout: int):
        return await self.post("getUpdates", offset=offset, limit=limit, timeout=timeout)

    async def send_message(
        self,
        chat_id: typing.Union[int, str],
        text: str,
        parse_mode=ParseMode.default,
        disable_web_page_preview=False,
        reply_to_message_id=None,
        reply_markup=None,
    ):
        params = {"chat_id": chat_id, "text": text}
        if parse_mode != ParseMode.default:
            params["parse_mode"] = parse_mode.value
        if disable_web_page_preview:
            params["disable_web_page_preview"] = disable_web_page_preview
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return await self.post("sendMessage", **params)

    async def post(self, method: str, **kwargs):
        logging.debug("%s(%s)", method, kwargs)
        async with self._session.post(self._url.format(method), data=json.dumps(kwargs), headers=self._HEADERS) as response:
            payload = await response.json()
            if payload["ok"]:
                logging.debug("%s → %s", method, payload)
                return payload["result"]
            else:
                logging.error("%s → %s", method, payload)
                raise TelegramException(payload["description"])

    def close(self):
        self._session.close()


class TelegramException(Exception):

    def __init__(self, message):
        super().__init__(message)


# Bot Analytics.
# ----------------------------------------------------------------------------------------------------------------------

class Botan:
    """
    Botan.io API.
    """

    _HEADERS = {"Content-Type": "application/json"}

    def __init__(self, token: str):
        self._token = token
        self._session = aiohttp.ClientSession()

    async def track(self, uid: typing.Optional[str], name: str, **kwargs):
        """
        Tracks event.
        """
        try:
            async with self._session.post(
                "https://api.botan.io/track",
                params={"token": self._token, "uid": uid, "name": name},
                data=json.dumps(kwargs),
                headers=self._HEADERS,
            ) as response:
                payload = await response.json()
                if payload["status"] == "failed":
                    logging.error("Failed to track event: %s", payload.get("info"))
        except Exception as ex:
            logging.error("Failed to track event.", exc_info=ex)

    def close(self):
        self._session.close()


# Entry point.
# ----------------------------------------------------------------------------------------------------------------------

if __name__ == "__main__":
    main()
