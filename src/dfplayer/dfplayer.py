try:
	from collections.abc import Callable
except ImportError:
	pass

from micropython import const
from machine import UART
from binascii import hexlify

try:
	from asyncio import create_task, sleep_ms, Task, TimeoutError
	from asyncio.funcs import wait_for_ms
	from asyncio.stream import Stream
	from asyncio.lock import Lock
	from asyncio.event import Event
except ImportError:
	from uasyncio import create_task, sleep_ms, Task, TimeoutError
	from uasyncio.funcs import wait_for_ms
	from uasyncio.stream import Stream
	from uasyncio.lock import Lock
	from uasyncio.event import Event

class DFPlayerError(Exception):
	pass
class DFPlayerInitializationError(DFPlayerError):
	pass
class DFPlayerTimeoutError(DFPlayerError):
	pass
class DFPlayerTransmissionError(DFPlayerError):
	pass
class DFPlayerInternalError(DFPlayerError):
	def __init__(self, code, *args):
		super().__init__(*args)
		self.code = code
	def __str__(self):
		return "{} ({})".format(self.value, hex(self.code))
class DFPlayerUnexpectedMessageError(DFPlayerError):
	pass

_INIT_FALSE   = const(0)
_INIT_TRUE    = const(1)
_INIT_DEINIT  = const(2)

_START_BIT = const(0x7e)
_END_BIT   = const(0xef)
_VERSION   = const(0xff)
_LENGTH    = const(6)

_EVENT_INSERT      = const(0x3a)
_EVENT_EJECT       = const(0x3b)
_EVENT_DONE_USB    = const(0x3c)
_EVENT_DONE_SDCARD = const(0x3d)
_EVENT_DONE_FLASH  = const(0x3e)
_EVENT_DONE        = _EVENT_DONE_USB # Used as a generic alias for all done events.
_EVENT_READY       = const(0x3f)

_DEVICE_SOURCE_USB    = const(0x01)
_DEVICE_SOURCE_SDCARD = const(0x02)
# _DEVICE_SOURCE_AUX    = const(0x03) # Unclear, seems unused.
_DEVICE_SOURCE_SLEEP  = const(0x04)
_DEVICE_SOURCE_FLASH  = const(0x05)

_DEVICE_FLAG_USB    = const(0b1)
_DEVICE_FLAG_SDCARD = const(0b10)
# _DEVICE_FLAG_PC     = const(0b100) # Unclear, seems unused.
_DEVICE_FLAG_FLASH  = const(0b1000)

_EVENT_TO_DEVICE_FLAG = {
	_EVENT_DONE_USB:    _DEVICE_FLAG_USB,
	_EVENT_DONE_SDCARD: _DEVICE_FLAG_SDCARD,
	_EVENT_DONE_FLASH:  _DEVICE_FLAG_FLASH,
}
_DEVICE_FLAG_TO_SOURCE = {
	_DEVICE_FLAG_USB:    _DEVICE_SOURCE_USB,
	_DEVICE_FLAG_SDCARD: _DEVICE_SOURCE_SDCARD,
	_DEVICE_FLAG_FLASH:  _DEVICE_SOURCE_FLASH,
}

_ERROR_CODE_TO_MESSAGE = {
	0x01: "Module is busy",
	0x02: "Device is in standby mode",
	0x03: "Received corrupt frame",
	0x04: "Invalid checksum",
	0x05: "File index out of bounds",
	0x06: "File not found",
	0x07: "Can only advertise while a track is playing",
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

	# Depending on the use-case, DFPlayer identifies devices differently.
	# Use the bitwise flags for user interfacing, because these are both
	# A) unique (so they can be mapped onto e. g. a playback source) and
	# B) only these can be used to identify devices in the `ready` event.
	DEVICE_USB    = _DEVICE_FLAG_USB
	DEVICE_SDCARD = _DEVICE_FLAG_SDCARD
	DEVICE_FLASH  = _DEVICE_FLAG_FLASH

	LOG_NONE  = _LOG_NONE
	LOG_DEBUG = _LOG_DEBUG
	LOG_ALL   = _LOG_ALL

	def __init__(
		self, uart_id: int,
		timeout = 200, feedback_timeout = 50,
		retries = 5,
		skip_ack: set[int] = {},
		log_level = _LOG_NONE,
	):
		self._init = _INIT_FALSE
		self._uart = UART(uart_id)
		self._stream = Stream(self._uart)
		self._lock = Lock()
		self._read_task: Task | None = None

		self.timeout = timeout
		self.feedback_timeout = feedback_timeout
		self.retries = retries
		self.skip_ack = skip_ack
		self.log_level = log_level

		self._buffer_send = bytearray([
			_START_BIT,
			_VERSION,
			_LENGTH, # number of bytes w/o start, end, checksum
			0, # command
			0, # whether to use ACK
			0, # param1
			0, # param2
			0, # checksum
			0, # checksum
			_END_BIT,
		])
		self._buffer_read = bytearray(10)
		self._buffer_read_partial = bytearray(10)
		self._error: DFPlayerError | None = None
		self._message_receive_ready = Event()
		self._message_receive_done = Event()
		self._message_receive_done.set()

		self._last_selected_device = DFPlayer.DEVICE_SDCARD

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

		class Log():
			def __init__(self, id: int):
				self._id = id
			def __call__(_, min_level = _LOG_DEBUG):
				return self.log_level >= min_level
			def print(self, *args, **kwargs):
				print("[DF {}]".format(self._id), *args, **kwargs)
			def format_error(self, error: BaseException):
				return "{}: {}".format(type(error).__name__, str(error))
		self._log = Log(uart_id)

	def init(self, **kwargs):
		if self._init == _INIT_DEINIT:
			raise DFPlayerInitializationError("Cannot initialize DFPlayer instance after deinit")
		self._uart.init(baudrate=9600, bits=8, parity=None, stop=1, **kwargs)
		self._read_task = create_task(self._read_loop())
		self._init = _INIT_TRUE

	def deinit(self):
		if self._init != _INIT_TRUE:
			raise DFPlayerInitializationError("Cannot deinit uninitialized DFPlayer instance")
		self._stream.close()
		self._uart.deinit()
		self._read_task.cancel()
		self._init = _INIT_DEINIT

	def _get_checksum(self, bytes: bytearray):
		result = 0
		for i in range(1, 7):
			result += bytes[i]
		return -result

	def _uint16_to_bytes(self, value: int):
		return (value >> 8 & 0xFF), (value & 0xFF)

	def _bytes_to_uint16(self, bytes: tuple[int, int]):
		return (bytes[0] << 8) + bytes[1]

	def _validate_read(self, stop: int):
		bytes = self._buffer_read
		if ((stop > 0 and bytes[0] != _START_BIT)
		or (stop > 1 and bytes[1] != _VERSION)
		or (stop > 2 and bytes[2] != _LENGTH)
		or (stop > 9 and bytes[9] != _END_BIT)):
			raise DFPlayerTransmissionError("Corrupt frame received")
		if stop > 8 and (bytes[7], bytes[8]) != self._uint16_to_bytes(self._get_checksum(bytes)):
			raise DFPlayerTransmissionError("Invalid checksum")

	async def _read(self):
		bytes = self._buffer_read
		bytes_partial = self._buffer_read_partial

		read_length = await self._stream.readinto(bytes)
		self._validate_read(read_length)
		while read_length < len(bytes):
			read_length_partial = await self._stream.readinto(bytes_partial)
			for i in range(read_length_partial):
				bytes[read_length + i] = bytes_partial[i]
			read_length += read_length_partial
			self._validate_read(read_length)

		if self._log(_LOG_ALL): self._log.print("RX:", hexlify(bytes, " "))

		if bytes[3] == 0x40: # error reported
			code = bytes[6]
			message = _ERROR_CODE_TO_MESSAGE[code] if code in _ERROR_CODE_TO_MESSAGE else "Unknown error"
			raise DFPlayerInternalError(code, message)

	async def _read_loop(self):
		while True:
			try:
				await self._read()
				if (0xf0 & self._buffer_read[3]) == 0x30: # event notifications
					self._handle_event()
					continue
				self._error = None
			except DFPlayerError as error:
				self._error = error

			if not self._message_receive_done.is_set():
				self._message_receive_ready.set()
				await self._message_receive_done.wait()
			elif self._log(_LOG_ALL):
				self._log.print(
					"Ignoring RX message...",
					"({})".format(self._log.format_error(self._error) if self._error else hex(self._buffer_read[3]))
				)

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
		if self._log(): self._log.print("--> EVENT:", hex(event))

		if event == _EVENT_DONE_USB or event == _EVENT_DONE_SDCARD or event == _EVENT_DONE_FLASH:
			device = _EVENT_TO_DEVICE_FLAG[event]
			self._last_selected_device = device
			track_id = self._bytes_to_uint16((bytes[5], bytes[6]))
			args = (device, track_id)
			event = _EVENT_DONE
			if self._events.advert_done.is_set():
				self._events.track_done.set()
			else:
				self._events.advert_done.set()
		elif event == _EVENT_INSERT or event == _EVENT_EJECT or event == _EVENT_READY:
			device_flags = bytes[6]
			args = (device_flags, )
		else:
			if self._log(): self._log.print("Received unknown event:", hex(event))
			return

		for handler in self._events.handlers[event]:
			handler(*args)

	async def _exec_cmd(self, cmd: int, param1 = 0, param2: int | None = None, timeout: int | None = None):
		if self._init != _INIT_TRUE:
			raise DFPlayerInitializationError("DFPlayer instance must be initialized to execute commands")
		if param2 is None:
			param1, param2 = self._uint16_to_bytes(param1)
		if timeout is None:
			timeout = self.timeout
		use_ack = cmd not in self.skip_ack

		bytes = self._buffer_send
		bytes[3] = cmd
		bytes[4] = use_ack
		bytes[5] = param1
		bytes[6] = param2
		bytes[7], bytes[8] = self._uint16_to_bytes(self._get_checksum(bytes))

		for retries in reversed(range(self.retries + 1)):
			if self._log(): self._log.print("<-- Send CMD", hex(cmd))
			if self._log(_LOG_ALL): self._log.print("TX:", hexlify(bytes, " "))

			self._stream.write(bytes)
			while self._uart.any():
				await sleep_ms(0)
			await self._stream.drain()

			if not use_ack:
				break
			# Wait for ACK:
			try:
				await self._receive_message(timeout)
			except DFPlayerError as error:
				if retries == 0:
					raise error
				if retries > 0 and self._log():
					self._log.print("ERROR ({})".format(self._log.format_error(error)))
					self._log.print("Retrying command...")
				continue

			res_cmd = self._buffer_read[3]
			if res_cmd != 0x41: # acknowledgement
				raise DFPlayerUnexpectedMessageError("ACK expected, instead received: " + hex(res_cmd))
			if self._log(): self._log.print("--> ACKd CMD", hex(cmd))
			break

	def _require_lock(func):
		async def locked(self: DFPlayer, *args, **kwargs):
			try:
				await self._lock.acquire()
				return await func(self, *args, **kwargs)
			finally:
				self._lock.release()
		return locked

	@_require_lock
	async def send_cmd(self, cmd: int, param1 = 0, param2: int | None = None, timeout: int | None = None):
		await self._exec_cmd(cmd, param1, param2, timeout)
		# Wait for feedback (success / internal error), timeout type depends on whether cmd was ACKd:
		try:
			await self._receive_message(self.feedback_timeout if self._buffer_send[4] else self.timeout)
		except DFPlayerInternalError as error:
			raise error
		except (DFPlayerTimeoutError, DFPlayerTransmissionError):
			# Timeout => Success (DFPlayer sends nothing on success)
			# TransmissionError => DFPlayer sent garbage data: Practice shows it's usually a success.
			pass

	@_require_lock
	async def send_query(self, cmd: int, param1 = 0, param2: int | None = None, timeout: int | None = None):
		await self._exec_cmd(cmd, param1, param2, timeout)
		# Wait for feedback (return value / error), timeout type depends on whether cmd was ACKd:
		await self._receive_message(self.feedback_timeout if self._buffer_send[4] else self.timeout)
		bytes = self._buffer_read
		res_cmd = bytes[3]
		if res_cmd != cmd:
			raise DFPlayerUnexpectedMessageError("Query for " + hex(cmd) + " returned " + hex(res_cmd))
		return self._bytes_to_uint16((bytes[5], bytes[6]))



	async def play(self, folder: int, file: int):
		if folder == DFPlayer.FOLDER_ADVERT:
			self._events.advert_done.set()
			await sleep_ms(0)
			self._events.advert_done.clear()
			await self.send_cmd(0x13, file)
			return

		self._events.advert_done.set() # Playing regular tracks also cancels currently running adverts.
		self._events.track_done.set()
		await sleep_ms(0)
		self._events.track_done.clear()

		if folder == DFPlayer.FOLDER_ROOT:
			await self.send_cmd(0x03, file)
		elif folder == DFPlayer.FOLDER_MP3:
			await self.send_cmd(0x12, file)
		else: # numbered folder
			await self.send_cmd(0x0f, folder, file)

	async def play_root(self, file: int):
		await self.play(DFPlayer.FOLDER_ROOT, file)

	async def play_mp3(self, file: int):
		await self.play(DFPlayer.FOLDER_MP3, file)

	async def play_advert(self, file: int):
		await self.play(DFPlayer.FOLDER_ADVERT, file)

	async def resume(self):
		await self.send_cmd(0x0D)

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

	async def standby_enter(self):
		await self.send_cmd(0x0a)

	async def standby_exit(self):
		await self.send_cmd(0x0b)

	async def sleep(self):
		await self.send_cmd(0x09, _DEVICE_SOURCE_SLEEP)

	async def wake(self):
		await self.send_cmd(0x09, _DEVICE_FLAG_TO_SOURCE[self._last_selected_device])

	async def reset(self):
		await self.send_cmd(0x0c) # TODO: Await the accompanying ready event?

	async def source(self, device: int):
		self._last_selected_device = device
		await self.send_cmd(0x09, _DEVICE_FLAG_TO_SOURCE[device])

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
