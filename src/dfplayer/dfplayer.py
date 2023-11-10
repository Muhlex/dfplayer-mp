try:
	from collections.abc import Callable
except ImportError:
	pass

from micropython import const
from machine import UART, Pin
from binascii import hexlify

try:
	from asyncio import create_task, sleep_ms, Task, TimeoutError
	from asyncio.funcs import wait_for_ms, gather
	from asyncio.stream import Stream
	from asyncio.lock import Lock
	from asyncio.event import Event, ThreadSafeFlag
except ImportError:
	from uasyncio import create_task, sleep_ms, Task, TimeoutError
	from uasyncio.funcs import wait_for_ms, gather
	from uasyncio.stream import Stream
	from uasyncio.lock import Lock
	from uasyncio.event import Event, ThreadSafeFlag

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

_START_BIT = const(0x7e)
_END_BIT   = const(0xef)
_VERSION   = const(0xff)
_LENGTH    = const(6)

_CMD_NEXT               = const(0x01);
_CMD_PREVIOUS           = const(0x02);
_CMD_PLAY_ID            = const(0x03);
_CMD_VOLUME_INC         = const(0x04);
_CMD_VOLUME_DEC         = const(0x05);
_CMD_VOLUME             = const(0x06);
_CMD_EQ                 = const(0x07);
_CMD_PLAY_ID_REPEAT     = const(0x08);
_CMD_SOURCE             = const(0x09);
_CMD_STANDBY_ON         = const(0x0a);
_CMD_STANDBY_OFF        = const(0x0b);
_CMD_RESET              = const(0x0c);
_CMD_RESUME             = const(0x0d);
_CMD_PAUSE              = const(0x0e);
_CMD_PLAY_FOLDER        = const(0x0f);
_CMD_VOLUME_GAIN        = const(0x10);
_CMD_PLAY_ALL_REPEAT    = const(0x11);
_CMD_PLAY_MP3           = const(0x12);
_CMD_PLAY_ADVERT        = const(0x13);
_CMD_PLAY_FOLDER_XL     = const(0x14);
_CMD_STOP_ADVERT        = const(0x15);
_CMD_STOP               = const(0x16);
_CMD_PLAY_FOLDER_REPEAT = const(0x17);
_CMD_PLAY_ALL_RANDOM    = const(0x18);
_CMD_FILE_REPEAT_MODE   = const(0x19);
_CMD_DAC                = const(0x1a);

_INFO_ERROR             = const(0x40);
_INFO_ACK               = const(0x41);

_QUERY_STATUS           = const(0x42);
_QUERY_VOLUME           = const(0x43);
_QUERY_EQ               = const(0x44);
_QUERY_MODE             = const(0x45);
_QUERY_VERSION          = const(0x46);
_QUERY_FILES_USB        = const(0x47);
_QUERY_FILES_SDCARD     = const(0x48);
_QUERY_FILES_FLASH      = const(0x49);
_QUERY_TRACK_USB        = const(0x4b);
_QUERY_TRACK_SDCARD     = const(0x4c);
_QUERY_TRACK_FLASH      = const(0x4d);
_QUERY_FILES_FOLDER     = const(0x4e);
_QUERY_FOLDERS          = const(0x4f);

_ALL_QUERIES = {
	_QUERY_STATUS, _QUERY_VOLUME, _QUERY_EQ, _QUERY_MODE, _QUERY_VERSION,
	_QUERY_FILES_USB, _QUERY_FILES_SDCARD, _QUERY_FILES_FLASH,
	_QUERY_TRACK_USB, _QUERY_TRACK_SDCARD, _QUERY_TRACK_FLASH,
	_QUERY_FILES_FOLDER, _QUERY_FOLDERS
}

_EVENT_INSERT      = const(0x3a)
_EVENT_EJECT       = const(0x3b)
_EVENT_DONE_USB    = const(0x3c)
_EVENT_DONE_SDCARD = const(0x3d)
_EVENT_DONE_FLASH  = const(0x3e)
_EVENT_DONE        = _EVENT_DONE_USB # Used as a generic alias for all done events.
_EVENT_READY       = const(0x3f)

_DEVICE_SOURCE_USB    = const(0x01)
_DEVICE_SOURCE_SDCARD = const(0x02)
_DEVICE_SOURCE_AUX    = const(0x03) # Unclear, seems unused.
_DEVICE_SOURCE_SLEEP  = const(0x04)
_DEVICE_SOURCE_FLASH  = const(0x05)

_DEVICE_FLAG_USB    = const(0b1)
_DEVICE_FLAG_SDCARD = const(0b10)
_DEVICE_FLAG_PC     = const(0b100) # Unclear, seems unused.
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

_INIT_FALSE   = const(0)
_INIT_TRUE    = const(1)
_INIT_DEINIT  = const(2)

_LOG_NONE  = const(0)
_LOG_DEBUG = const(1)
_LOG_ALL   = const(2)

class DFPlayer:
	"""
	Driver for the DFPlayer module by DFRobot. Compatible with other DFPlayer versions.

	⚠️ All async methods may raise `DFPlayerError`s on communication failure
	or internal errors reported by the module.
	"""
	FOLDER_MP3    = const(-1)
	FOLDER_ADVERT = const(-2)

	STATE_STOPPED = const(0)
	STATE_PLAYING = const(1)
	STATE_PAUSED  = const(2)

	MODE_REPEAT_ALL    = const(1)
	MODE_REPEAT_FILE   = const(2)
	MODE_REPEAT_FOLDER = const(3)
	MODE_RANDOM_ALL    = const(4)
	MODE_SINGLE        = const(5)

	EQ_FLAT    = const(0)
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
		self,
		uart_id: int, busy_pin_id: int | None = None,
		timeout = 200, timeout_feedback = 50, timeout_busy = 300,
		retries = 5,
		skip_ack: set[int] = _ALL_QUERIES,
		log_level = _LOG_NONE,
	):
		"""
		Construct a new DFPlayer object to control a DFPlayer module using UART communication.

		Args:
			uart_id: Hardware UART id
			busy_pin_id: Optional pin id that is connected to the BUSY pin of the DFPlayer (uses up one interrupt slot)
			timeout: Milliseconds allowed for the player to process and acknowledge (ACK) sent commands
			timeout_feedback: Milliseconds allowed between command ACK and feedback (error/query value)
			timeout_busy: Milliseconds allowed between command ACK and busy pin activating (relevant commands only)
			retries: How often to re-send a command on communication failure
			skip_ack: Set of command bytes (see source) to not require/request command ACK for
				(by default excludes all queries as original DFPlayer will never ACK query commands)
			log_level: Print additional communication information (expects ``DFPlayer.LOG_``... constant)
		"""
		df = self
		self._init = _INIT_FALSE
		self._uart = UART(uart_id)
		self._stream = Stream(self._uart)
		self._lock = Lock()
		self._read_task: Task | None = None

		self._busy_pin = None
		self._busy_flag = ThreadSafeFlag()
		if busy_pin_id is not None:
			self._busy_pin = Pin(busy_pin_id, Pin.IN)
			if not self._busy_pin.value(): self._busy_flag.set() # busy pin is low when busy

		self.timeout = timeout
		self.timeout_feedback = timeout_feedback
		self.timeout_busy = timeout_busy
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
		self._error: DFPlayerError | None = None
		self._message_receive_ready = Event()
		self._message_receive_done = Event()
		self._message_receive_done.set()

		self._last_mode = DFPlayer.MODE_SINGLE
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

				class Available(): # Wrap Event API to ensure DFPlayer availability will be fetched once.
					def __init__(self):
						self._fetched = False
						self._event = Event()
					def _ensure_fetch(self):
						if not self._fetched:
							self._fetched = True
							create_task(df._fetch_available())
					def set(self):
						self._fetched = True
						self._event.set()
					def clear(self):
						self._event.clear()
					def is_set(self):
						self._ensure_fetch()
						return self._event.is_set()
					def wait(self):
						self._ensure_fetch()
						return self._event.wait()
				self.available = Available()
		self._events = Events()

		class Log():
			def __init__(self, id: int):
				self._id = id
			def __call__(self, min_level = _LOG_DEBUG):
				return df.log_level >= min_level
			def print(self, *args, **kwargs):
				print("[DF {}]".format(self._id), *args, **kwargs)
			def format_error(self, error: BaseException):
				return "{}: {}".format(type(error).__name__, str(error))
		self._log = Log(uart_id)

	def init(self, **kwargs):
		"""
		Initialize the DFPlayer.

		Needs to be called before any commands can be sent or events can be received.
		Additional keyword-only parameters will be passed to ``UART.init`` of the underlying
		UART bus used for communicating with the player.
		"""
		if self._init == _INIT_DEINIT:
			raise DFPlayerInitializationError("Cannot initialize DFPlayer instance after deinit")
		self._uart.init(baudrate=9600, bits=8, parity=None, stop=1, **kwargs)
		self._read_task = create_task(self._read_loop())
		if self._busy_pin is not None:
			def busy_isr(pin: Pin):
				self._busy_flag.clear() if pin.value() else self._busy_flag.set()
			self._busy_pin.irq(handler=busy_isr, trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING)
		self._init = _INIT_TRUE

	def deinit(self):
		"""
		Turn off the DFPlayer connection.

		Turns off the underlying UART bus and frees an interrupt handler for the busy pin (if used).
		"""
		if self._init != _INIT_TRUE:
			raise DFPlayerInitializationError("Cannot deinit uninitialized DFPlayer instance")
		self._stream.close()
		self._uart.deinit()
		self._read_task.cancel()
		if self._busy_pin is not None:
			self._busy_pin.irq(handler=None)
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

		read_length = await self._stream.readinto(bytes)
		try:
			self._validate_read(read_length)
			while read_length < len(bytes):
				bytes_partial = await self._stream.read(len(bytes) - read_length)
				bytes[read_length:read_length + len(bytes_partial)] = bytes_partial
				read_length += len(bytes_partial)
				self._validate_read(read_length)
		finally:
			if self._log(_LOG_ALL): self._log.print("RX:", hexlify(bytes[:read_length], " "))

		self._events.available.set()

		if bytes[3] == _INFO_ERROR:
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

	async def _exec_cmd(self, cmd: int, param1 = 0, param2: int | None = None, use_ack = False):
		if self._init != _INIT_TRUE:
			raise DFPlayerInitializationError("DFPlayer instance must be initialized to execute commands")
		if param2 is None:
			param1, param2 = self._uint16_to_bytes(param1)

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
				await self._receive_message(self.timeout)
			except DFPlayerError as error:
				if retries == 0:
					raise error
				if retries > 0 and self._log():
					self._log.print("ERROR ({})".format(self._log.format_error(error)))
					self._log.print("Retrying command...")
				continue

			res_cmd = self._buffer_read[3]
			if res_cmd != _INFO_ACK:
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
	async def _fetch_available(self):
		await self._exec_cmd(_QUERY_VOLUME, use_ack=False)
		try:
			# Successful read of any message will set the available event:
			await self._receive_message(self.timeout)
		except:
			pass

	@_require_lock
	async def send_cmd(self, cmd: int, param1 = 0, param2: int | None = None, await_busy = False):
		"""
		Send a raw command to the player.

		While usually unnecessary as the full feature-set of the DFPlayer is supported by the API,
		raw commands can be sent to directly interface with the DFPlayer.

		Args:
			cmd: DFPlayer command byte
			param1: uint8 when first of two parameters or single uint16 parameter
			param2: uint8 second parameter
			await_busy: If set, an error is raised if the player does not report busy state in response
		"""
		use_ack = cmd not in self.skip_ack
		await self._exec_cmd(cmd, param1, param2, use_ack)

		# Wait for feedback (error / success -> error times out or busy pin activates):
		async def wait_feedback():
			timeout_feedback = self.timeout_feedback
			if not use_ack: timeout_feedback = self.timeout
			try:
				await self._receive_message(timeout_feedback)
				raise DFPlayerUnexpectedMessageError("Error expected, instead received: " + hex(self._buffer_read[3]))
			except (DFPlayerTimeoutError, DFPlayerTransmissionError):
				# Timeout => Success (DFPlayer sends nothing on success)
				# TransmissionError => DFPlayer sent garbage data: Practice shows it's usually a success.
				pass
		async def wait_busy():
			timeout_busy = self.timeout_busy
			if not use_ack: timeout_busy += self.timeout
			try:
				await wait_for_ms(self._busy_flag.wait(), timeout_busy)
			except TimeoutError:
				raise DFPlayerTimeoutError("DFPlayer did not go busy in time")
		awaitables = [wait_feedback()]
		if await_busy:
			awaitables.append(wait_busy())

		await gather(*awaitables, return_exceptions=False)

	@_require_lock
	async def send_query(self, cmd: int, param1 = 0, param2: int | None = None):
		"""
		Send a raw query command to the player and return the responded value.

		While usually unnecessary as the full feature-set of the DFPlayer is supported by the API,
		raw query commands can be sent to directly interface with the DFPlayer.

		Args:
			cmd: DFPlayer command byte
			param1: uint8 when first of two parameters or single uint16 parameter
			param2: uint8 second parameter
		"""
		use_ack = cmd not in self.skip_ack
		await self._exec_cmd(cmd, param1, param2, use_ack)

		# Wait for feedback (error / return value):
		timeout_feedback = self.timeout_feedback
		if not use_ack: timeout_feedback = self.timeout

		await self._receive_message(timeout_feedback)
		bytes = self._buffer_read
		res_cmd = bytes[3]
		if (0xf0 & res_cmd) != 0x40:
			raise DFPlayerUnexpectedMessageError("Query result expected, instead received: " + hex(res_cmd))
		return self._bytes_to_uint16((bytes[5], bytes[6]))



	async def play(self, folder: int, file: int, await_start = False):
		"""
		Start playback of a track or advert.

		Adverts will temporarily interrupt tracks.

		⚠️ On some DFPlayer versions, adverts may only be played while a regular track is playing.

		⚠️ Playback from numeric folders and files prefixed with 0 seems to work but may report an
		out-of-bounds error.

		Args:
			folder: Folder of the track to be played (either numeric 1-99 or ``DFPlayer.FOLDER_``... constant).
			file: Numeric file prefix (1-3000 in folders 1-15; 1-256 in folders 16-99; 0-9999 in MP3; 0-9999 in ADVERT)
			await_start: If set, this coroutine only returns once the player starts playback (is busy).
				``busy_pin_id`` must have been passed to constructor.

		Returns:
			An awaitable that will finish at the end of playback.
			When looping is enabled, the awaitable will still finish when the first iteration ends.

		Examples::

			done = await df.play(df.FOLDER_MP3, 24)
			await done
			print("Playback finished.")

			from asyncio import sleep
			from asyncio.funcs import gather
			done = []
			done.append(await df.play(8, 64))
			await sleep(4)
			done.append(await df.play(df.FOLDER_ADVERT, 12))
			await gather(*done)
			print("Playback finished.")
		"""
		if folder == DFPlayer.FOLDER_ADVERT:
			self._events.advert_done.set()
			self._events.advert_done = Event()
			await self.send_cmd(_CMD_PLAY_ADVERT, file, await_busy=await_start)
			return self._events.advert_done.wait()

		self._events.advert_done.set() # Playing regular tracks also cancels currently running adverts.
		self._events.track_done.set()
		self._events.track_done = Event()

		if folder is None: # file is track id
			if self._last_mode == DFPlayer.MODE_REPEAT_FILE:
				await self.send_cmd(_CMD_PLAY_ID_REPEAT, file, await_busy=await_start)
			else:
				await self.send_cmd(_CMD_PLAY_ID, file, await_busy=await_start)
		elif folder == DFPlayer.FOLDER_MP3:
			await self.send_cmd(_CMD_PLAY_MP3, file, await_busy=await_start)
		else: # numbered folder
			if file > 0xff:
				if folder > 0xf:
					raise ValueError("Cannot use folder " + str(folder) + " to playback file numbers > 255 (folder must be < 16)")
				param = (folder << 12) | (file & 0xfff)
				await self.send_cmd(_CMD_PLAY_FOLDER_XL, param, await_busy=await_start)
			else:
				await self.send_cmd(_CMD_PLAY_FOLDER, folder, file, await_busy=await_start)

		return self._events.track_done.wait()

	async def play_id(self, track_id: int, await_start = False):
		"""
		Start playback via track's id.

		The id refers to the index of the file on the filesystem (starting at 1). This usually resembles
		the order in which files were copied onto the storage device and can target files in any folder.

		⚠️ Using this method is seldom the best approach. Other playback methods are more robust when
		playing specific files on the filesystem.
		"""
		return await self.play(None, track_id, await_start)

	async def play_mp3(self, file: int, await_start = False):
		""" Alias of ``play(DFPlayer.FOLDER_MP3, ...)``. """
		return await self.play(DFPlayer.FOLDER_MP3, file, await_start)

	async def play_advert(self, file: int, await_start = False):
		""" Alias of ``play(DFPlayer.FOLDER_ADVERT, ...)``. """
		return await self.play(DFPlayer.FOLDER_ADVERT, file, await_start)

	async def resume(self):
		"""
		Resume playback.

		Can also start playback of the file currently internally selected by the player.
		"""
		await self.send_cmd(_CMD_RESUME)

	async def pause(self):
		""" Pause playback (can be resumed via ``resume()``). """
		await self.send_cmd(_CMD_PAUSE)

	async def stop(self):
		""" Stop playback. """
		await self.send_cmd(_CMD_STOP)

	async def stop_advert(self):
		""" Stop playback of running advert, continuing regular track. """
		await self.send_cmd(_CMD_STOP_ADVERT)

	async def next(self):
		""" Play the next track. """
		await self.send_cmd(_CMD_NEXT)

	async def previous(self):
		""" Play the previous track. """
		await self.send_cmd(_CMD_PREVIOUS)

	async def state(self):
		"""
		Query playback state.

		Returns:
			One of the ``DFPlayer.STATE_``... constants.
		"""
		return await self.send_query(_QUERY_STATUS)

	async def playing(self):
		"""
		Fetch whether playback is in progress.

		Will use the busy pin, if specified in the constructor.
		Otherwise is equivalent to ``(await self.state()) == DFPlayer.STATE_PLAYING``.
		"""
		if self._busy_pin is not None:
			return not self._busy_pin.value()
		return (await self.state()) == DFPlayer.STATE_PLAYING

	async def volume(self, volume: int | None = None):
		"""
		Query or set the player's volume.

		Args:
			volume: Set volume to a value between 0-30 inclusive.
		"""
		if volume is None:
			return await self.send_query(_QUERY_VOLUME)
		else:
			if volume < 0 or volume > 30:
				raise ValueError("Volume must be between 0-30 inclusive")
			await self.send_cmd(_CMD_VOLUME, volume)
			return volume

	async def gain(self, gain: int):
		"""
		Set DAC output gain.
		Args:
			gain: Set gain to a value between 0-31 inclusive.
		"""
		if gain < 0 or gain > 31:
				raise ValueError("Gain must be between 0-31 inclusive")
		await self.send_cmd(_CMD_VOLUME_GAIN, gain > 0, gain)

	async def dac(self, enable = True):
		""" Enable or disable the internal DAC (enabled on DFPlayer boot). """
		await self.send_cmd(_CMD_DAC, not enable)

	async def eq(self, eq: int | None = None):
		"""
		Query or set an equalizer preset.

		Args:
			eq: Set equalizer to one of ``DFPlayer.EQ_``... constants.
		"""
		if eq is None:
			return await self.send_query(_QUERY_EQ)
		else:
			await self.send_cmd(_CMD_EQ, eq)
			return eq

	async def mode(self, mode: int | None = None, folder: int | None = None):
		"""
		Query or set a playback mode.

		⚠️ While ``DFPlayer.MODE_SINGLE`` and ``DFPlayer.MODE_REPEAT_FILE`` passively change the playback
		mode, the other modes will actively start playback.

		⚠️ ``DFPlayer.MODE_REPEAT_ALL`` and ``DFPlayer.MODE_RANDOM_ALL`` will also consider adverts.

		Args:
			mode: Set mode to one of ``DFPlayer.MODE_``... constants.
			folder: Folder to repeat (only with ``DFPlayer.MODE_REPEAT_FOLDER``).
		"""
		if mode is None:
			mode = await self.send_query(_QUERY_MODE)
			self._last_mode = mode
			return mode
		else:
			if mode == DFPlayer.MODE_SINGLE:
				await self.send_cmd(_CMD_FILE_REPEAT_MODE, 1)
			elif mode == DFPlayer.MODE_REPEAT_FILE:
				await self.send_cmd(_CMD_FILE_REPEAT_MODE, 0)
			elif mode == DFPlayer.MODE_REPEAT_FOLDER:
				if folder is None or folder < 0:
					raise ValueError("Must specify numeric folder for folder-repeating playback mode")
				await self.send_cmd(_CMD_PLAY_FOLDER_REPEAT, folder)
			elif mode == DFPlayer.MODE_REPEAT_ALL:
				await self.send_cmd(_CMD_PLAY_ALL_REPEAT, 1)
			elif mode == DFPlayer.MODE_RANDOM_ALL:
				await self.send_cmd(_CMD_PLAY_ALL_RANDOM)
			self._last_mode = mode
			return mode

	async def source(self, device: int):
		"""
		Set the playback source device.

		Args:
			mode: Set mode to one of ``DFPlayer.DEVICE_``... constants.
		"""
		self._last_selected_device = device
		await self.send_cmd(_CMD_SOURCE, _DEVICE_FLAG_TO_SOURCE[device])

	async def standby(self, enable = True):
		"""
		Enable or disable the player's standby mode.

		This is supposed to be a power-saving mode but does not seem to have a power-saving effect on
		most or all DFPlayer versions. Due to the player not processing any commands in this mode,
		disabling standby mode via ``standby(False)`` does not actually seem to work.
		"""
		await self.send_cmd(_CMD_STANDBY_ON if enable else _CMD_STANDBY_OFF)

	async def sleep(self):
		"""
		Set the player into sleep mode.

		This changes the playback source to an idle 'sleep' device.
		"""
		await self.send_cmd(_CMD_SOURCE, _DEVICE_SOURCE_SLEEP)

	async def wake(self):
		"""
		Wake the player from sleep mode.

		This changes the playback source to the last active one known to this driver,
		defaulting to the SD card.
		"""
		await self.send_cmd(_CMD_SOURCE, _DEVICE_FLAG_TO_SOURCE[self._last_selected_device])

	async def reset(self):
		"""
		Resets (reboots) the player.

		The reboot can take up to 3 seconds and can be awaited using ``wait_available()``

		Example::

			await df.reset()
			await df.wait_available()
			# ...
		"""
		await self.send_cmd(_CMD_RESET)
		self._events.available.clear()

	def available(self) -> bool:
		"""
		Get if the player is booted and ready to be interfaced with.

		⚠️ The player may still not process commands if in standby or sleep mode.
		"""
		return self._events.available.is_set()

	def wait_available(self):
		"""
		Await DFPlayer availability (see ``available()``)

		Can be used at program start to ensure the player is booted up before taking commands.

		Returns:
			Awaitable that finishes once the player is available to be interfaced with.

		Example::

			df = DFPlayer(0)
			df.init()
			await df.wait_available()
			# ...
		"""
		return self._events.available.wait()

	async def num_folders(self):
		""" Query the number of available folders. """
		return await self.send_query(_QUERY_FOLDERS)

	async def num_files_folder(self, folder: int):
		"""
		Query the number of files in a numeric folder.

		Args:
			folder: Folder number to query.
		"""
		if folder < 0:
			raise ValueError("Only numeric folders can be queried for number of contained files")
		return await self.send_query(_QUERY_FILES_FOLDER, folder)

	async def num_files_device(self, device: int | None = None):
		"""
		Query the number of files on a given storage device.

		If no device is specified, the last active one known to this driver is queried.

		Args:
			device: ``DFPlayer.DEVICE_``... constant to query.
		"""
		if device is None:
			device = self._last_selected_device

		if device == DFPlayer.DEVICE_USB:
			return await self.send_query(_QUERY_FILES_USB)
		elif device == DFPlayer.DEVICE_SDCARD:
			return await self.send_query(_QUERY_FILES_SDCARD)
		elif device == DFPlayer.DEVICE_FLASH:
			return await self.send_query(_QUERY_FILES_FLASH)

		raise ValueError("Invalid device specified")

	async def track_id(self, device: int | None = None):
		"""
		Query the currently playing/internally selected track's id on a given storage device.

		If no device is specified, the last active one known to this driver is queried.

		Args:
			device: ``DFPlayer.DEVICE_``... constant to query.
		"""
		if device is None:
			device = self._last_selected_device

		if device == DFPlayer.DEVICE_USB:
			return await self.send_query(_QUERY_TRACK_USB)
		elif device == DFPlayer.DEVICE_SDCARD:
			return await self.send_query(_QUERY_TRACK_SDCARD)
		elif device == DFPlayer.DEVICE_FLASH:
			return await self.send_query(_QUERY_TRACK_FLASH)

		raise ValueError("Invalid device specified")

	async def version(self):
		""" Query the player's software version. """
		return await self.send_query(_QUERY_VERSION)

	def _on(self, event: int, handler: Callable):
		self._events.handlers[event].append(handler)

	def _off(self, event: int, handler: Callable | None):
		if handler is None:
			self._events.handlers[event].clear()
		else:
			self._events.handlers[event].remove(handler)

	def on_done(self, handler: Callable[[int, int]]):
		"""
		Register an event handler for completion of file playback.

		⚠️ Some DFPlayer versions may not report this event for adverts or might report it multiple times
		in quick succession for any file type.

		Args:
			handler: Takes integer parameters: ``(device, track_id)``
		"""
		self._on(_EVENT_DONE, handler)

	def on_eject(self, handler: Callable[[int]]):
		"""
		Register an event handler for storage device ejection.

		Args:
			handler: Takes integer parameter: ``(device)``
		"""
		self._on(_EVENT_EJECT, handler)

	def on_insert(self, handler: Callable[[int]]):
		"""
		Register an event handler for storage device insertion.

		⚠️ Some DFPlayer versions may not (always) report this event for device insertion,
		but always or sometimes report the 'ready' event instead.

		Args:
			handler: Takes integer parameter: ``(device)``
		"""
		self._on(_EVENT_INSERT, handler)

	def on_ready(self, handler: Callable[[int]]):
		"""
		Register an event handler for player / storage device readying.

		⚠️ Some DFPlayer versions may not (always) report this event for device readying,
		but always or sometimes report the 'insert' event instead.

		Args:
			handler: Takes integer parameter (bit field of ``DFPlayer.DEVICE_``... constants): ``(devices)``

		Example::

			df.on_ready(lambda d: print("Ready! SD-card available?", bool(d & df.DEVICE_SDCARD)))
		"""
		self._on(_EVENT_READY, handler)

	def off_done(self, handler: Callable[[int, int]] | None = None):
		"""
		Remove one or all event handler(s) for completion of file playback.

		Args:
			handler: Handler to remove (all when ``None``)
		"""
		self._off(_EVENT_DONE, handler)

	def off_eject(self, handler: Callable[[int]] | None = None):
		"""
		Remove one or all event handler(s) for storage device ejection.

		Args:
			handler: Handler to remove (all when ``None``)
		"""
		self._off(_EVENT_EJECT, handler)

	def off_insert(self, handler: Callable[[int]] | None = None):
		"""
		Remove one or all event handler(s) for storage device insertion.

		Args:
			handler: Handler to remove (all when ``None``)
		"""
		self._off(_EVENT_INSERT, handler)

	def off_ready(self, handler: Callable[[int]] | None = None):
		"""
		Remove one or all event handler(s) for player / storage device readying.

		Args:
			handler: Handler to remove (all when ``None``)
		"""
		self._off(_EVENT_READY, handler)
