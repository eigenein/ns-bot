#!/usr/bin/env python3
# coding: utf-8

# Yes, I’m aware that splitting into smaller modules is good.
# I didn’t do that intentionally this time.
# Sorry.

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
import uuid

import aiohttp
import aioredis
import click


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
            # TODO: graceful stop.


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
        "\n"
        "To get started please send me some URLs of either RSS or Atom feeds you’d like to read."
    ).format(emoji=Emoji)

    SUBSCRIBED = (
        "Ok, the following URLs were found and added to the queue:\n"
        "\n"
        "{urls}\n"
        "\n"
        "Hang on while we’re reading them. This can take several minutes."
    )

    ERROR = (
        "I’m experiencing some problems, sorry. {emoji.PENSIVE_FACE}\n"
        "\n"
        "Please try again later."
    ).format(emoji=Emoji)


# Redis wrapper.
# ----------------------------------------------------------------------------------------------------------------------

class Database:
    @staticmethod
    async def create():
        return Database(await aioredis.create_redis(("localhost", 6379)))

    def __init__(self, connection: aioredis.Redis):
        self._connection = connection

    async def upsert_feed(self, feed_id, url):
        logging.debug("Add feed %s: %s", feed_id, url)
        await self._connection.setnx("rss:%s:url" % feed_id, url)

    async def subscribe(self, user_id: int, feed_id: str):
        logging.debug("Subscribe user #%s to feed %s.", user_id, feed_id)
        await self._connection.sadd("rss:%s:subscriptions" % user_id, feed_id)

    def close(self):
        self._connection.close()


# Bot implementation.
# ----------------------------------------------------------------------------------------------------------------------

class Bot:
    _LIMIT = 10
    _TIMEOUT = 10

    _KEYBOARD_DEFAULT = json.dumps({
        "inline_keyboard": [[
            {"text": "Read", "callback_data": "/read"},
            {"text": "Unsubscribe", "callback_data": "/unsubscribe"},
        ]],
    })

    def __init__(self, telegram_token: str, botan_token: str):
        self._telegram = Telegram(telegram_token)
        self._botan = Botan(botan_token)
        self._session = aiohttp.ClientSession()
        self._db = None  # type: Database
        self._offset = 0
        self._is_stopped = False

    async def run(self):
        self._db = await Database.create()
        while not self._is_stopped:
            try:
                await self._loop()
            except Exception as ex:
                logging.error("Unhandled error.", exc_info=ex)
                await self._botan.track(None, "Error", message=str(ex))

    def stop(self):
        self._is_stopped = True

    def close(self):
        self._db.close()
        self._session.close()
        self._botan.close()
        self._telegram.close()

    async def _loop(self):
        """
        Executes one message loop iteration. Gets Telegram updates and handles them.
        """
        updates = await self._telegram.get_updates(self._offset, self._LIMIT, self._TIMEOUT)
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
        if text.startswith("/"):
            command, *arguments = text.split()
            if command == "/start":
                await self._handle_start(sender)
                await self._botan.track(sender["id"], "Start")
        else:
            await self._handle_subscribe(sender, text)
            await self._botan.track(sender["id"], "Subscribe")

    async def _handle_start(self, sender: dict):
        """
        Handles /start command.
        """
        await self._telegram.send_message(sender["id"], Responses.START.format(sender=sender), parse_mode=ParseMode.markdown)

    async def _handle_subscribe(self, sender: dict, text: str):
        """
        Handles subscribing to the feeds.
        """
        # TODO: limit count and length of URLs per user.
        added_urls = []
        for url in set(text.split()):
            if not url.startswith("http://") and not url.startswith("https://"):
                continue
            feed_id = uuid.uuid5(uuid.NAMESPACE_URL, url).hex
            await asyncio.gather(
                self._db.upsert_feed(feed_id, url),
                self._db.subscribe(sender["id"], feed_id),
            )
            added_urls.append(url)
        await self._telegram.send_message(
            sender["id"],
            Responses.SUBSCRIBED.format(urls="\n".join(added_urls)),
            reply_markup=self._KEYBOARD_DEFAULT,
            disable_web_page_preview=True,
        )


# Telegram API.
# ----------------------------------------------------------------------------------------------------------------------

class ParseMode(enum.Enum):
    """
    Telegram message parse mode.
    """
    default = None
    markdown = "Markdown"
    html = "HTML"


class ChatAction(enum.Enum):
    """
    https://core.telegram.org/bots/api#sendchataction
    """
    typing = "typing"
    upload_photo = "upload_photo"
    record_video = "record_video"
    upload_video = "upload_video"
    record_audio = "record_audio"
    upload_audio = "upload_audio"
    upload_document = "upload_document"
    find_location = "find_location"


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

    async def send_chat_action(self, chat_id: typing.Union[int, str], action: ChatAction):
        """
        https://core.telegram.org/bots/api#sendchataction
        """
        return await self.post("sendChatAction", chat_id=chat_id, action=action.value)

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
