# DFPlayer Driver for MicroPython

This is a fully featured driver for the DFPlayer MP3 player by DFRobot.
It also supports other manufacturer's versions of the module.

⚠️️ The API is meant to be used in conjunction with the `asyncio` library.
All methods that interface with the DFPlayer must be awaited until
the player acknowledged the command/returned a queried value.

🌐 **[DFRobot Wiki](https://wiki.dfrobot.com/DFPlayer_Mini_SKU_DFR0299)** (for Pinout & Connection)

## Examples
### Basic Usage

```python
from asyncio import run
from dfplayer import DFPlayer

async def main():
	df = DFPlayer(0)
	df.init()
	await df.wait_available() # Optional, make sure DFPlayer is booted.

	await df.volume(15)
	print("DFPlayer reports volume:", await df.volume())

	await df.play(4, 28)

run(main())
```

### Event Listeners
```python
df = DFPlayer(0)
def handle_done(device: int, track_id: int):
	print("Playback ended on storage device: {} (Track ID: {})".format(device, track_id))
df.on_done(handle_done)
df.on_eject(lambda device: print("Device", device, "ejected!"))
df.on_insert(lambda device: print("Device", device, "inserted!"))
df.on_ready(lambda devices: print("Ready! Is the SD-card ready?", bool(devices & df.DEVICE_SDCARD)))

df.init()
await df.play_mp3(12)
```

### Await Playback End
```python
done = await df.play(2, 2540)
await done
print("Track done, playing the next one.")
done = await df.play(2, 2541)
```
