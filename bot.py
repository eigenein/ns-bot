#!/usr/bin/env python3
# coding: utf-8

"""
What can this bot do?

The bot can plan a journey with NS trains via their recommendations service.
"""

import asyncio
import collections
import datetime
import difflib
import enum
import itertools
import json
import logging
import math
import pickle
import time
import typing

from contextlib import ExitStack, closing
from xml.etree import ElementTree

import aiohttp
import aioredis
import click


# CLI Commands.
# ----------------------------------------------------------------------------------------------------------------------

@click.command()
@click.option("--telegram-token", help="Telegram Bot API token.", required=True, envvar="NS_BOT_TELEGRAM_TOKEN")
@click.option("--ns-login", help="NS API login.", required=True, envvar="NS_API_LOGIN")
@click.option("--ns-password", help="NS API password.", required=True, envvar="NS_API_PASSWORD")
@click.option("-l", "--log-file", type=click.File("at", encoding="utf-8"))
@click.option("-v", "--verbose", type=bool, is_flag=True)
def main(
    telegram_token: str,
    ns_login: str,
    ns_password: str,
    log_file: click.File,
    verbose: bool,
):
    logging.basicConfig(
        datefmt="%Y-%m-%d %H:%M:%S",
        format="%(asctime)s [%(levelname).1s] %(message)s",
        level=(logging.INFO if not verbose else logging.DEBUG),
        stream=(log_file or click.get_text_stream("stderr")),
    )

    logging.info("Starting bot…")
    with ExitStack() as exit_stack:
        telegram = exit_stack.enter_context(closing(Telegram(telegram_token)))
        ns = exit_stack.enter_context(closing(Ns(ns_login, ns_password)))
        bot = exit_stack.enter_context(closing(Bot(telegram, ns)))
        try:
            asyncio.ensure_future(bot.run())
            asyncio.get_event_loop().run_forever()
        finally:
            bot.stop()


# Bot response phrases.
# ----------------------------------------------------------------------------------------------------------------------

class Responses:
    START = "\n".join((
        "*Hi {sender[first_name]}!* \N{BLACK SUN WITH RAYS}",
        "",
        "You can add your favorite station simply by sending me its name. Small typos are okay.",
        "",
        "You can also send me your location to see departures from the nearest station.",
    ))

    ERROR = "\n".join((
        "I’m experiencing some technical problems, sorry. \N{PENSIVE FACE}",
        "",
        "Maybe try again later.",
    ))

    DEPARTURE = "\n".join((
        "*{departure.destination}*",
        "\N{ALARM CLOCK} *{departure.time:%-H:%M}* _{departure.delay_text}_ \N{STATION} *{departure.platform}*",
        "\N{TRAIN} {departure.train_type} _{departure.route_text}_",
    ))

    JOURNEY = "\n".join((
        "\N{ALARM CLOCK} *{journey.actual_departure_time:%-H:%M}* → _{duration}_ → *{journey.actual_arrival_time:%-H:%M}*",
        "",
        "{components_text}",
    ))

    COMPONENT = "\n".join((
        "\N{TRAIN} _{component.transport_type}_",
        "*{first_stop.name}* → *{last_stop.name}*",
        "\N{ALARM CLOCK} *{first_stop.time:%-H:%M}* \N{STATION} *{first_stop.platform}*"
        " → \N{ALARM CLOCK} *{last_stop.time:%-H:%M}* \N{STATION} *{last_stop.platform}*",
    ))

    ADDED = (
        "*{}* station is added to your favorites! Now you can use it as either departure or destination."
        " Add as many stations as you would like to use."
    )

    DELETED = "*{}* station is deleted from your favorites. You can always add it back later."
    DEFAULT = "*Hi {sender[first_name]}!* \N{BLACK SUN WITH RAYS} Tap a departure station to plan a journey or send me a station name."
    NO_SEARCH_RESULTS = "I couldn’t find any station with similar name. Please check it and try again."
    SEARCH_RESULTS = "I’ve found the following stations. Tap a station to add it to favorites."
    SELECT_DESTINATION = "Ok, where would you like to go from *{station_name}*?"
    LOCATION_FOUND = "The nearest station is *{station_name}*. Where would you like to go from there?"


# Redis wrapper.
# ----------------------------------------------------------------------------------------------------------------------

class Database:
    T = typing.TypeVar("T")

    @staticmethod
    async def create():
        return Database(await aioredis.create_redis(("redis", 6379)))

    def __init__(self, connection: aioredis.Redis):
        self.connection = connection

    async def add_favorite_station(self, user_id: int, station_code: str):
        """
        Adds the station to the user's favorites.
        """
        await self.connection.sadd("ns:%s:favorites" % user_id, station_code)

    async def get_favorites_stations(self, user_id: int) -> typing.Iterable[str]:
        """
        Gets the codes of the user's favorite stations.
        """
        return await self.connection.smembers("ns:%s:favorites" % user_id, encoding="utf-8")

    async def delete_favorite_station(self, user_id, station_code: str):
        """
        Deletes the station from user's favorites.
        """
        await self.connection.srem("ns:%s:favorites" % user_id, station_code)

    async def is_cached(self, sub_key: str):
        """
        Gets whether the specified cache key exists.
        """
        return await self.connection.exists("ns:cache:%s" % sub_key)

    async def put_into_cache(self, sub_key: str, items: typing.Iterable[T], score: typing.Callable[[T], typing.Union[int, float]]):
        """
        Puts the items into a sorted set according to their scores.
        """
        key = "ns:cache:%s" % sub_key
        await self.connection.zadd(key, *itertools.chain(*((score(item), pickle.dumps(item)) for item in items)))
        await self.connection.expire(key, 60)

    async def get_previous_from_cache(self, sub_key: str, score: typing.Union[int, float]):
        items = await self.connection.zrevrangebyscore(
            "ns:cache:%s" % sub_key, max=score, offset=0, count=1, exclude="ZSET_EXCLUDE_MAX")
        return pickle.loads(items[0]) if items else None

    async def get_next_from_cache(self, sub_key: str, score: typing.Union[int, float]):
        items = await self.connection.zrangebyscore(
            "ns:cache:%s" % sub_key, min=score, offset=0, count=1, exclude="ZSET_EXCLUDE_MIN")
        return pickle.loads(items[0]) if items else None

    def close(self):
        self.connection.close()


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

    HEADERS = {"Content-Type": "application/json"}

    def __init__(self, token: str):
        self._url = "https://api.telegram.org/bot{}/{{}}".format(token)
        self.session = aiohttp.ClientSession()

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

    async def edit_message_text(
        self,
        chat_id: typing.Union[None, int, str],
        message_id: typing.Optional[int],
        inline_message_id: typing.Optional[str],
        text: str,
        parse_mode=ParseMode.default,
        disable_web_page_preview=False,
        reply_markup=None,
    ):
        """
        https://core.telegram.org/bots/api#editmessagetext
        """
        params = {"text": text}
        if chat_id:
            params["chat_id"] = chat_id
        if message_id:
            params["message_id"] = message_id
        if inline_message_id:
            params["inline_message_id"] = inline_message_id
        if parse_mode != ParseMode.default:
            params["parse_mode"] = parse_mode.value
        if disable_web_page_preview:
            params["disable_web_page_preview"] = disable_web_page_preview
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return await self.post("editMessageText", **params)

    async def send_chat_action(self, chat_id: typing.Union[int, str], action: ChatAction):
        """
        https://core.telegram.org/bots/api#sendchataction
        """
        return await self.post("sendChatAction", chat_id=chat_id, action=action.value)

    async def answer_callback_query(self, callback_query_id: str, text=None, show_alert=False):
        """
        https://core.telegram.org/bots/api#answercallbackquery
        """
        params = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        if show_alert:
            params["show_alert"] = show_alert
        return await self.post("answerCallbackQuery", **params)

    async def send_location(
        self,
        chat_id: typing.Union[int, str],
        latitude: float,
        longitude: float,
        disable_notification=False,
        reply_to_message_id=None,
        reply_markup=None,
    ):
        """
        https://core.telegram.org/bots/api#sendlocation
        """
        params = {"chat_id": chat_id, "latitude": latitude, "longitude": longitude}
        if disable_notification:
            params["disable_notification"] = disable_notification
        if reply_to_message_id:
            params["reply_to_message_id"] = reply_to_message_id
        if reply_markup:
            params["reply_markup"] = reply_markup
        return await self.post("sendLocation", **params)

    async def post(self, method: str, **kwargs):
        logging.debug("%s(%s)", method, kwargs)
        async with self.session.post(self._url.format(method), data=json.dumps(kwargs), headers=self.HEADERS) as response:
            payload = await response.json()
            if payload["ok"]:
                logging.debug("%s → %s", method, payload)
                return payload["result"]
            elif payload["description"] == "Bad Request: message is not modified":
                logging.warning("%s → %s", method, payload)
                return
            else:
                logging.error("%s → %s", method, payload)
                raise TelegramException(payload["description"])

    def close(self):
        self.session.close()


class TelegramException(Exception):

    def __init__(self, message):
        super().__init__(message)


# NS API.
# ----------------------------------------------------------------------------------------------------------------------

Station = collections.namedtuple("Station", ["code", "long_name", "names", "latitude", "longitude"])
Departure = collections.namedtuple("Departure", [
    "train_id",
    "time",
    "delay",
    "delay_text",
    "destination",
    "train_type",
    "route_text",
    "platform",
    "is_platform_changed",
])
Journey = collections.namedtuple("Journey", [
    "transfer_count",
    "planned_duration",
    "actual_duration",
    "is_optimal",
    "actual_departure_time",
    "actual_arrival_time",
    "components",
])
JourneyComponent = collections.namedtuple("JourneyComponent", ["transport_type", "stops"])
Stop = collections.namedtuple("Stop", ["name", "time", "platform", "is_platform_changed", "delay_text"])


class Ns:
    def __init__(self, login: str, password: str):
        self.session = aiohttp.ClientSession(auth=aiohttp.BasicAuth(login=login, password=password))

    async def get_stations(self):
        """
        Gets station list.
        """
        stations = []
        async with self.session.get("http://webservices.ns.nl/ns-api-stations-v2") as response:
            root = ElementTree.fromstring(await response.text())
        for station in root:
            names = {element.text for element in station.find("Namen")}
            names.update(element.text for element in station.find("Synoniemen"))
            stations.append(Station(
                code=station.find("Code").text,
                long_name=station.find("Namen").find("Lang").text,
                names=names,
                latitude=float(station.find("Lat").text),
                longitude=float(station.find("Lon").text),
            ))
        return stations

    async def departures(self, station_code: str):
        logging.debug("Departures: %s.", station_code)
        async with self.session.get("http://webservices.ns.nl/ns-api-avt", params={"station": station_code}) as response:
            root = ElementTree.fromstring(await response.text())
        return [
            Departure(
                train_id=departure.find("RitNummer").text,
                time=self.strptime(departure.find("VertrekTijd").text),
                delay=self.element_text(departure.find("VertrekVertraging")),
                delay_text=self.element_text(departure.find("VertrekVertragingTekst")),
                destination=departure.find("EindBestemming").text,
                train_type=departure.find("TreinSoort").text,
                route_text=self.element_text(departure.find("RouteTekst")),
                platform=departure.find("VertrekSpoor").text,
                is_platform_changed=departure.find("VertrekSpoor").attrib["wijziging"] == "true",
            )
            for departure in root
        ]

    async def plan_journey(self, departure_code: str, destination_code: str) -> typing.List[Journey]:
        """
        http://www.ns.nl/en/travel-information/ns-api/documentation-travel-recommendations.html
        """
        async with self.session.get(
            "http://webservices.ns.nl/ns-api-treinplanner",
            params={"fromStation": departure_code, "toStation": destination_code},
        ) as response:
            root = ElementTree.fromstring(await response.text())
        return [
            Journey(
                transfer_count=int(journey.find("AantalOverstappen").text),
                planned_duration=journey.find("GeplandeReisTijd").text,
                actual_duration=self.element_text(journey.find("ActueleReisTijd")),
                is_optimal=journey.find("Optimaal").text == "true",
                actual_departure_time=self.strptime(journey.find("ActueleVertrekTijd").text),
                actual_arrival_time=self.strptime(journey.find("ActueleAankomstTijd").text),
                components=[
                    JourneyComponent(
                        transport_type=component.find("VervoerType").text,
                        stops=[
                            Stop(
                                name=stop.find("Naam").text,
                                time=self.strptime(stop.find("Tijd").text, allow_empty=True),
                                platform=self.element_text(stop.find("Spoor")),
                                is_platform_changed=self.element_attribute(stop.find("Spoor"), "wijziging", "") == "true",
                                delay_text=self.element_text(stop.find("VertrekVertraging")),
                            ) for stop in component.findall("ReisStop")
                        ]
                    ) for component in journey.findall("ReisDeel")
                ]
            )
            for journey in root
            if self.element_text(journey.find("Status")) != "NIET-MOGELIJK"
        ]

    @staticmethod
    def strptime(time_string: str, allow_empty=False):
        if not time_string:
            if allow_empty:
                return None
            else:
                raise ValueError("empty time string")
        return datetime.datetime.strptime(time_string, "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)

    @staticmethod
    def element_text(element) -> str:
        return element.text if element is not None else ""

    @staticmethod
    def element_attribute(element, name, default):
        return element.attrib[name] if element is not None else default

    def close(self):
        self.session.close()


# Station index.
# ----------------------------------------------------------------------------------------------------------------------

class StationIndex:
    def __init__(self, stations: typing.Iterable[Station]):
        self.code_station = {station.code: station for station in stations}
        self.name_station = {name.lower(): station for station in stations for name in station.names}
        self.names = list(self.name_station)

    def search(self, query: str) -> typing.List[Station]:
        """
        Searches for unique stations that match the query.
        """
        logging.debug("Query: %s", query)
        return [
            self.code_station[code]
            for code in collections.OrderedDict.fromkeys(
                self.name_station[name].code
                for name in difflib.get_close_matches(query.lower(), self.names)
            )
        ]


# Bot implementation.
# ----------------------------------------------------------------------------------------------------------------------

class Bot:
    TELEGRAM_LIMIT = 100
    TELEGRAM_TIMEOUT = 60

    BUTTON_BACK = {"text": "Back", "callback_data": "/back"}

    TRANSLATE_TABLE = {
        ord("а"): "a", ord("б"): "b", ord("в"): "v", ord("г"): "g", ord("д"): "d", ord("е"): "e", ord("ж"): "zh",
        ord("з"): "z", ord("и"): "i", ord("й"): "i", ord("к"): "k", ord("л"): "l", ord("м"): "m", ord("н"): "n",
        ord("о"): "o", ord("п"): "p", ord("р"): "r", ord("с"): "c", ord("т"): "t", ord("у"): "u", ord("ф"): "f",
        ord("х"): "h", ord("ц"): "c", ord("ч"): "ch", ord("ш"): "sch", ord("щ"): "shch", ord("ъ"): "", ord("ь"): "",
        ord("э"): "e", ord("ю"): "u", ord("я"): "ya",
    }

    def __init__(self, telegram: Telegram, ns: Ns):
        self.telegram = telegram
        self.ns = ns
        self.db = None  # type: Database
        self.stations = None  # type: StationIndex
        self.offset = 0
        self.is_stopped = False

    async def run(self):
        self.db = await Database.create()

        logging.info("Pre-populating station list…")
        stations = await self.ns.get_stations()
        logging.info("Retrieved %d stations.", len(stations))
        self.stations = StationIndex(stations)

        logging.info("Running.")
        while not self.is_stopped:
            try:
                await self.run_loop()
            except Exception as ex:
                logging.error("Unhandled error.", exc_info=ex)

    def stop(self):
        self.is_stopped = True

    def close(self):
        self.db.close()

    async def run_loop(self):
        """
        Executes one message loop iteration. Gets Telegram updates and handles them.
        """
        updates = await self.telegram.get_updates(self.offset, self.TELEGRAM_LIMIT, self.TELEGRAM_TIMEOUT)
        for update in updates:
            logging.info("Got update #%s.", update["update_id"])
            self.offset = update["update_id"] + 1
            # Merge message text and callback query data.
            if "message" in update:
                sender = update["message"]["from"]
                text = update["message"].get("text")
                location = update["message"].get("location")
                original_message = None
            elif "callback_query" in update:
                sender = update["callback_query"]["from"]
                text = update["callback_query"]["data"]
                location = None
                original_message = update["callback_query"].get("message")
            else:
                continue
            if not text and not location:
                continue
            try:
                future = self.handle_message(sender, original_message, text, location)
                if "callback_query" in update:
                    await asyncio.gather(future, self.telegram.answer_callback_query(update["callback_query"]["id"]))
                else:
                    await future
            except Exception:
                await self.telegram.send_message(
                    sender["id"],
                    Responses.ERROR,
                    reply_markup=(await self.get_default_keyboard(sender["id"])),
                )
                raise

    async def get_default_keyboard(self, user_id: int) -> str:
        """
        Gets keyboard to choose from user's favorite stations.
        """
        station_codes = await self.db.get_favorites_stations(user_id)
        buttons = [
            [{"text": self.get_station_name(station_code), "callback_data": "/from %s" % station_code}]
            for station_code in station_codes
        ]
        buttons.extend([[
            {"text": "Bot Feedback", "url": "https://telegram.me/eigenein"},
        ]])
        return json.dumps({"inline_keyboard": buttons})

    async def handle_message(self, sender: dict, original_message: typing.Optional[dict], text: str, location: dict):
        """
        Handle single message from a user.
        """
        if text and text.startswith("/"):
            command, *arguments = text.split()
            if command == "/start":
                await self.handle_start(sender)
            elif command == "/back":
                await self.handle_back(sender)
            elif command == "/add":
                if arguments:
                    await self.handle_add(sender["id"], original_message, arguments[0])
            elif command == "/delete":
                if arguments:
                    await self.handle_delete(sender["id"], original_message, arguments[0])
            elif command == "/from":
                if arguments:
                    await self.handle_from(sender["id"], original_message, arguments[0])
            elif command == "/go":
                if len(arguments) == 2:
                    departure_code, destination_code = arguments
                    await self.handle_go(sender["id"], None, departure_code, destination_code)
                elif len(arguments) == 4:
                    departure_code, destination_code, direction, timestamp = arguments
                    await self.handle_go(
                        sender["id"],
                        original_message,
                        departure_code,
                        destination_code,
                        direction == "<",
                        int(timestamp),
                    )
            elif command == "/departures":
                if len(arguments) == 1:
                    await self.handle_departures(sender["id"], original_message, arguments[0])
                elif len(arguments) == 3:
                    station_code, direction, timestamp = arguments
                    await self.handle_departures(sender["id"], original_message, station_code, direction == "<", int(timestamp))
        elif text:
            await self.handle_search_query(sender, text)
        elif location:
            await self.handle_location(sender["id"], location["latitude"], location["longitude"])

    async def handle_start(self, sender: dict):
        """
        Handles /start command.
        """
        await self.telegram.send_message(
            sender["id"],
            Responses.START.format(sender=sender),
            parse_mode=ParseMode.markdown,
            reply_markup=(await self.get_default_keyboard(sender["id"])),
        )

    async def handle_back(self, sender: dict):
        """
        Handles /back command.
        """
        await self.edit_message(
            sender["id"],
            None,
            Responses.DEFAULT.format(sender=sender),
            parse_mode=ParseMode.markdown,
            reply_markup=(await self.get_default_keyboard(sender["id"])),
        )

    async def handle_add(self, user_id: int, original_message: typing.Optional[dict], station_code: str):
        """
        Handles /add <station_code> command.
        """
        await self.db.add_favorite_station(user_id, station_code)
        reply_markup = await self.get_default_keyboard(user_id)
        await self.edit_message(
            user_id,
            original_message,
            Responses.ADDED.format(self.get_station_name(station_code)),
            parse_mode=ParseMode.markdown,
            reply_markup=reply_markup,
        )

    async def handle_delete(self, user_id: int, original_message: typing.Optional[dict], station_code: str):
        """
        Handles /delete <station_code> command.
        """
        await self.db.delete_favorite_station(user_id, station_code)
        reply_markup = await self.get_default_keyboard(user_id)
        await self.edit_message(
            user_id,
            original_message,
            Responses.DELETED.format(self.get_station_name(station_code)),
            parse_mode=ParseMode.markdown,
            reply_markup=reply_markup,
        )

    async def handle_from(self, user_id: int, original_message: typing.Optional[dict], departure_code: str):
        """
        Handles /from <departure_code> command.
        """
        buttons = await self.get_from_buttons(user_id, departure_code)
        buttons.append([
            {"text": "Delete station", "callback_data": "/delete %s" % departure_code},
            {"text": "Departures", "callback_data": "/departures %s" % departure_code},
            self.BUTTON_BACK,
        ])
        await self.edit_message(
            user_id,
            original_message,
            Responses.SELECT_DESTINATION.format(station_name=self.get_station_name(departure_code)),
            parse_mode=ParseMode.markdown,
            reply_markup=json.dumps({"inline_keyboard": buttons}),
        )

    async def handle_go(
        self,
        user_id: int,
        original_message: typing.Optional[dict],
        departure_code: str,
        destination_code: str,
        navigate_backwards=False,
        timestamp=None,
    ):
        """
        Handles /go <departure_code> <destination_code> [<direction>] [<timestamp>] command.
        """
        timestamp = timestamp or int(time.time())
        sub_key = "journeys:%s:%s" % (departure_code, destination_code)

        await self.ensure_cache(
            sub_key,
            lambda: self.ns.plan_journey(departure_code, destination_code),
            lambda _journey: int(_journey.actual_departure_time.timestamp()),
        )
        if navigate_backwards:
            journey = await self.db.get_previous_from_cache(sub_key, timestamp)
        else:
            journey = await self.db.get_next_from_cache(sub_key, timestamp)

        if journey is None:
            logging.debug("No journeys found.")
            return

        text = Responses.JOURNEY.format(
            journey=journey,
            duration=(journey.actual_duration or journey.planned_duration),
            components_text="\n\n".join(
                Responses.COMPONENT.format(
                    component=component,
                    first_stop=component.stops[0],
                    last_stop=component.stops[-1],
                )
                for component in journey.components
            ),
        )

        navigate_arguments = (departure_code, destination_code, int(journey.actual_departure_time.timestamp()))
        reply_markup = json.dumps({
            "inline_keyboard": [
                [
                    {"text": "« Previous", "callback_data": "/go %s %s < %s" % navigate_arguments},
                    {"text": "Now", "callback_data": "/go %s %s" % (departure_code, destination_code)},
                    {"text": "Next »", "callback_data": "/go %s %s > %s" % navigate_arguments},
                ],
                [self.BUTTON_BACK],
            ],
        })
        await self.edit_message(user_id, original_message, text, parse_mode=ParseMode.markdown, reply_markup=reply_markup),

    async def handle_departures(
        self,
        user_id: int,
        original_message: typing.Optional[dict],
        station_code: str,
        navigate_backwards=False,
        timestamp=None,
    ):
        """
        Handles /departure <station_code> [<direction>] [<timestamp>] command.
        """
        timestamp = timestamp or int(time.time())
        sub_key = "departures:%s" % station_code

        await self.ensure_cache(
            sub_key, lambda: self.ns.departures(station_code), lambda _departure: int(_departure.time.timestamp()))
        if navigate_backwards:
            departure = await self.db.get_previous_from_cache(sub_key, timestamp)
        else:
            departure = await self.db.get_next_from_cache(sub_key, timestamp)

        if departure is None:
            logging.debug("No departures found.")
            return

        text = Responses.DEPARTURE.format(departure=departure).rstrip()

        navigate_arguments = (station_code, int(departure.time.timestamp()))
        reply_markup = json.dumps({
            "inline_keyboard": [
                [
                    {"text": "« Previous", "callback_data": "/departures %s < %s" % navigate_arguments},
                    {"text": "Now", "callback_data": "/departures %s" % station_code},
                    {"text": "Next »", "callback_data": "/departures %s > %s" % navigate_arguments},
                ],
                [self.BUTTON_BACK],
            ],
        })
        await self.edit_message(user_id, original_message, text, parse_mode=ParseMode.markdown, reply_markup=reply_markup)

    async def handle_location(self, user_id: int, latitude: float, longitude: float):
        # Find the nearest station.
        departure_code, station = min(
            self.stations.code_station.items(),
            key=lambda item: estimate_distance(item[1].latitude, item[1].longitude, latitude, longitude),
        )
        _, buttons = await asyncio.gather(
            self.telegram.send_message(
                user_id,
                Responses.LOCATION_FOUND.format(station_name=station.long_name),
                parse_mode=ParseMode.markdown,
            ),
            self.get_from_buttons(user_id, departure_code),
        )
        buttons.extend([
            [
                {"text": "Add", "callback_data": "/add %s" % station.code},
                {"text": "Departures", "callback_data": "/departures %s" % station.code},
                self.BUTTON_BACK,
            ],
        ])
        await self.telegram.send_location(
            user_id,
            station.latitude,
            station.longitude,
            disable_notification=True,
            reply_markup={"inline_keyboard": buttons},
        )

    async def handle_search_query(self, sender: dict, text: str):
        """
        Handles station search.
        """
        logging.debug("Search: %s", text)
        stations = self.stations.search(text.strip().translate(self.TRANSLATE_TABLE))
        if stations:
            buttons = [
                [{"text": station.long_name, "callback_data": "/add %s" % station.code}]
                for station in stations
            ]
            buttons.append([self.BUTTON_BACK])
            future = self.telegram.send_message(
                sender["id"],
                Responses.SEARCH_RESULTS,
                reply_markup=json.dumps({"inline_keyboard": buttons}),
            )
        else:
            future = self.telegram.send_message(
                sender["id"],
                Responses.NO_SEARCH_RESULTS,
                reply_markup=(await self.get_default_keyboard(sender["id"])),
            )
        await future

    async def get_from_buttons(self, user_id: int, departure_code: str) -> typing.List[typing.List[dict]]:
        """
        Gets keyboard buttons that are displayed when the user is going from the specified station.
        """
        station_codes = await self.db.get_favorites_stations(user_id)
        return [
            [{
                "text": "Go to %s" % self.get_station_name(destination_code),
                "callback_data": "/go %s %s" % (departure_code, destination_code),
            }]
            for destination_code in station_codes
            if destination_code != departure_code
        ]

    async def ensure_cache(self, sub_key: str, get, score):
        """
        Ensures that the data under the specified key is cached.
        Requests the data and puts into the cache otherwise.
        """
        if not await self.db.is_cached(sub_key):
            logging.debug("Caching items for %s…", sub_key)
            await self.db.put_into_cache(sub_key, await get(), score)

    async def edit_message(
        self,
        chat_id: typing.Union[None, int, str],
        message: typing.Optional[dict],
        text: str,
        parse_mode=ParseMode.default,
        disable_web_page_preview=False,
        reply_markup=None,
    ):
        """
        Edit the existing text message if any or sends a new one otherwise.
        """
        if message and "text" in message:
            return await self.telegram.edit_message_text(
                chat_id, message["message_id"], None, text, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview, reply_markup=reply_markup)
        else:
            return await self.telegram.send_message(
                chat_id, text, parse_mode, disable_web_page_preview=disable_web_page_preview, reply_markup=reply_markup)

    def get_station_name(self, station_code: str):
        """
        Gets station name by the station code.
        """
        return self.stations.code_station[station_code].long_name


# Utilities.
# ----------------------------------------------------------------------------------------------------------------------

def estimate_distance(latitude1: float, longitude1: float, latitude2: float, longitude2: float) -> float:
    """
    Estimates distance between two points on the geosphere.
    http://stackoverflow.com/a/15742266/359730
    """
    dx = (longitude2 - longitude1) * math.cos(0.5 * (latitude2 + latitude1))
    dy = latitude2 - latitude1
    return 6371 * math.sqrt(dx * dx + dy * dy)


# Entry point.
# ----------------------------------------------------------------------------------------------------------------------

if __name__ == "__main__":
    main()
