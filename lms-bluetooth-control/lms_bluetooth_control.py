"""Python agent to interface between a bluetooth audio source and LMS."""

import asyncio
import logging

import aiohttp
from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError
from pysqueezebox import Server
from json import dump

logging.basicConfig(level=logging.WARN)
logger = logging.getLogger(__name__)
logging.getLogger("pysqueezebox").setLevel(logging.WARN)
logger.debug("Starting lms-bluetooth-control")

SERVER = "127.0.0.1"
DEFAULT_PLAYER = "Main Cabin"
TRACK_INFO_JSON = "/tmp/bluetooth_metadata.json"

SERVICE_NAME = "org.bluez"
PLAYER_IFACE = SERVICE_NAME + ".MediaPlayer1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"

with open("objectmanager_introspection.xml", "r", encoding="utf-8") as f:
    OBJECT_MANAGER_INTROSPECTION = f.read()

with open("mediaplayer1_introspection.xml", "r", encoding="utf-8") as f:
    MEDIA_PLAYER1_INTROSPECTION = f.read()


class BluetoothPlayer:
    """Represent a particular bluetooth player and accompanying Squeezebox player object."""

    def __init__(self, bus, player):
        """Initialize BluetoothPlayer.

        :param MessageBus bus:
            The system DBus.

        :param Player player:
            The LMS player to control.
        """
        self.bus = bus
        self.path = None
        self.lms_player = player
        self.mediaplayer1_interface = None
        self.connected = False
        self.lms_pause_watch = None
        self.lms_play_watch = None
        self.remote_mac_address = None

        dbus_object = bus.get_proxy_object(
            SERVICE_NAME, "/", OBJECT_MANAGER_INTROSPECTION
        )

        self.manager = dbus_object.get_interface(OBJECT_MANAGER_IFACE)
        self.manager.on_interfaces_added(self.interfaces_added)

    async def find_player(self):
        """Look for device associated with PLAYER_IFACE."""
        objects = await self.manager.call_get_managed_objects()

        player_path = None

        for path, values in objects.items():
            if "org.bluez.MediaPlayer1" in values:
                player_path = path

        if player_path:
            logger.debug("Found player on path %s", player_path)
            self.connected = True
            bluetooth_player_object = self.bus.get_proxy_object(
                SERVICE_NAME, player_path, MEDIA_PLAYER1_INTROSPECTION
            )
            self.mediaplayer1_interface = bluetooth_player_object.get_interface(
                PLAYER_IFACE
            )

            properties_interface = bluetooth_player_object.get_interface(
                "org.freedesktop.DBus.Properties"
            )
            properties_interface.on_properties_changed(self.properties_changed)

            try:
                if await self.mediaplayer1_interface.get_status() == "playing":
                    return await self.lms_play()
            except DBusError:
                logger.debug("No status property found, presumably player not playing.")

            await self.lms_player.async_update()
            if self.lms_player.url and "wavin:bluealsa" in self.lms_player.url:
                # source already set to bluetooth, so let's watch it's status
                if self.lms_player.mode == "play":
                    await self.pause_watch()
                else:
                    await self.play_watch()
        else:
            logger.debug("Could not find player.")

    async def pause_watch(self):
        """Wait for the LMS player to pause or stop."""
        if self.lms_pause_watch is None or self.lms_pause_watch.done():
            logger.debug("Setting LMS pause watch.")
            self.lms_pause_watch = self.lms_player.create_property_future(
                "mode", lambda x: x != "play"
            )
            await self.lms_pause_watch
            logger.debug("LMS player paused.")
            await self.pause_if_playing()
        else:
            logger.debug("Pause watch already set, player is %s", self.lms_player.mode)

    async def play_watch(self):
        """Wait for the LMS player to play."""
        if self.lms_play_watch is None or self.lms_pause_watch.done():
            logger.debug("Setting LMS play watch.")
            self.lms_play_watch = self.lms_player.create_property_future(
                "mode", lambda x: x == "play"
            )
            await self.lms_play_watch
            logger.debug("LMS player playing.")
            await self.play_if_paused()
        else:
            logger.debug("Play watch already set, player is %s", self.lms_player.mode)

    # pylint: disable=unused-argument
    def interfaces_added(self, object_added, interfaces_and_paths):
        """Monitor for new media player devices."""
        logger.debug("Interfaces added: %s", interfaces_and_paths)
        if "org.bluez.MediaPlayer1" in interfaces_and_paths:
            # this is lazy but should work
            asyncio.create_task(self.find_player())
        else:
            logger.debug("Not a media player.")

    # pylint: disable=unused-argument
    def properties_changed(self, interface, changed, invalidated):
        """Schedule the property change coroutine."""
        asyncio.create_task(self.async_properties_changed(changed))

    async def async_properties_changed(self, changed):
        """Handle relevant property change signals."""
        if "Status" in changed:
            status = changed["Status"].value
            logger.debug("Bluetooth player changed to status %s", status)
            if status == "paused":
                await self.lms_pause()
            elif status == "playing":
                await self.lms_play()

        # write track title, album, and artist to JSON file
        if "Track" in changed:
            track = changed["Track"].value
            track_values = {}
            for key in track:
                track_values[key] = track[key].value
            logger.info("Track changed to %s", track_values)
            # write track info to JSON file
            with open(TRACK_INFO_JSON, "w") as f:
                dump(track_values, f)

    async def lms_play(self):
        """Start LMS player and pause watch."""
        if self.lms_play_watch and not self.lms_play_watch.done():
            logger.debug("Starting player and watching for pause.")
            self.lms_play_watch.cancel()
        mac_address = await self.mediaplayer1_interface.get_device()
        mac_address = mac_address.split("dev_")[1]
        mac_address = mac_address.replace("_", ":")
        await self.lms_player.async_load_url(f"wavin:bluealsa:DEV={mac_address}")
        await self.lms_player.async_play()
        await self.pause_watch()

    async def lms_pause(self):
        """Pause the LMS player and stop watching for a pause."""
        if self.lms_pause_watch and not self.lms_pause_watch.done():
            logger.debug("Pausing player and watching for pause.")
            self.lms_pause_watch.cancel()
        await self.lms_player.async_pause()
        await self.play_watch()

    async def pause_if_playing(self):
        """Confirm LMS paused and send the bluetooth pause command if bluetooth playing."""
        await self.lms_player.async_update()
        if self.lms_player.mode != "play":
            if await self.mediaplayer1_interface.get_status() == "playing":
                logger.debug("Sending pause command to bluetooth player.")
                await self.mediaplayer1_interface.call_pause()
            else:
                logger.debug(
                    "Not sending pause command because bluetooth player is paused."
                )
        else:
            logger.debug("Not pausing bluetooth player because LMS is playing.")

    async def play_if_paused(self):
        """Confirm LMS playing and send the bluetooth play command if bluetooth paused."""
        await self.lms_player.async_update()
        if self.lms_player.mode == "play":
            if await self.mediaplayer1_interface.get_status() == "paused":
                logger.debug("Sending play command to bluetooth player.")
                await self.mediaplayer1_interface.call_play()
            else:
                logger.debug(
                    "Not sending play command because bluetooth player is playing."
                )
        else:
            logger.debug("Not playing bluetooth player because LMS is not playing.")


async def find_active_player(lms):
    """Find an active LMS player, if there is one. Otherwise use default player."""
    assert lms is not None
    players = await lms.async_get_players()
    if not players:
        logger.warning("No Squeezebox players found on server %s", lms)
        return None
    for player in players:
        await player.async_update()
        if player.power:
            logger.info("Found active Squeezebox player %s", player.name)
            return player
    logger.info("Using default Squeezebox player %s", DEFAULT_PLAYER)
    return await lms.async_get_player(DEFAULT_PLAYER)


async def main():
    """Monitor DBus for bluetooth players."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async with aiohttp.ClientSession() as session:
        lms = Server(session, SERVER)
        player = await find_active_player(lms)
        assert player is not None

        bluetooth_player = BluetoothPlayer(bus, player)
        await bluetooth_player.find_player()

        while True:
            await asyncio.sleep(60)
            # check that we still have a player to control
            if not player.power:
                # search for an active player
                logger.info("Player %s powered off", player.name)
                player = await find_active_player(lms)


asyncio.run(main())
