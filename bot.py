#!/usr/bin/env python3
# coding: utf-8

"""
What can this bot do?

The bot can plan a journey with NS trains via their recommendations service.

Configuration module should contain the following constants:
TELEGRAM_TOKEN = "…"  # Telegram Bot API token
BOTAN_TOKEN = "…"  # Botan.io token
NS_LOGIN = "…"  # NS API login
NS_PASSWORD = "…"  # NS API password
ADMIN_IDS = {…}  # admin chat IDs
"""

import asyncio
import collections
import datetime
import difflib
import enum
import json
import logging
import math
import pickle
import typing

from contextlib import ExitStack, closing
from xml.etree import ElementTree

import aiohttp
import aioredis
import click

import config


# CLI Commands.
# ----------------------------------------------------------------------------------------------------------------------

@click.command()
@click.option("-l", "--log-file", type=click.File("at", encoding="utf-8"))
@click.option("-v", "--verbose", type=bool, is_flag=True)
def main(log_file: click.File, verbose: bool):
    logging.basicConfig(
        datefmt="%Y-%m-%d %H:%M:%S",
        format="%(asctime)s (%(module)s) [%(levelname)s] %(message)s",
        level=(logging.INFO if not verbose else logging.DEBUG),
        stream=(log_file or click.get_text_stream("stderr")),
    )

    logging.info("Starting bot…")
    with ExitStack() as exit_stack:
        telegram = exit_stack.enter_context(closing(Telegram(config.TELEGRAM_TOKEN)))
        botan = exit_stack.enter_context(closing(Botan(config.BOTAN_TOKEN)))
        ns = exit_stack.enter_context(closing(Ns(config.NS_LOGIN, config.NS_PASSWORD)))
        bot = exit_stack.enter_context(closing(Bot(telegram, botan, ns)))
        try:
            asyncio.ensure_future(bot.run())
            asyncio.get_event_loop().run_forever()
        finally:
            bot.stop()


# Emoji.
# ----------------------------------------------------------------------------------------------------------------------

class Emoji:
    ALARM_CLOCK = chr(0x23F0)
    BLACK_SUN_WITH_RAYS = chr(0x2600)
    PENSIVE_FACE = chr(0x1F614)
    STATION = chr(0x1F689)
    TRAIN = chr(0x1F686)
    TWISTED_RIGHTWARDS_ARROWS = chr(0x1F500)
    UPWARDS_BLACK_ARROW = chr(0x2B06)
    WARNING_SIGN = chr(0x26A0)
    WHITE_QUESTION_MARK_ORNAMENT = chr(0x2754)


# Bot response phrases.
# ----------------------------------------------------------------------------------------------------------------------

class Responses:
    START = "\n".join((
        "*Hi {{sender[first_name]}}!* {emoji.BLACK_SUN_WITH_RAYS}",
        "",
        "You can add your favorite station simply by sending me its name. Small typos are okay.",
        "",
        "You can also send me your location to see departures from the nearest station.",
    )).format(emoji=Emoji)

    ERROR = "\n".join((
        "I’m experiencing some technical problems, sorry. {emoji.PENSIVE_FACE}",
        "",
        "Maybe try again later.",
    )).format(emoji=Emoji)

    DEPARTURE = "\n".join((
        "*{{departure.destination}}*",
        "{emoji.ALARM_CLOCK} *{{departure.time:%-H:%M}}* _{{departure.delay_text}}_ {emoji.STATION} *{{departure.platform}}{{platform_changed}}*",
        "{emoji.TRAIN} {{departure.train_type}} _{{departure.route_text}}_",
    )).format(emoji=Emoji)

    JOURNEY = "\n".join((
        "{emoji.ALARM_CLOCK} *{{journey.actual_departure_time:%-H:%M}}* → _{{duration}}_ → *{{journey.actual_arrival_time:%-H:%M}}*",
        "",
        "{{components_text}}",
    )).format(emoji=Emoji)

    COMPONENT = (
        "{{i}}. {emoji.TRAIN} _{{component.transport_type}}_ will depart at *{{first_stop.time:%-H:%M}}* from platform"
        " *{{first_stop.platform}}*{{first_stop_warning}} on *{{first_stop.name}}*"
        " and arrive at *{{last_stop.time:%-H:%M}}* to platform *{{last_stop.platform}}*{{last_stop_warning}}"
        " on *{{last_stop.name}}*."
    ).format(emoji=Emoji)

    DEFAULT = "*Hi {sender[first_name]}!* Tap a departure station to plan a journey."
    ADDED = "It’s added! Now you can use it as either departure or destination. Add as many stations as you would like to use."
    DELETED = "It’s deleted. You can always add it back later."
    SEARCH = "Just send me a station name. You can do that whenever you want."
    NO_SEARCH_RESULTS = "I couldn’t find any station with similar name. Please check it and try again."
    SEARCH_RESULTS = "I’ve found the following stations. Tap a station to add it to favorites."
    SELECT_DESTINATION = "Ok, where would you like to go?"
    LOCATION_FOUND = "The nearest station is *{station_name}*. Where would you like to go from there?"
    DEPARTURES = "Departures from *{station_name}*:\n\n{departures_text}"
    NO_DEPARTURES = "No departures found from *{station_name}*."
    JOURNEYS = "Journeys from *{departure_name}* to *{destination_name}*:\n\n{journeys_text}"
    NO_JOURNEYS = "No journeys found from *{departure_name}* to *{destination_name}*."


# Redis wrapper.
# ----------------------------------------------------------------------------------------------------------------------

class Database:
    @staticmethod
    async def create():
        return Database(await aioredis.create_redis(("localhost", 6379)))

    def __init__(self, connection: aioredis.Redis):
        self.connection = connection

    async def add_favorite_station(self, user_id: int, station_code: str):
        await self.connection.sadd("ns:%s:favorites" % user_id, station_code)

    async def get_favorites_stations(self, user_id: int) -> typing.Iterable[str]:
        """
        Gets the codes of the user's favorite stations.
        """
        return await self.connection.smembers("ns:%s:favorites" % user_id, encoding="utf-8")

    async def delete_favorite_station(self, user_id, station_code: str):
        await self.connection.srem("ns:%s:favorites" % user_id, station_code)

    async def get_departures(self, station_code: str) -> typing.List["Departure"]:
        """
        Gets cached departures from the specified station.
        """
        serialized_departures = await self.connection.get("ns:%s:departures" % station_code)
        return pickle.loads(serialized_departures) if serialized_departures else None

    async def set_departures(self, station_code: str, departures):
        await self.connection.setex("ns:%s:departures" % station_code, 60, pickle.dumps(departures))

    async def get_journeys(self, departure_code: str, destination_code: str) -> typing.List["Journey"]:
        serialized_journeys = await self.connection.get("ns:%s:%s:journeys" % (departure_code, destination_code))
        return pickle.loads(serialized_journeys) if serialized_journeys else None

    async def set_journeys(self, departure_code: str, destination_code: str, journeys):
        await self.connection.setex("ns:%s:%s:journeys" % (departure_code, destination_code), 60, pickle.dumps(journeys))

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
            else:
                logging.error("%s → %s", method, payload)
                raise TelegramException(payload["description"])

    def close(self):
        self.session.close()


class TelegramException(Exception):

    def __init__(self, message):
        super().__init__(message)


# Bot Analytics.
# ----------------------------------------------------------------------------------------------------------------------

class Botan:
    """
    Botan.io API.
    """

    HEADERS = {"Content-Type": "application/json"}

    def __init__(self, token: str):
        self.token = token
        self.session = aiohttp.ClientSession()

    async def track(self, uid: typing.Union[None, str, int], name: str, **kwargs):
        """
        Tracks event.
        """
        try:
            async with self.session.post(
                "https://api.botan.io/track",
                params={"token": self.token, "uid": uid, "name": name},
                data=json.dumps(kwargs),
                headers=self.HEADERS,
            ) as response:
                payload = await response.json()
                if payload["status"] == "failed":
                    logging.error("Failed to track event: %s", payload.get("info"))
        except Exception as ex:
            logging.error("Failed to track event.", exc_info=ex)

    def close(self):
        self.session.close()


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
            ) for journey in root
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
    def element_text(element):
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
        self.code_station = {
            station.code: station
            for station in stations
        }  # type: typing.Dict[str, Station]
        self.name_station = {
            name.lower(): station
            for station in stations
            for name in station.names
        }  # type: typing.Dict[str, Station]
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
    LIMIT = 100
    TIMEOUT = 60

    JOURNEY_COUNT = 2
    DEPARTURE_COUNT = 5

    BUTTON_CANCEL = {"text": "Cancel", "callback_data": "/cancel"}
    BUTTON_SEARCH = {"text": "Search Station", "callback_data": "/search"}
    BUTTON_FEEDBACK = {"text": "Bot Feedback", "url": "https://telegram.me/eigenein"}
    BUTTON_BACK = {"text": "Back", "callback_data": "/cancel"}

    TRANSLATE_TABLE = {
        ord("а"): "a", ord("б"): "b", ord("в"): "v", ord("г"): "g", ord("д"): "d", ord("е"): "e", ord("ж"): "zh",
        ord("з"): "z", ord("и"): "i", ord("й"): "i", ord("к"): "k", ord("л"): "l", ord("м"): "m", ord("н"): "n",
        ord("о"): "o", ord("п"): "p", ord("р"): "r", ord("с"): "c", ord("т"): "t", ord("у"): "u", ord("ф"): "f",
        ord("х"): "h", ord("ц"): "c", ord("ч"): "ch", ord("ш"): "sch", ord("щ"): "shch", ord("ъ"): "", ord("ь"): "",
        ord("э"): "e", ord("ю"): "u", ord("я"): "ya",
    }

    def __init__(self, telegram: Telegram, botan: Botan, ns: Ns):
        self.telegram = telegram
        self.botan = botan
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
                await self.botan.track(None, "Error", message=str(ex))

    def stop(self):
        self.is_stopped = True

    def close(self):
        self.db.close()

    async def run_loop(self):
        """
        Executes one message loop iteration. Gets Telegram updates and handles them.
        """
        updates = await self.telegram.get_updates(self.offset, self.LIMIT, self.TIMEOUT)
        for update in updates:
            logging.info("Got update #%s.", update["update_id"])
            self.offset = update["update_id"] + 1
            # Merge message text and callback query data.
            if "message" in update:
                sender = update["message"]["from"]
                text = update["message"].get("text")
                location = update["message"].get("location")
            elif "callback_query" in update:
                sender = update["callback_query"]["from"]
                text = update["callback_query"]["data"]
                location = None
            else:
                continue
            if not text and not location:
                continue
            try:
                future = self.handle_message(sender, text, location)
                if "callback_query" in update:
                    await asyncio.gather(
                        future,
                        self.telegram.answer_callback_query(update["callback_query"]["id"]),
                    )
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
        station_codes = await self.db.get_favorites_stations(user_id)
        buttons = [
            [{"text": self.stations.code_station[station_code].long_name, "callback_data": "/go %s" % station_code}]
            for station_code in station_codes
        ]
        buttons.extend([[self.BUTTON_SEARCH, self.BUTTON_FEEDBACK]])
        return json.dumps({"inline_keyboard": buttons})

    async def handle_message(self, sender: dict, text: str, location: dict):
        """
        Handle single message from a user.
        """
        if text and text.startswith("/"):
            command, *arguments = text.split()
            if command == "/start":
                await self.handle_start(sender)
            elif command == "/search":
                await self.handle_search(sender["id"])
            elif command == "/cancel":
                await self.handle_cancel(sender)
            elif command == "/add":
                await self.handle_add(sender["id"], arguments)
            elif command == "/delete":
                if arguments:
                    await self.handle_delete(sender["id"], arguments[0])
            elif command == "/go":
                if len(arguments) == 1:
                    await self.handle_go_from(sender["id"], arguments[0])
                elif len(arguments) == 2:
                    await self.handle_go_from_to(sender["id"], arguments[0], arguments[1])
            elif command == "/departures":
                if len(arguments) == 1:
                    await self.handle_departures(sender["id"], arguments[0])
        elif text:
            await self.handle_search_query(sender, text)
        elif location:
            await self.handle_location(sender["id"], location["latitude"], location["longitude"])

    async def handle_start(self, sender: dict):
        """
        Handles /start command.
        """
        await asyncio.gather(
            self.telegram.send_message(
                sender["id"],
                Responses.START.format(sender=sender),
                parse_mode=ParseMode.markdown,
                reply_markup=(await self.get_default_keyboard(sender["id"])),
            ),
            self.botan.track(sender["id"], "Start"),
        )

    async def handle_cancel(self, sender: dict):
        """
        Handles /cancel command.
        """
        await asyncio.gather(
            self.telegram.send_message(
                sender["id"],
                Responses.DEFAULT.format(sender=sender),
                parse_mode=ParseMode.markdown,
                reply_markup=(await self.get_default_keyboard(sender["id"])),
            ),
            self.botan.track(sender["id"], "Cancel"),
        )

    async def handle_search(self, user_id: int):
        """
        Handles /search command.
        """
        reply_markup = await self.get_default_keyboard(user_id)
        await asyncio.gather(
            self.telegram.send_message(user_id, Responses.SEARCH, reply_markup=reply_markup),
            self.botan.track(user_id, "Search"),
        )

    async def handle_add(self, user_id: int, station_codes: typing.Iterable[str]):
        """
        Handles /add command.
        """
        for station_code in station_codes:
            await self.db.add_favorite_station(user_id, station_code)
        reply_markup = await self.get_default_keyboard(user_id)
        await asyncio.gather(
            self.telegram.send_message(user_id, Responses.ADDED, reply_markup=reply_markup),
            *(self.botan.track(user_id, "Add", station_code=station_code) for station_code in station_codes),
        )

    async def handle_delete(self, user_id: int, station_code: str):
        """
        Handles /delete command.
        """
        await self.db.delete_favorite_station(user_id, station_code)
        reply_markup = await self.get_default_keyboard(user_id)
        await asyncio.gather(
            self.telegram.send_message(user_id, Responses.DELETED, reply_markup=reply_markup),
            self.botan.track(user_id, "Delete", station_code=station_code),
        )

    async def handle_go_from(self, user_id: int, departure_code: str):
        """
        Handles /go command with one argument provided.
        """
        buttons = await self.get_go_buttons_from(user_id, departure_code)
        buttons.append([
            {"text": "Delete station", "callback_data": "/delete %s" % departure_code},
            {"text": "Departures", "callback_data": "/departures %s" % departure_code},
            self.BUTTON_CANCEL,
        ])
        await asyncio.gather(
            self.telegram.send_message(
                user_id,
                Responses.SELECT_DESTINATION,
                reply_markup=json.dumps({"inline_keyboard": buttons}),
            ),
            self.botan.track(user_id, "From", station_code=departure_code),
        )

    async def handle_go_from_to(self, user_id: int, departure_code: str, destination_code: str):
        """
        Handles /go command with two arguments provided.
        """
        journeys = await self.db.get_journeys(departure_code, destination_code)
        if journeys is None:
            logging.debug("Journeys from %s to %s are not cached.", departure_code, destination_code)
            journeys = await self.ns.plan_journey(departure_code, destination_code)
            # Sometimes NS API returns too old journeys.
            now = datetime.datetime.now()
            journeys = [journey for journey in journeys if journey.actual_departure_time >= now]
            await self.db.set_journeys(departure_code, destination_code, journeys)
        journeys = journeys[:self.JOURNEY_COUNT]

        departure_name = self.stations.code_station[departure_code].long_name
        destination_name = self.stations.code_station[destination_code].long_name

        journeys_text = "\n\n".join(
            Responses.JOURNEY.format(
                journey=journey,
                duration=(journey.actual_duration or journey.planned_duration),
                components_text="\n\n".join(
                    Responses.COMPONENT.format(
                        i=i,
                        component=component,
                        first_stop=component.stops[0],
                        first_stop_warning=(" %s" % Emoji.WARNING_SIGN if component.stops[0].is_platform_changed else ""),
                        last_stop=component.stops[-1],
                        last_stop_warning=(" %s" % Emoji.WARNING_SIGN if component.stops[-1].is_platform_changed else ""),
                    )
                    for i, component in enumerate(journey.components, start=1)
                ),
            ) for journey in journeys
        )
        if journeys_text:
            text = Responses.JOURNEYS.format(departure_name=departure_name, destination_name=destination_name, journeys_text=journeys_text)
        else:
            text = Responses.NO_JOURNEYS.format(departure_name=departure_name, destination_name=destination_name)

        await asyncio.gather(
            self.telegram.send_message(
                user_id,
                text,
                parse_mode=ParseMode.markdown,
                reply_markup=json.dumps({
                    "inline_keyboard": [[
                        self.BUTTON_BACK,
                        {"text": "Refresh", "callback_data": "/go %s %s" % (departure_code, destination_code)},
                    ]],
                }),
            ),
            self.botan.track(
                user_id,
                "Plan",
                departure_code=departure_code,
                destination_code=destination_code,
                route=("%s-%s" % (departure_code, destination_code)),
            ),
        )

    async def handle_departures(self, user_id: int, station_code: str):
        """
        Handles /departure command.
        """
        departures = await self.db.get_departures(station_code)
        if departures is None:
            logging.debug("Departures from %s are not cached.", station_code)
            departures = await self.ns.departures(station_code)
            now = datetime.datetime.now()
            # Sometimes NS API returns too old departures.
            departures = [departure for departure in departures if departure.time >= now]
            await self.db.set_departures(station_code, departures)
        departures = departures[:self.DEPARTURE_COUNT]

        departures_text = "\n\n".join(
            Responses.DEPARTURE.format(
                departure=departure,
                platform_changed=(" %s" % Emoji.WARNING_SIGN if departure.is_platform_changed else " "),
            ).rstrip()
            for departure in departures
        )
        station_name = self.stations.code_station[station_code].long_name
        text = (
            Responses.DEPARTURES.format(station_name=station_name, departures_text=departures_text) if departures
            else Responses.NO_DEPARTURES.format(station_name=station_name)
        )

        await asyncio.gather(
            self.telegram.send_message(
                user_id,
                text,
                parse_mode=ParseMode.markdown,
                reply_markup=json.dumps({"inline_keyboard": [
                    [self.BUTTON_BACK, {"text": "Refresh", "callback_data": "/departures %s" % station_code}],
                ]})
            ),
            self.botan.track(user_id, "Departures", station_code=station_code),
        )

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
            self.get_go_buttons_from(user_id, departure_code),
        )
        buttons.extend([
            [
                {"text": "Add", "callback_data": "/add %s" % station.code},
                {"text": "Departures", "callback_data": "/departures %s" % station.code},
                self.BUTTON_CANCEL,
            ],
        ])
        await asyncio.gather(
            self.telegram.send_location(
                user_id,
                station.latitude,
                station.longitude,
                disable_notification=True,
                reply_markup={"inline_keyboard": buttons},
            ),
            self.botan.track(user_id, "Location", station_code=departure_code),
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
            buttons.append([self.BUTTON_CANCEL])
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
        await asyncio.gather(
            future,
            self.botan.track(sender["id"], "Query", query=text),
        )

    async def get_go_buttons_from(self, user_id: int, departure_code: str) -> typing.List[typing.List[dict]]:
        station_codes = await self.db.get_favorites_stations(user_id)
        return [
            [{
                "text": "Go to %s" % self.stations.code_station[destination_code].long_name,
                "callback_data": "/go %s %s" % (departure_code, destination_code),
            }]
            for destination_code in station_codes
            if destination_code != departure_code
        ]


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
