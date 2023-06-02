# lms-bluetooth-control

Automatically start and pause Bluetooth audio stream in LMS Server

This is designed to run on a Raspberry Pi set up as a bluetooth audio sink and an LMS server.

Note: this script assumes that you have configured a bluetooth receiver as an audio sink using BlueAlsa,
and that it sits on the same server as your LMS instance. It further assumes that the bluetooth audio
can be captured on the `bluealsa` ALSA device. Finally, it assumes that you have the `wavin` plugin installed.

Make sure that the user running this script has access to the bluetooth DBus. The easiest way is to add
the user to the `bluetooth` group.
