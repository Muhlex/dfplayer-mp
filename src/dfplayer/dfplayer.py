try:
	from collections.abc import Callable
except ImportError:
	pass

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
class DFPlayerTimeoutError(DFPlayerError):
	pass
class DFPlayerTransmissionError(DFPlayerError):
	pass
class DFPlayerInternalError(DFPlayerError):
	pass
class DFPlayerUnexpectedMessageError(DFPlayerError):
	pass

_START_BIT = const(0x7e)
_END_BIT   = const(0xef)
_VERSION   = const(0xff)

_EVENT_INSERT      = const(0x3a)
_EVENT_EJECT       = const(0x3b)
_EVENT_DONE_USB    = const(0x3c)
_EVENT_DONE_SDCARD = const(0x3d)
_EVENT_DONE_FLASH  = const(0x3e)
_EVENT_DONE        = _EVENT_DONE_USB # used as a generic alias where the device is abstracted away
_EVENT_READY       = const(0x3f)

_DEVICE_USB            = const(0x01)
_DEVICE_SDCARD         = const(0x02)
_DEVICE_USB_AND_SDCARD = const(0x03)
_DEVICE_FLASH          = const(0x08)

_EVENT_TO_DEVICE = {
	_EVENT_DONE_USB:    _DEVICE_USB,
	_EVENT_DONE_SDCARD: _DEVICE_SDCARD,
	_EVENT_DONE_FLASH:  _DEVICE_FLASH,
}

_LOG_NONE  = const(0)
_LOG_DEBUG = const(1)
_LOG_ALL   = const(2)

class DFPlayer:
	FOLDER_ROOT   = const(-1)
	FOLDER_MP3    = const(-2)
	FOLDER_ADVERT = const(-3)

	STATE_STOPPED = const(0)
	STATE_PLAYING = const(1)
	STATE_PAUSED  = const(2)

	EQ_NORMAL  = const(0)
	EQ_POP     = const(1)
	EQ_ROCK    = const(2)
	EQ_JAZZ    = const(3)
	EQ_CLASSIC = const(4)
	EQ_BASS    = const(5)

	DEVICE_USB            = _DEVICE_USB
	DEVICE_SDCARD         = _DEVICE_SDCARD
	DEVICE_USB_AND_SDCARD = _DEVICE_USB_AND_SDCARD
	DEVICE_FLASH          = _DEVICE_FLASH

	LOG_NONE  = _LOG_NONE
	LOG_DEBUG = _LOG_DEBUG
	LOG_ALL   = _LOG_ALL

	def __init__(self, uart_id: int, timeout = 100, retries = 5, log_level = _LOG_NONE, **kwargs):
		self._uart = UART(uart_id)
		self._uart.init(
			baudrate=9600, bits=8, parity=None, stop=1, timeout=0,
			**kwargs
		)
		self._stream = Stream(self._uart)
		self._lock = Lock()
		self.timeout = timeout
		self.retries = retries
		self.log_level = log_level

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
			_END_BIT,
		])
		self._buffer_read = bytearray(10)
		self._error: DFPlayerError | None = None
		self._read_task = create_task(self._read_loop()) # TODO: Clean this up? (As well as stream/UART in general)
		self._message_receive_ready = Event()
		self._message_receive_done = Event()
		self._message_receive_done.set()

		class Events():
			def __init__(self):
				self.handlers = {
					_EVENT_INSERT: [],
					_EVENT_EJECT: [],
					_EVENT_DONE: [],
					_EVENT_READY: [],
				}
				self.track_done = Event()
				self.track_done.set()
				self.advert_done = Event()
				self.advert_done.set()
		self._events = Events()

	def _log(self, *args, level = _LOG_DEBUG, **kwargs):
		if self.log_level >= level:
			print("[DF]", *args, **kwargs)

	def _get_checksum(self, bytes: bytearray):
		result = 0
		for i in range(1, 7):
			result += bytes[i]
		return -result

	def _uint16_to_bytes(self, value: int):
		return (value >> 8 & 0xFF), (value & 0xFF)

	def _bytes_to_uint16(self, bytes: tuple[int, int]):
		return (bytes[0] << 8) + bytes[1];

	async def _read(self):
		bytes = self._buffer_read
		read_count = await self._stream.readinto(bytes)
		self._log("Read", read_count, "bytes: ", [hex(byte) for byte in bytes][:read_count], level=_LOG_ALL)
		if read_count != 10:
			raise DFPlayerTransmissionError("Malformed message: Incomplete frame");
		if bytes[0] != _START_BIT or bytes[1] != _VERSION or bytes[9] != _END_BIT:
			raise DFPlayerTransmissionError("Malformed message: Invalid format");
		if (bytes[7], bytes[8]) != self._uint16_to_bytes(self._get_checksum(bytes)):
			raise DFPlayerTransmissionError("Malformed message: Invalid checksum");
		if bytes[3] == 0x40: # error reply
			err_code = bytes[6]
			err_code_readable = "(" + hex(err_code) + ")"
			if err_code == 0x00:
				raise DFPlayerInternalError("Module is busy " + err_code_readable)
			elif err_code == 0x01:
				raise DFPlayerInternalError("Received incomplete frame " + err_code_readable)
			elif err_code == 0x02:
				raise DFPlayerInternalError("Received corrupt frame " + err_code_readable)
			else:
				raise DFPlayerInternalError("Unknown error " + err_code_readable)

	async def _read_loop(self):
		while True:
			try:
				await self._read();
				if (0xf0 & self._buffer_read[3]) == 0x30: # event notifications
					self._handle_event()
					continue;
				self._error = None
			except DFPlayerError as error:
				self._error = error

			if not self._message_receive_done.is_set():
				self._message_receive_ready.set()
				await self._message_receive_done.wait()

	async def _receive_message(self, timeout: int):
		try:
			self._message_receive_done.clear()
			await wait_for_ms(self._message_receive_ready.wait(), timeout)
			if self._error:
				raise self._error
		except TimeoutError:
			raise DFPlayerTimeoutError("DFPlayer did not answer in time")
		finally:
			self._message_receive_ready.clear()
			self._message_receive_done.set()

	def _handle_event(self):
		bytes = self._buffer_read
		event = bytes[3]
		self._log("--> EVENT:", hex(event))

		if event == _EVENT_DONE_USB or event == _EVENT_DONE_SDCARD or event == _EVENT_DONE_FLASH:
			device = _EVENT_TO_DEVICE[event]
			track_id = self._bytes_to_uint16((bytes[5], bytes[6]))
			args = (device, track_id)
			event = _EVENT_DONE
			if self._events.advert_done.is_set():
				self._events.track_done.set()
			else:
				self._events.advert_done.set()
		elif event == _EVENT_INSERT or event == _EVENT_EJECT or event == _EVENT_READY:
			device = bytes[6]
			args = (device, )
		else:
			self._log("Received unknown event:", hex(event));
			return

		for handler in self._events.handlers[event]:
			handler(*args)

	def _require_lock(func):
		async def locked(self: DFPlayer, *args, **kwargs):
			try:
				await self._lock.acquire()
				return await func(self, *args, **kwargs)
			finally:
				self._lock.release()
		return locked

	@_require_lock
	def send_cmd(self, cmd: int, param1 = 0, param2: int | None = None, timeout: int | None = None):
		return self._send_cmd(cmd, param1, param2, timeout)

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
			self._log("<-- Send CMD", hex(cmd))

			self._stream.write(bytes)
			while self._uart.any():
				await sleep_ms(0)
			await self._stream.drain()

			try:
				await self._receive_message(timeout);
			except DFPlayerError as error:
				if retries == 0:
					raise error
				if retries > 0:
					self._log("ERROR ({}: {})".format(type(error).__name__, str(error)))
					self._log("Retrying command...")
				continue

			res_cmd = self._buffer_read[3]
			if res_cmd != 0x41: # ACK
				raise DFPlayerUnexpectedMessageError("ACK expected, instead received: " + hex(res_cmd))
			self._log("--> ACKd CMD", hex(cmd))
			break

	@_require_lock
	async def send_query(self, cmd: int, param1 = 0, param2: int | None = None, timeout: int | None = None):
		await self._send_cmd(cmd, param1, param2, timeout)
		await self._receive_message(self.timeout);
		bytes = self._buffer_read
		res_cmd = bytes[3]
		if res_cmd != cmd:
			raise DFPlayerUnexpectedMessageError("Query for " + hex(cmd) + " returned command " + hex(res_cmd))
		return self._bytes_to_uint16((bytes[5], bytes[6]))

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
			await self.send_cmd(0x0f, folder, file)

	async def play_advert(self, file: int):
		await self.play(DFPlayer.FOLDER_ADVERT, file)

	async def resume(self):
		# DFPlayer seems to take long to process resuming
		await self.send_cmd(0x0D, timeout=self.timeout + 100)

	async def pause(self):
		await self.send_cmd(0x0e)

	async def stop(self):
		await self.send_cmd(0x16)

	async def stop_advert(self):
		await self.send_cmd(0x15)

	async def next(self):
		await self.send_cmd(0x01)

	async def previous(self):
		await self.send_cmd(0x02)

	async def state(self):
		return await self.send_query(0x42)

	async def playing(self):
		# TODO: Add busy pin support
		return (await self.state()) == DFPlayer.STATE_PLAYING

	async def volume(self, volume: int | None = None):
		if volume is None:
			return await self.send_query(0x43)
		else:
			await self.send_cmd(0x06, volume)
			return volume

	async def eq(self, eq: int | None = None):
		if eq is None:
			return await self.send_query(0x44)
		else:
			await self.send_cmd(0x07, eq)
			return eq

	async def sleep(self):
		# TODO: Document that this often doesn't work
		await self.send_cmd(0x0a)

	async def wake(self):
		await self.send_cmd(0x0b)

	async def reset(self):
		await self.send_cmd(0x0c, timeout=self.timeout + 200)

	async def wait_track_done(self):
		await self._events.track_done.wait()

	async def wait_advert_done(self):
		await self._events.advert_done.wait()

	def on_done(self, handler: Callable[[int, int]]):
		self._on(_EVENT_DONE, handler)
	def on_eject(self, handler: Callable[[int]]):
		self._on(_EVENT_EJECT, handler)
	def on_insert(self, handler: Callable[[int]]):
		self._on(_EVENT_INSERT, handler)
	def on_ready(self, handler: Callable[[int]]):
		self._on(_EVENT_READY, handler)

	def _on(self, event: int, handler: Callable):
		self._events.handlers[event].append(handler)

	def off_done(self, handler: Callable[[int, int]] | None = None):
		self._off(_EVENT_DONE, handler)
	def off_eject(self, handler: Callable[[int]] | None = None):
		self._off(_EVENT_EJECT, handler)
	def off_insert(self, handler: Callable[[int]] | None = None):
		self._off(_EVENT_INSERT, handler)
	def off_ready(self, handler: Callable[[int]] | None = None):
		self._off(_EVENT_READY, handler)

	def _off(self, event: int, handler: Callable | None):
		if handler is None:
			self._events.handlers[event].clear()
		else:
			self._events.handlers[event].remove(handler)
