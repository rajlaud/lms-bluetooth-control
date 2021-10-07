"""Python agent to interface between a bluetooth audio source and LMS."""

from re import S
from dbus_next import Message, MessageType, BusType
from dbus_next.aio import MessageBus
from pysqueezebox import Server
import aiohttp
import asyncio
import logging


SERVER = "127.0.0.1"

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
            logging.debug("Found player on path [{}]".format(player_path))
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
        else:
            logging.debug("Could not find player.")

    def interfaces_added(self, object_added, interfaces_and_paths):
        """Monitor for new media player devices."""
        logging.debug("Interfaces added: {}".format(interfaces_and_paths))
        if "org.bluez.MediaPlayer1" in interfaces_and_paths:
            # this is lazy but should work
            asyncio.create_task(self.find_player())
        else:
            logging.debug("Not a media player.")

    def properties_changed(self, interface, changed, invalidated):
        """Handle relevant property change signals."""
        if "Status" in changed:
            status = changed["Status"].value
            logging.debug("Player changed to status {}".format(status))
            if status == "paused":
                asyncio.create_task(self.lms_player.async_pause())
            elif status == "playing":
                asyncio.create_task(self.lms_player.async_load_url("wavin:bluealsa"))


async def main():
    """Monitor DBus for bluetooth players."""
    logging.basicConfig(level=logging.DEBUG)

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async with aiohttp.ClientSession() as session:
        lms = Server(session, SERVER)
        player = await lms.async_get_player("Aft Cabin")

        bluetoothPlayer = BluetoothPlayer(bus, player)
        await bluetoothPlayer.find_player()

        await asyncio.get_event_loop().create_future()


asyncio.run(main())
