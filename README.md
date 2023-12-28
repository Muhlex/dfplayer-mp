# DFPlayer Mini for MicroPython
This is a fully featured library for interfacing with the DFPlayer Mini MP3 player by DFRobot.
It also supports other manufacturer's versions of the module.

> [!NOTE]
> This driver is meant to be used in conjunction with the `asyncio` library.<br>
> Methods that interface with the DFPlayer should be awaited until
> the player acknowledged the command/returned a queried value.


## Resources
📖 [**API Reference**](https://muhlex.github.io/dfplayer-mp/)<br>
🌐 [**Official DFRobot Wiki**](https://wiki.dfrobot.com/DFPlayer_Mini_SKU_DFR0299)


## Installation
Extract the [latest release](https://github.com/Muhlex/dfplayer-mp/releases/latest) to your MCU's
storage. On most platforms `/lib/` is on the search path per default and should preferably be used
to install libraries to. An example directory may look like this:

```
lib/
└── dfplayer/
    ├── __init__.py
    └── dfplayer.py
main.py
```

> [!TIP]
> The source code contains lengthy Docstrings and is thus quite large.
> You may
> [compile the library to *.mpy* files](https://docs.micropython.org/en/latest/reference/mpyfiles.html)
> to remove this overhead.


## Example Setup

### Required Hardware
- MicroPython-installed MCU with UART capabilities
- DFPlayer Mini
- microSD card (FAT16 or FAT32; up to 32 GB)
- Speaker (3 W output power)

### Wiring
For this wiring diagram an *ESP32-DevKitC* is used.
Any MicroPython-supported MCU with RX/TX pins for UART communication should work.

![Circuit Diagram](https://github.com/Muhlex/dfplayer-mp/assets/21311428/6a219356-be6d-46e9-b53d-9db849c3099f)

Make sure to connect the `RX` pin to a `TX` capable pin on the MCU and vice-versa.
Boards with only a single UART bus are hard to develop with, due to that bus usually being used for
serial debug communication (the MicroPython REPL). This example uses UART bus id `2` for which
the board's *default* pins are `16` (RX) and `17` (TX).

An additional 1k Ohm resistor can be connected between the `RX` input of the DFPlayer Mini and the
`TX` pin of the MCU, if commands are sometimes not being acknowledged due to board noise on the MCU.

### DFPlayer Filesystem
Either *MP3* or *WAV* files can be played back. For simple use cases, use this file structure:
```
01/
├── 001.mp3
├── 002.mp3
├── 003.wav
    ...
└── 255.mp3
02/
├── 001_Files-can-have.mp3
├── 002_human-readable.mp3
└── 003_suffixes.mp3
    ...
```
Folders `01` - `15` can have up to ~3000 files if required.


## Example Code

### Basic Usage
```python
from asyncio import run
from dfplayer import DFPlayer

async def main():
    df = DFPlayer(2) # using UART id 2
    df.init() # initialize UART connection
    await df.wait_available() # optional; making sure DFPlayer finished booting

    await df.volume(15)
    print("DFPlayer reports volume:", await df.volume())

    await df.play(1, 1) # folder 1, file 1

run(main())
```

### Event Listeners
```python
def handle_done(device: int, track_id: int):
    print("Playback ended on storage device: {} (Track ID: {})".format(device, track_id))
df.on_done(handle_done)
df.on_eject(lambda device: print("Device", device, "ejected!"))
df.on_insert(lambda device: print("Device", device, "inserted!"))
df.on_ready(lambda devices: print("Ready! Is the SD-card ready?", bool(devices & df.DEVICE_SDCARD)))

df.init()
```

### Await Playback End
```python
done = await df.play(2, 2540)
await done
print("Track done, playing another one.")

done = await df.play(1, 4)
```
