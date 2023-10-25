from micropython import const
from machine import UART

try:
	from asyncio import create_task, sleep_ms, TimeoutError
	from asyncio.funcs import wait_for_ms
	from asyncio.stream import Stream
	from asyncio.lock import Lock
	from asyncio.event import Event
except ImportError:
	from uasyncio import create_task, sleep_ms, TimeoutError
	from uasyncio.funcs import wait_for_ms
	from uasyncio.stream import Stream
	from uasyncio.lock import Lock
	from uasyncio.event import Event

class DFPlayerError(Exception):
	pass
class DFPlayerUnavailableError(DFPlayerError):
	pass
class DFPlayerResponseError(DFPlayerError):
	pass
class DFPlayerUnexpectedResponseError(DFPlayerError):
	pass

_START_BIT = const(0x7E)
_END_BIT   = const(0xEF)
_VERSION   = const(0xFF)

class DFPlayer:
	FOLDER_ROOT = -1
	FOLDER_MP3 = -2
	FOLDER_ADVERT = -3

	STATE_STOPPED = 0
	STATE_PLAYING = 1
	STATE_PAUSED  = 2

	EQ_NORMAL  = 0
	EQ_POP     = 1
	EQ_ROCK    = 2
	EQ_JAZZ    = 3
	EQ_CLASSIC = 4
	EQ_BASS    = 5

	EVENT_INSERT      = 0x3a
	EVENT_EJECT       = 0x3b
	EVENT_DONE_USB    = 0x3c
	EVENT_DONE_SDCARD = 0x3d
	EVENT_DONE_FLASH  = 0x3e
	EVENT_READY       = 0x3f

	def __init__(self, uart_id: int, tx = None, rx = None, timeout = 100, retries = 7, debug = False):
		kwargs = {};
		if tx is not None: kwargs["tx"] = tx
		if rx is not None: kwargs["rx"] = rx

		self._uart = UART(uart_id)
		self._uart.init(
			baudrate=9600, bits=8, parity=None, stop=1, timeout=0,
			**kwargs
		)
		self._stream = Stream(self._uart)
		self._lock = Lock()
		self.timeout = timeout;
		self.retries = retries

		self._buffer_send = bytearray([
			_START_BIT,
			_VERSION,
			6, # number of byes w/o start, end, verification
			0, # command
			1, # whether to use ACK
			0, # param1
			0, # param2
			0, # checksum
			0, # checksum
			_END_BIT
		])
		self._buffer_read = bytearray(10)
		self._read_idle_task = create_task(self._read_idle())

		class Events():
			def __init__(self):
				self.track_done = Event()
				self.track_done.set()
				self.advert_done = Event()
				self.advert_done.set()
		self._events = Events()

		self.debug = debug

	def _log(self, *args, **kwargs):
		print("[DF]", *args, **kwargs)

	async def _read_idle(self):
		while True:
			try:
				await self._read(timeout=None)
				if self.debug:
					self._log("Unhandled data received:", [hex(byte) for byte in self._buffer_read])
			except DFPlayerError as error:
				if self.debug:
					self._log("Unhandled error received:", error)

	def _handle_event(self, event: int):
		# TODO: Handle in-/eject events
		if event == DFPlayer.EVENT_DONE_USB or event == DFPlayer.EVENT_DONE_SDCARD or event == DFPlayer.EVENT_DONE_FLASH:
			if self._events.advert_done.is_set():
				self._events.track_done.set()
			else:
				self._events.advert_done.set()
		elif event == DFPlayer.EVENT_INSERT:
			print(bytes[6], 'inserted')
		elif event == DFPlayer.EVENT_EJECT:
			print(bytes[6], 'ejected')
		elif event == DFPlayer.EVENT_READY:
			print('player ready')

	async def _require_lock(self, coro):
		try:
			await self._lock.acquire()
			self._read_idle_task.cancel()
			return await coro
		except Exception as error:
			raise error
		finally:
			self._lock.release()
			self._read_idle_task = create_task(self._read_idle())

	async def send_cmd(self, *args, **kwargs):
		return await self._require_lock(self._send_cmd(*args, **kwargs))
	async def _send_cmd(self, cmd: int, param1 = 0, param2: int | None = None, timeout: int | None = None):
		if param2 is None:
			param1, param2 = self._uint16_to_bytes(param1)
		if timeout is None:
			timeout = self.timeout

		bytes = self._buffer_send
		bytes[3] = cmd
		bytes[5] = param1
		bytes[6] = param2
		bytes[7], bytes[8] = self._uint16_to_bytes(self._get_checksum(bytes))

		for retries in reversed(range(self.retries + 1)):
			if self.debug:
				self._log("<-- Send CMD", hex(cmd))

			while count := self._uart.any():
				self._uart.read(count)
				if self.debug:
					self._log("Discarded", count, "bytes from RX")
				await sleep_ms(0)
			self._stream.write(bytes)
			await self._stream.drain()

			try:
				await self._read(timeout) # wait for the ACK response
			except DFPlayerError as error:
				if retries == 0:
					raise error
				if self.debug and retries > 0:
					self._log("ERROR:", error)
					self._log("Retrying command...")
				continue

			command = self._buffer_read[3]
			if command != 0x41: # ACK
				raise DFPlayerUnexpectedResponseError("ACK expected, instead received: " + hex(command))
			if self.debug:
				self._log("--> ACKd CMD", hex(cmd))
			break

	async def send_query(self, *args, **kwargs):
		return await self._require_lock(self._send_query(*args, **kwargs))
	async def _send_query(self, cmd: int, param1 = 0, param2: int | None = None, timeout: int | None = None):
		await self._send_cmd(cmd, param1, param2, timeout)
		await self._read(timeout=50)
		bytes = self._buffer_read
		command = bytes[3]
		if command != cmd:
			raise DFPlayerUnexpectedResponseError("Query for " + hex(cmd) + " returned command " + hex(command))
		return bytes

	async def _read(self, timeout: int | None = None):
		bytes = self._buffer_read
		if timeout is None:
			read_count = await self._stream.readinto(bytes)
		else:
			try:
				read_count = await wait_for_ms(self._stream.readinto(bytes), timeout)
			except TimeoutError:
				raise DFPlayerUnavailableError("Response timed out")
		if read_count != 10 or bytes[0] != _START_BIT or bytes[1] != _VERSION or bytes[9] != _END_BIT:
			raise DFPlayerUnavailableError("Malformed response");
		if (bytes[7], bytes[8]) != self._uint16_to_bytes(self._get_checksum(bytes)):
			raise DFPlayerUnavailableError("Malformed response: Invalid checksum");
		if bytes[3] == 0x40: # error response
			err_code = bytes[6]
			err_code_readable = "(" + hex(err_code) + ")"
			if err_code == 0x00:
				raise DFPlayerResponseError("Module is busy " + err_code_readable)
			elif err_code == 0x01:
				raise DFPlayerResponseError("Received incomplete frame " + err_code_readable)
			elif err_code == 0x02:
				raise DFPlayerResponseError("Received corrupt frame " + err_code_readable)
			else:
				raise DFPlayerResponseError("Unknown error " + err_code_readable)
		if (0xF0 & bytes[3]) == 0x30: # event notification
			if self.debug:
				self._log("--> EVENT:", hex(bytes[3]))
			self._handle_event(bytes[3])
			await self._read(timeout)

	def _get_checksum(self, bytes: bytearray):
		result = 0
		for i in range(1, 7):
			result += bytes[i]
		return -result

	def _uint16_to_bytes(self, value: int):
		return (value >> 8 & 0xFF), (value & 0xFF)

	async def play(self, folder: int, file: int):
		if folder == DFPlayer.FOLDER_ADVERT:
			self._events.advert_done.set()
			await sleep_ms(0)
			self._events.advert_done.clear()
			await self.send_cmd(0x13, file)
			return

		self._events.advert_done.set() # playing regular tracks also cancels currently running adverts
		self._events.track_done.set()
		await sleep_ms(0)
		self._events.track_done.clear()

		if folder == DFPlayer.FOLDER_ROOT:
			await self.send_cmd(0x03, file)
		elif folder == DFPlayer.FOLDER_MP3:
			await self.send_cmd(0x12, file)
		else: # numbered folder
			await self.send_cmd(0x0F, folder, file)

	async def play_advert(self, file: int):
		await self.play(DFPlayer.FOLDER_ADVERT, file)

	async def resume(self):
		# DFPlayer seems to take long to process resuming
		await self.send_cmd(0x0D, timeout=self.timeout + 100)

	async def pause(self):
		await self.send_cmd(0x0E)

	async def stop(self):
		await self.send_cmd(0x16)

	async def stop_advert(self):
		await self.send_cmd(0x15)

	async def next(self):
		await self.send_cmd(0x01)

	async def previous(self):
		await self.send_cmd(0x02)

	async def state(self):
		bytes = await self.send_query(0x42)
		return bytes[6]

	async def playing(self):
		# TODO: Add busy pin support
		state = await self.state()
		return state == DFPlayer.STATE_PLAYING

	async def volume(self, volume: int | None = None):
		if volume is None:
			bytes = await self.send_query(0x43)
			return bytes[6]
		else:
			await self.send_cmd(0x06, volume)

	async def eq(self, eq: int | None = None):
		if eq is None:
			bytes = await self.send_query(0x44)
			return bytes[6]
		else:
			await self.send_cmd(0x07, eq)

	async def sleep(self):
		# TODO: Make this work
		await self.send_cmd(0x0A)

	async def wake(self):
		await self.send_cmd(0x0B)

	async def reset(self):
		await self.send_cmd(0x0C, timeout=self.timeout + 200)

	async def wait_track(self):
		await self._events.track_done.wait()

	async def wait_advert(self):
		await self._events.advert_done.wait()
