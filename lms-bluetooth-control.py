"""Python agent to interface between a bluetooth audio source and LMS."""

from dbus_next import Message, MessageType, BusType
from dbus_next.aio import MessageBus
from pysqueezebox import Server
import aiohttp
import asyncio
import logging


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("pysqueezebox").setLevel(logging.WARN)
logger.debug("Starting lms-bluetooth-control.")

SERVER = "127.0.0.1"
DEFAULT_PLAYER = "Aft Cabin"

SERVICE_NAME = "org.bluez"
PLAYER_IFACE = SERVICE_NAME + ".MediaPlayer1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"

with open("objectmanager_introspection.xml", "r") as f:
    OBJECT_MANAGER_INTROSPECTION = f.read()

with open("mediaplayer1_introspection.xml", "r") as f:
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
            logger.debug("Found player on path [{}]".format(player_path))
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

            if await self.mediaplayer1_interface.get_status() == "playing":
                return await self.lms_play()

            await self.lms_player.async_update()
            if self.lms_player.url and "wavin:bluealsa" in self.lms_player.url:
                # source already set to bluetooth, so let's watch it's status
                if self.lms_player.mode == "play":
                    await self.pause_watch()
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
            logger.debug(
                "Pause watch already set, player is {}".format(self.lms_player.mode)
            )

    def interfaces_added(self, object_added, interfaces_and_paths):
        """Monitor for new media player devices."""
        logger.debug("Interfaces added: {}".format(interfaces_and_paths))
        if "org.bluez.MediaPlayer1" in interfaces_and_paths:
            # this is lazy but should work
            asyncio.create_task(self.find_player())
        else:
            logger.debug("Not a media player.")

    def properties_changed(self, interface, changed, invalidated):
        """Schedule the property change coroutine."""
        asyncio.create_task(self.async_properties_changed(changed))

    async def async_properties_changed(self, changed):
        """Handle relevant property change signals."""
        if "Status" in changed:
            status = changed["Status"].value
            logger.debug("Bluetooth player changed to status {}".format(status))
            if status == "paused":
                await self.lms_pause()
            elif status == "playing":
                await self.lms_play()

    async def lms_play(self):
        """Start LMS player and pause watch."""
        await self.lms_player.async_load_url("wavin:bluealsa")
        await self.lms_player.async_play()
        await self.pause_watch()

    async def lms_pause(self):
        if self.lms_pause_watch and not self.lms_pause_watch.done():
            logger.debug("Canceling pause watch.")
            self.lms_pause_watch.cancel()
        else:
            logger.debug("No pause watch set.")
        await self.lms_player.async_pause()

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


async def find_active_player(lms):
    """Find an active LMS player, if there is one. Otherwise use default player."""
    assert lms is not None
    players = await lms.async_get_players()
    if not players:
        logger.warn("No Squeezebox players found on server {}.".format(lms))
        return None
    for player in players:
        await player.async_update()
        if player.power:
            logger.info("Found active Squeezebox player {}.".format(player.name))
            return player
    logger.info("Using default Squeezebox player {}".format(DEFAULT_PLAYER))
    return await lms.async_get_player(DEFAULT_PLAYER)


async def main():
    """Monitor DBus for bluetooth players."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async with aiohttp.ClientSession() as session:
        lms = Server(session, SERVER)
        player = await find_active_player(lms)
        assert player is not None

        bluetoothPlayer = BluetoothPlayer(bus, player)
        await bluetoothPlayer.find_player()

        while True:
            await asyncio.sleep(60)
            # check that we still have a player to control
            if not player.power:
                # search for an active player
                logger.info("Player {} powered off.".format(player.name))
                player = await find_active_player(lms)


asyncio.run(main())
