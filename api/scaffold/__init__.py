# This file is part of Scaffold
#
# Scaffold is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
#
# Copyright 2019 Ledger SAS, written by Olivier Hériveaux


from enum import Enum
import serial
from binascii import hexlify


class TimeoutError(Exception):
    """ Thrown when a polling read or write command timed out. """
    def __init__(self, data=None, size=None):
        """
        :param data: The received data until timeout. None if timeout occured
        during a write operation.
        :param size: The number of successfully proceeded bytes.
        """
        self.data = data
        if self.data is not None:
            assert size is None
            self.size = len(data)
        else:
            self.size = size

    def __str__(self):
        s = "Timeout."
        if self.data is not None:
            if len(self.data):
                h = hexlify(self.data).decode()
                return (
                    f'Read timeout: partially received {len(self.data)} '
                    f'bytes {h}.')
            else:
                return 'Read timeout: no data received.'
        else:
            return f'Write timeout. Only {self.size} bytes written.'


class Signal:
    """
    Base class for all connectable signals in Scaffold. Every :class:`Signal`
    instance has a Scaffold board parent instance which is used to electrically
    configure the hardware board when two :class:`Signal` are connected
    together.
    """
    def __init__(self, parent, path):
        """
        :param parent: Scaffold instance which the signal belongs to.
        :param path: Signal path string. Uniquely identifies a Scaffold board
            internal signal. For instance '/dev/uart0/tx'.
        """
        self.__parent = parent
        self.__path = path

    @property
    def parent(self):
        """ Parent :class:`Scaffold` board instance. Read-only. """
        return self.__parent

    @property
    def path(self):
        """ Signal path. For instance '/dev/uart0/tx'. Read-only. """
        return self.__path

    @property
    def name(self):
        """
        Signal name (last element of the path). For instance 'tx'. Read-only.
        """
        return self.__path.split('/')[-1]

    def __str__(self):
        """ :return: Signal path. For instance '/dev/uart0/tx'. """
        return self.__path

    def __lshift__(self, other):
        """
        Feed the current signal with another signal.

        :param other: Another :class:`Signal` instance. The other signal must
            belong to the same :class:`Scaffold` instance.
        """
        self.__parent.sig_connect(self, other)

    def __rshift__(self, other):
        """
        Feed another signal with current signal.

        :param other: Another :class:`Signal` instance. The other signal must
            belong to the same :class:`Scaffold` instance.
        """
        self.__parent.sig_connect(other, self)


class Module:
    """
    Class to facilitate signals and registers declaration.
    """
    def __init__(self, parent, path=None):
        """
        :param parent: The Scaffold instance owning the object.
        :param path: Base path for the signals. For instance '/uart'.
        """
        self.__parent = parent
        self.__path = path

    def add_signal(self, name):
        """
        Add a new signal to the object and set it as a new attribute of the
        instance.
        :name: Signal name.
        """
        assert not hasattr(self, name)
        if self.__path is None:
            path = '/' + name
        else:
            path = self.__path + '/' + name
        sig = Signal(self.__parent, path)
        self.__dict__[name] = sig

    def add_signals(self, *names):
        """
        Add many signals to the object and set them as new attributes of the
        instance.
        :param names: Name of the signals.
        """
        for name in names:
            self.add_signal(name)

    def add_register(self, name, *args, **kwargs):
        """
        Create a new register.
        :param name: Register name.
        :param args: Arguments passed to Register.__init__.
        :param kwargs: Keyword arguments passed to Register.__init__.
        """
        attr_name = 'reg_' + name
        self.__dict__[attr_name] = Register(self.__parent, *args, **kwargs)

    def __setattr__(self, key, value):
        if key in self.__dict__:
            item = self.__dict__[key]
            if isinstance(item, Register):
                item.set(value)
                return
            else:
                super().__setattr__(key, value)
        super().__setattr__(key, value)

    @property
    def parent(self):
        """ Scaffold instance the module belongs to. Read-only. """
        return self.__parent


class Register:
    """
    Manages accesses to a register of a module. Implements value cache
    mechanism whenever possible.
    """
    def __init__(
            self, parent, mode, address, wideness=1, min_value=None,
            max_value=None):
        """
        :param parent: The Scaffold instance owning the register.
        :param address: 16-bits address of the register.
        :param mode: Access mode string. Can have the following characters: 'r'
            for read, 'w' for write, 'v' to indicate the register is volatile.
            When the register is not volatile, a cache is used for read
            accesses.
        :param wideness: Number of bytes stored by the register. When this
            value is not 1, the register cannot be read.
        :parma min_value: Minimum allowed value. If None, minimum value will be
            0 by default.
        :param max_value: Maximum allowed value. If None, maximum value will be
            2^(wideness*8)-1 by default.
        """
        self.__parent = parent

        if address not in range(0x10000):
            raise ValueError('Invalid register address')
        self.__address = address

        self.__w = 'w' in mode
        self.__r = 'r' in mode
        self.__volatile = 'v' in mode

        if wideness < 1:
            raise ValueError('Invalid wideness')
        if (wideness > 1) and self.__r:
            raise ValueError('Wideness must be 1 if register can be read.')
        self.__wideness = wideness

        if min_value is None:
            # Set default minimum value to 0.
            self.__min_value = 0
        else:
            # Check maximum value.
            if min_value not in range(2**(wideness*8)):
                raise ValueError('Invalid register minimum value')
            self.__min_value = min_value

        if max_value is None:
            # Set default maximum value based on register size.
            self.__max_value = 2**(wideness*8) - 1
        else:
            # Check maximum value.
            if max_value not in range(2**(wideness*8)):
                raise ValueError('Invalid register maximum value')
            self.__max_value = max_value

        if self.__min_value > self.__max_value:
            raise ValueError(
                'Register minimum value must be lower or equal to maximum '
                'value')

        self.__cache = None

    def set(self, value, poll=None, poll_mask=0xff, poll_value=0x00):
        """
        Set a new value to the register. This method will check bounds against
        the minimum and maximum allowed values of the register. If polling is
        enabled and the register is wide, polling is applied for each byte of the
        register.
        :param value: New value.
        :param poll: Register instance or address. None if polling is not
            required.
        :param poll_mask: Register polling mask.
        :param poll_value: Register polling value.
        """
        if value < self.__min_value:
            raise ValueError('Value too low')
        if value > self.__max_value:
            raise ValueError('Value too high')
        if not self.__w:
            raise RuntimeError('Register cannot be written')
        # Handle wideness
        value_bytes = value.to_bytes(self.__wideness, 'big', signed=False)
        self.__parent.bus.write(self.__address, value_bytes, poll, poll_mask,
            poll_value)
        # Save as int
        self.__cache = value

    def get(self):
        """
        :return: Current register value.
        If the register is not volatile and the value has been cached, no
        access to the board is performed and the cache is returned. If the
        register is not volatile but can't be read, the cached value is
        returned or an exception is raised if cache is not set.
        """
        if self.__volatile:
            if not self.__r:
                raise RuntimeError('Register cannot be read')
            return self.__parent.bus.read(self.__address)[0]
        else:
            # Register is not volatile, so its data can be cached.
            if self.__cache is None:
                if self.__r:
                    value = self.__parent.bus.read(self.__address)[0]
                    self.__cache = value
                else:
                    raise RuntimeError('Register cannot be read')
            return self.__cache

    def or_set(self, value):
        """
        Sets some bits to 1 in the register.
        :param value: An int.
        """
        self.set(self.get() | value)

    def set_bit(self, index, value, poll=None, poll_mask=0xff, poll_value=0x00):
        """
        Sets the value of a single bit of the register.
        :param index: Bit index, in [0, 7].
        :param value: True, False, 0 or 1.
        :param poll: Register instance or address. None if polling is not
            required.
        :param poll_mask: Register polling mask.
        :param poll_value: Register polling value.
        """
        self.set(
            (self.get() & ~(1 << index)) | (int(bool(value)) << index),
            poll, poll_mask, poll_value)

    def get_bit(self, index):
        """
        :return: Value of a given bit, 0 or 1.
        :param index: Bit index, in [0, 7].
        """
        return (self.get() >> index) & 1

    def set_mask(self, value, mask, poll=None, poll_mask=0xff, poll_value=0x00):
        """
        Set selected bits value.
        :param value: Bits value.
        :param mask: A mask indicating which bits must be sets.
        :param poll: Register instance or address. None if polling is not
            required.
        :param poll_mask: Register polling mask.
        :param poll_value: Register polling value.
        """
        # TODO: raise an exception is the register is declared as volatile ?
        current = self.get()
        self.set((current & (~mask)) | (value & mask), poll, poll_mask,
            poll_value)

    def write(self, data, poll=None, poll_mask=0xff, poll_value=0x00):
        """
        Raw write in the register. This method raises a RuntimeError if the
        register cannot be written.
        :param data: Data to be written. Can be a byte, bytes or bytearray.
        :param poll: Register instance or address. None if polling is not
            required.
        :param poll_mask: Register polling mask.
        :param poll_value: Register polling value.
        """
        if not self.__w:
            raise RuntimeError('Register cannot be written')
        self.__parent.bus.write(
            self.__address, data, poll, poll_mask, poll_value)

    def read(self, size=1, poll=None, poll_mask=0xff, poll_value=0x00):
        """
        Raw read the register. This method raises a RuntimeError if the
        register cannot be read.
        :param poll: Register instance or address. None if polling is not
            required.
        :param poll_mask: Register polling mask.
        :param poll_value: Register polling value.
        :return: bytearray
        """
        if not self.__r:
            raise RuntimeError('Register cannot be read')
        return self.__parent.bus.read(
            self.__address, size, poll, poll_mask, poll_value)

    @property
    def address(self):
        """ :return: Register address. """
        return self.__address


class Version(Module):
    """ Version module of Scaffold. """
    def __init__(self, parent):
        """
        :param parent: The Scaffold instance owning the version module.
        """
        super().__init__(parent)
        self.add_register('data', 'r', 0x0100)

    def get_string(self):
        """
        Read the data register multiple times until the full version string has
        been retrieved.
        :return: Hardware version string.
        """
        # We consider the version string is not longer than 32 bytes. This
        # allows us reading the string with only one command to be faster.
        buf = self.reg_data.read(32 + 1 + 32 + 1)
        offset = 0
        result = ''
        # Find the first \0 character.
        while buf[offset] != 0:
            offset += 1
        offset += 1
        # Read characters until second \0 character.
        while buf[offset] != 0:
            result += chr(buf[offset])
            offset += 1
        return result


class LEDMode(Enum):
    EVENT = 0
    VALUE = 1


class LED:
    """
    Represents a LED of the board.
    Each instance of this class is an attribute of a :class:`LEDs` instance.
    """
    def __init__(self, parent, index):
        """
        :param parent: Parent LEDs module instance.
        :param index: Bit index of the LED.
        """
        self.__parent = parent
        self.__index = index

    @property
    def mode(self):
        """
        LED lighting mode. When mode is EVENT, the led is lit for a short
        period of time when an edge is detected on the monitored signal. When
        the mode is VALUE, the LED is lit when the monitored signal is high.
        Default mode is EVENT.

        :type: LEDMode.
        """
        return LEDMode((self.__parent.reg_mode.get() >> self.__index) & 1)

    @mode.setter
    def mode(self, value):
        self.__parent.reg_mode.set_mask(
            LEDMode(value).value << self.__index, 1 << self.__index)


class LEDs(Module):
    """ LEDs module of Scaffold. """
    def __init__(self, parent):
        """
        :param parent: The Scaffold instance owning the version module.
        """
        super().__init__(parent)
        self.add_register('control', 'w', 0x0200)
        self.add_register('brightness', 'w', 0x0201)
        self.add_register('leds_0', 'w', 0x0202)
        self.add_register('leds_1', 'w', 0x0203)
        self.add_register('leds_2', 'w', 0x0204)
        self.add_register('mode', 'w', 0x0205, wideness=3)
        leds = ['a0', 'b1', 'b0', 'b1', 'c0', 'c1', 'd0', 'd1', 'd2', 'd3',
            'd4', 'd5']
        for i, name in enumerate(leds):
            self.__setattr__(name, LED(self, i+6))

    def reset(self):
        """ Set module registers to default values. """
        self.reg_control = 0
        self.reg_brightness.set(20)
        self.reg_mode.set(0)

    @property
    def brightness(self):
        """
        LEDs brightness. 0 is the minimum. 1 is the maximum.

        :type: float
        """
        return self.reg_brightness.get() / 127.0

    @brightness.setter
    def brightness(self, value):
        if (value < 0) or (value > 1):
            raise ValueError('Invalid brightness value')
        self.reg_brightness.set(int(value * 127))

    @property
    def disabled(self):
        """ If set to True, LEDs driver outputs are all disabled. """
        return bool(self.reg_control.get() & 1)

    @disabled.setter
    def disabled(self, value):
        value = int(bool(value))
        self.reg_control.set_mask(value, 1)

    @property
    def override(self):
        """
        If set to True, LEDs state is the value of the leds_n registers.
        """
        return bool(self.reg_control.get() & 2)

    @override.setter
    def override(self, value):
        value = int(bool(value))
        self.reg_control.set_mask(value << 1, 2)


class UART(Module):
    """
    UART module of Scaffold.
    """
    __REG_CONTROL_BIT_FLUSH = 0
    __REG_CONFIG_BIT_TRIGGER = 3

    def __init__(self, parent, index):
        """
        :param parent: The Scaffold instance owning the UART module.
        :param index: UART module index.
        """
        super().__init__(parent, f'/uart{index}')
        self.__index = index
        # Declare the signals
        self.add_signals('rx', 'tx', 'trigger')
        # Declare the registers
        self.__addr_base = base = 0x0400 + 0x0010 * index
        self.add_register('status', 'rv', base)
        self.add_register('control', 'w', base + 1)
        self.add_register('config', 'w', base + 2)
        self.add_register('divisor', 'w', base + 3, wideness=2, min_value=1)
        self.add_register('data', 'rwv', base + 4)
        # Current board target baudrate (this is not the effective baudrate)
        self.__cache_baudrate = None
        # Accuracy parameter
        self.max_err = 0.01

    def reset(self):
        """
        Reset the UART to a default configuration: 9600 bps, no parity, one
        stop bit, trigger disabled.
        """
        self.reg_config.set(0)
        self.reg_control.set(0)
        self.baudrate = 9600

    @property
    def baudrate(self):
        """
        Target UART baudrate.

        :getter: Returns current baudrate, or None if no baudrate has
            been previously set during current session.
        :setter: Set target baudrate. If baudrate cannot be reached within 1%
            accuracy, a RuntimeError is thrown. Reading the baudrate attribute
            after setting it will return the real effective baudrate.
        """
        return self.__cache_baudrate

    @baudrate.setter
    def baudrate(self, value):
        """
        Set target baudrate. If baudrate is too low or too high, a ValueError
        is thrown. If baudrate cannot be reached within 1% accuracy, a
        RuntimeError is thrown.
        :param value: New target baudrate.
        """
        d = round((self.parent.SYS_FREQ / value) - 1)
        # Check that the divisor can be stored on 16 bits.
        if d > 0xffff:
            raise ValueError('Target baudrate is too low.')
        if d < 1:
            raise ValueError('Target baudrate is too high.')
        # Calculate error between target and effective baudrates
        real = self.parent.SYS_FREQ / (d + 1)
        err = abs(real - value) / value
        max_err = self.max_err
        if err > max_err:
            raise RuntimeError(
                f'Cannot reach target baudrate within {max_err*100}% '
                'accuracy.')
        self.reg_divisor.set(d)
        self.__cache_baudrate = real

    def transmit(self, data, trigger=False):
        """
        Transmit data using the UART.

        :param data: Data to be transmitted. bytes or bytearray.
        :param trigger: True or 1 to enable trigger on last byte, False or 0 to
            disable trigger.
        """
        if trigger:
            buf = data[:-1]
        else:
            buf = data
        # Polling on status.ready bit before sending each character.
        self.reg_data.write(
            buf, poll=self.reg_status, poll_mask=0x01, poll_value=0x01)
        if trigger:
            config = self.reg_config.get()
            # Enable trigger as soon as previous transmission ends
            self.reg_config.write(
                config | (1 << self.__REG_CONFIG_BIT_TRIGGER),
                poll=self.reg_status, poll_mask=0x01, poll_value=0x01)
            # Send the last byte. No need for polling here, because it has
            # already been done when enabling trigger.
            self.reg_data.write(data[-1])
            # Disable trigger
            self.reg_config.write(
                config, poll=self.reg_status, poll_mask=0x01, poll_value=0x01)

    def receive(self, n=1):
        """
        Receive n bytes from the UART. This function blocks until all bytes
        have been received or the timeout expires and a TimeoutError is thrown.
        """
        return self.reg_data.read(
            n, poll=self.reg_status, poll_mask=0x04, poll_value=0x00)

    def flush(self):
        """ Discard all the received bytes in the FIFO. """
        self.reg_control.set_bit(self.__REG_CONTROL_BIT_FLUSH, 1)


class PulseGenerator(Module):
    """
    Pulse generator module of Scaffold.
    Usually abreviated as pgen.
    """
    def __init__(self, parent, index):
        """
        :param parent: The Scaffold instance owning the UART module.
        :param index: UART module index.
        """
        super().__init__(parent, '/pgen{0}'.format(index))
        # Create the signals
        self.add_signals('start', 'out')
        # Create the registers
        self.__addr_base = base = 0x0300 + 0x0010 * index
        self.add_register('status', 'rv', base)
        self.add_register('control', 'wv', base + 1)
        self.add_register('config', 'w', base + 2)
        self.add_register('delay', 'w', base + 3, wideness=3)
        self.add_register('interval', 'w', base + 4, wideness=3)
        self.add_register('width', 'w', base + 5, wideness=3)
        self.add_register('count', 'w', base + 6, wideness=2)

    def fire(self):
        """ Manually trigger the pulse generation. """
        self.reg_control.set(1)

    def __duration_to_clock_cycles(self, t):
        """
        Calculate the number of clock cycles corresponding to a given time.
        :param t: Time in seconds. float.
        """
        if t < 0:
            raise ValueError('Duration cannot be negative')
        cc = round(t * self.parent.SYS_FREQ)
        return cc

    def __clock_cycles_to_duration(self, cc):
        """
        Calculate the time elapsed during a given number of clock cycles.
        :param cc: Number of clock cycles.
        """
        return cc / self.parent.SYS_FREQ

    @property
    def delay(self):
        """ Delay before pulse, in seconds. float. """
        return self.__clock_cycles_to_duration(self.reg_delay.get()+1)

    @delay.setter
    def delay(self, value):
        n = self.__duration_to_clock_cycles(value)-1
        self.reg_delay.set(n)

    @property
    def interval(self):
        """ Delay between pulses, in seconds. float. """
        return self.__clock_cycles_to_duration(self.reg_interval.get()+1)

    @interval.setter
    def interval(self, value):
        n = self.__duration_to_clock_cycles(value)-1
        self.reg_interval.set(n)

    @property
    def width(self):
        """ Pulse width, in seconds. float. """
        return self.__clock_cycles_to_duration(self.reg_width.get()+1)

    @width.setter
    def width(self, value):
        n = self.__duration_to_clock_cycles(value)-1
        self.reg_width.set(n)

    @property
    def count(self):
        """
        Number of pulses to be generated. Minimum value is 1. Maximum value is
        2^16.
        """
        return self.reg_count.get()+1

    @count.setter
    def count(self, value):
        if value not in range(1, 2**16+1):
            raise ValueError('Invalid pulse count')
        self.reg_count.set(value-1)


class Power(Module):
    """ Controls the platform and DUT sockets power supplies. """
    __ADDR_CONTROL = 0x0600

    def __init__(self, parent):
        """ :param parent: The Scaffold instance owning the power module. """
        super().__init__(parent, '/power')
        self.add_register('control', 'rwv', self.__ADDR_CONTROL)
        self.add_signals('dut_trigger', 'platform_trigger')

    @property
    def all(self):
        """
        All power-supplies state. int. Bit 0 corresponds to the DUT power
        supply. Bit 1 corresponds to the platform power-supply. When a bit is
        set to 1, the corresponding power supply is enabled. This attribute can
        be used to control both power supplies simultaneously.
        """
        return self.reg_control.get()

    @all.setter
    def all(self, value):
        assert (value & ~0b11) == 0
        self.reg_control.set(value)

    @property
    def platform(self):
        """ Platform power-supply state. int. """
        return self.reg_control.get_bit(1)

    @platform.setter
    def platform(self, value):
        self.reg_control.set_bit(1, value)

    @property
    def dut(self):
        """ DUT power-supply state. int. """
        return self.reg_control.get_bit(0)

    @dut.setter
    def dut(self, value):
        self.reg_control.set_bit(0, value)


class ISO7816ParityMode(Enum):
    EVEN = 0b00  # Even parity (standard and default)
    ODD = 0b01  # Odd parity
    FORCE_0 = 0b10  # Parity bit always 0
    FORCE_1 = 0b11  # Parity bit always 1


class ISO7816(Module):
    """
    ISO7816 peripheral of Scaffold. Does not provide convention or protocol
    management. See :class:`scaffold.iso7816.Smartcard` for more features.
    """
    __REG_STATUS_BIT_READY = 0
    __REG_STATUS_BIT_PARITY_ERROR = 1
    __REG_STATUS_BIT_EMPTY = 2
    __REG_CONTROL_BIT_FLUSH = 0
    __REG_CONFIG_TRIGGER_TX = 0
    __REG_CONFIG_TRIGGER_RX = 1
    __REG_CONFIG_TRIGGER_LONG = 2
    __REG_CONFIG_PARITY_MODE = 3

    def __init__(self, parent):
        """
        :param parent: The Scaffold instance owning the UART module.
        """
        super().__init__(parent, '/iso7816')
        self.add_signals('io_in', 'io_out', 'clk', 'trigger')
        self.__addr_base = base = 0x0500
        self.add_register('status', 'rv', base)
        self.add_register('control', 'w', base + 1)
        self.add_register('config', 'w', base + 2)
        self.add_register('divisor', 'w', base + 3)
        self.add_register('etu', 'w', base + 4, wideness=2)
        self.add_register('data', 'rwv', base + 5)
        # Accuracy parameter
        self.max_err = 0.01

    def reset_config(self):
        """
        Reset ISO7816 peripheral to its default configuration.
        """
        self.reg_config.set(0)
        self.etu = 372
        self.clock_frequency = 1e6  # 1 MHz

    @property
    def clock_frequency(self):
        """
        Target ISO7816 clock frequency. According to ISO7816-3 specification,
        minimum frequency is 1 Mhz and maximum frequency is 5 MHz. Scaffold
        hardware allows going up to 50 Mhz and down to 195312.5 Hz (although
        this may not work with the smartcard).

        :getter: Returns current clock frequency, or None if it has not been
            set previously.
        :setter: Set clock frequency. If requested frequency cannot be reached
            within 1% accuracy, a RuntimeError is thrown. Reading this
            attribute after setting it will return the real effective clock
            frequency.
        """
        return self.__cache_clock_frequency

    @clock_frequency.setter
    def clock_frequency(self, value):
        d = round((0.5 * self.parent.SYS_FREQ / value) - 1)
        # Check that the divisor fits one unsigned byte.
        if d > 0xff:
            raise ValueError('Target clock frequency is too low.')
        if d < 0:
            raise ValueError('Target clock frequency is too high.')
        # Calculate error between target and effective clock frequency
        real = self.parent.SYS_FREQ / ((d + 1) * 2)
        err = abs(real - value) / value
        max_err = self.max_err = 0.01
        if err > max_err:
            raise RuntimeError(
                f'Cannot reach target clock frequency within {max_err*100}% '
                'accuracy.')
        self.reg_divisor.set(d)
        self.__cache_clock_frequency = real

    @property
    def etu(self):
        """
        ISO7816 ETU parameter. Value must be in range [1, 2^11-1]. Default ETU
        is 372.
        """
        return self.reg_etu.get() + 1

    @etu.setter
    def etu(self, value):
        if value not in range(1, 2**11):
            raise ValueError('Invalid ETU parameter')
        self.reg_etu.set(value - 1)

    def flush(self):
        """ Discard all the received bytes in the FIFO. """
        self.reg_control.write(1 << self.__REG_CONTROL_BIT_FLUSH)

    def receive(self, n=1):
        """
        Receive bytes. This function blocks until all bytes have been
        received or the timeout expires and a TimeoutError is thrown.

        :param n: Number of bytes to be read.
        """
        return self.reg_data.read(
            n, poll=self.reg_status,
            poll_mask=(1 << self.__REG_STATUS_BIT_EMPTY), poll_value=0x00)

    def transmit(self, data):
        """
        Transmit data.

        :param data: Data to be transmitted.
        :type data: bytes
        """
        # Polling on status.ready bit before sending each character
        self.reg_data.write(
            data, poll=self.reg_status,
            poll_mask=(1 << self.__REG_STATUS_BIT_READY),
            poll_value=(1 << self.__REG_STATUS_BIT_READY))

    @property
    def empty(self):
        """ True if reception FIFO is empty. """
        return bool(self.reg_status.get() & (1 << self.__REG_STATUS_BIT_EMPTY))

    @property
    def parity_mode(self):
        """
        Parity mode. Standard is Even parity, but it can be changed to odd or
        forced to a fixed value for testing purposes.
        :type: ISO7816ParityMode
        """
        return ISO7816ParityMode(
            (self.reg_config.get() >> self.__REG_CONFIG_PARITY_MODE) & 0b11)

    @parity_mode.setter
    def parity_mode(self, value):
        self.reg_config.set_mask(
            (value.value & 0b11) << self.__REG_CONFIG_PARITY_MODE,
            0b11 << self.__REG_CONFIG_PARITY_MODE)

    @property
    def trigger_tx(self):
        """
        Enable or disable trigger upon transmission.
        :type: bool
        """
        return bool(self.reg_config.get_bit(self.__REG_CONFIG_TRIGGER_TX))

    @trigger_tx.setter
    def trigger_tx(self, value):
        self.reg_config.set_bit(self.__REG_CONFIG_TRIGGER_TX, value)

    @property
    def trigger_rx(self):
        """
        Enable or disable trigger upon reception.
        :type: bool
        """
        return bool(self.reg_config.get_bit(self.__REG_CONFIG_TRIGGER_RX))

    @trigger_rx.setter
    def trigger_rx(self, value):
        self.reg_config.set_bit(self.__REG_CONFIG_TRIGGER_RX, value)

    @property
    def trigger_long(self):
        """
        Enable or disable long trigger (set on transmission, cleared on
        reception). When changing this value, wait until transmission buffer is
        empty.

        :type: bool
        """
        return bool(self.reg_config.get_bit(self.__REG_CONFIG_TRIGGER_LONG))

    @trigger_long.setter
    def trigger_long(self, value):
        # We want until transmission is ready to avoid triggering on a pending
        # one.
        self.reg_config.set_bit(self.__REG_CONFIG_TRIGGER_LONG, value,
            poll=self.reg_status, poll_mask=1<<self.__REG_STATUS_BIT_READY,
            poll_value=1<<self.__REG_STATUS_BIT_READY)


class I2CNackError(Exception):
    """
    This exception is thrown by I2C peripheral when a transaction received a
    NACK from the I2C slave.
    """
    def __init__(self, index):
        """
        :param index: NACKed byte index. If N, then the N-th byte has not been
            acked.
        :type index: int
        """
        super().__init__()
        self.index = index

    def __str__(self):
        """ :return: Error details on the NACKed I2C transaction. """
        return f"Byte of index {self.index} NACKed during I2C transaction."


class I2C(Module):
    """
    I2C module of Scaffold.
    """
    __REG_STATUS_BIT_READY = 0
    __REG_STATUS_BIT_NACK = 1
    __REG_STATUS_BIT_DATA_AVAIL = 2
    __REG_CONTROL_BIT_START = 0
    __REG_CONTROL_BIT_FLUSH = 1
    __REG_CONFIG_BIT_TRIGGER_START = 0
    __REG_CONFIG_BIT_TRIGGER_END = 1
    __REG_CONFIG_BIT_CLOCK_STRETCHING = 2

    def __init__(self, parent, index):
        """
        :param parent: The Scaffold instance owning the UART module.
        :param index: I2C module index.
        """
        super().__init__(parent, f'/i2c{index}')
        self.__index = index
        # Declare the signals
        self.add_signals('sda_in', 'sda_out', 'scl_in', 'scl_out', 'trigger')
        # Declare the registers
        self.__addr_base = base = 0x0700 + 0x0010 * index
        self.add_register('status', 'rv', base)
        self.add_register('control', 'w', base + 1)
        self.add_register('config', 'w', base + 2)
        self.add_register('divisor', 'w', base + 3, wideness=2, min_value=1)
        self.add_register('data', 'rwv', base + 4)
        self.add_register('size_h', 'rwv', base + 5)
        self.add_register('size_l', 'rwv', base + 6)
        self.address = None
        # Current I2C clock frequency
        self.__cache_frequency = None

    def reset_config(self):
        """
        Reset the I2C peripheral to a default configuration.
        """
        self.reg_divisor = 1
        self.reg_size_h = 0
        self.reg_size_l = 0
        self.reg_config = (
            (1 << self.__REG_CONFIG_BIT_CLOCK_STRETCHING) |
            (1 << self.__REG_CONFIG_BIT_TRIGGER_START) )

    def flush(self):
        """ Discards all bytes in the transmission/reception FIFO. """
        self.reg_control.write(1 << self.__REG_CONTROL_BIT_FLUSH)

    def raw_transaction(self, data, read_size, trigger=None):
        """
        Executes an I2C transaction. This is a low-level function which does not
        manage I2C addressing nor read/write mode (those shall already be
        defined in data parameter).

        :param data: Transmitted bytes. First byte is usually the address of the
            slave and the R/W bit. If the R/W bit is 0 (write), this parameter
            shall then contain the bytes to be transmitted, and read_size shall
            be zero.
        :type data: bytes
        :param read_size: Number of bytes to be expected from the slave. 0 in
            case of a write transaction.
        :type read_size: int
        :param trigger: Trigger configuration. If int and value is 1, trigger
            is asserted when the transaction starts. If str, it may contain the
            letter 'a' and/or 'b', where 'a' asserts trigger on transaction
            start and 'b' on transaction end.
        :type trigger: int or str.
        :raises I2CNackError: If a NACK is received during the transaction.
        """
        # Verify trigger parameter before doing anything
        t_start = False
        t_end = False
        if type(trigger) is int:
            if trigger not in range(2):
                raise ValueError('Invalid trigger parameter')
            t_start = (trigger == 1)
        elif type(trigger) is str:
            t_start = ('a' in trigger)
            t_end = ('b' in trigger)
        else:
            if trigger is not None:
                raise ValueError('Invalid trigger parameter')
        # We are going to update many registers. We start a lazy section to make
        # the update faster: all the acknoledgements of bus write operations are
        # checked at the end.
        with self.parent.lazy_section():
            self.flush()
            self.reg_size_h = read_size >> 8
            self.reg_size_l = read_size & 0xff
            # Preload the FIFO
            self.reg_data.write(data)
            # Configure trigger for this transaction
            config_value = 0
            if t_start:
                config_value |= (1 << self.__REG_CONFIG_BIT_TRIGGER_START)
            if t_end:
                config_value |= (1 << self.__REG_CONFIG_BIT_TRIGGER_END)
            # Write config with mask to avoid overwritting clock_stretching
            # option bit
            self.reg_config.set_mask(
                config_value,
                (1 << self.__REG_CONFIG_BIT_TRIGGER_START) |
                (1 << self.__REG_CONFIG_BIT_TRIGGER_END) )
            # Start the transaction
            self.reg_control.write(1 << self.__REG_CONTROL_BIT_START)
            # End of lazy section. Leaving the scope will automatically check
            # the responses of the Scaffold write operations.
        # Wait until end of transaction and read NACK flag
        st = self.reg_status.read(
            poll=self.reg_status,
            poll_mask=(1 << self.__REG_STATUS_BIT_READY),
            poll_value=(1 << self.__REG_STATUS_BIT_READY))[0]
        nacked = (st & (1 << self.__REG_STATUS_BIT_NACK)) != 0
        # Fetch all the bytes which are stored in the FIFO.
        fifo = bytearray()
        while (self.reg_status.get() & (1 << self.__REG_STATUS_BIT_DATA_AVAIL)):
            fifo.append(self.reg_data.read()[0])
        if nacked:
            # Get the number of bytes remaining.
            remaining = ((self.reg_size_h.get() << 8)
                + self.reg_size_l.get())
            raise I2CNackError(len(data) - remaining - 1)
        return bytes(fifo)

    def __make_header(self, address, rw):
        """
        Internal method to build the transaction header bytes.

        :param address: Slave device address. If None, self.address is used by
            default. If defined, LSB must be 0 (this is the R/W bit).
        :type address: int or None
        :param rw: R/W bit value, 0 or 1.
        :type rw: int
        :return: Header bytearray.
        """
        result = bytearray()
        assert rw in (0, 1)
        # Check that the address is defined in parameters or in self.address.
        if address is None:
            address = self.address
        if address is None:
            raise ValueError('I2C transaction address is not defined')
        # Check address
        if address < 0:
            raise ValueError('I2C address cannot be negative')
        if address >= 2**11:  # R/W bit counted in address, so 11 bits max
            raise ValueError('I2C address is too big')
        if address > 2**8:
            # 10 bits addressing mode
            # R/W bit is bit 8.
            if address & 0x10:
                raise ValueError('I2C address bit 8 (R/W) must be 0')
            result.append(0xf0 + (address >> 8) + rw)
            result.append(address & 0x0f)
        else:
            # 7 bits addressing mode
            # R/W bit is bit 0.
            if address & 1:
                raise ValueError('I2C address LSB (R/W) must be 0')
            result.append(address + rw)
        return result

    def read(self, size, address=None, trigger=None):
        """
        Perform an I2C read transaction.

        :param address: Slave device address. If None, self.address is used by
            default. If defined and addressing mode is 7 bits, LSB must be 0
            (this is the R/W bit). If defined and addressing mode is 10 bits,
            bit 8 must be 0.
        :type address: int or None
        :return: Bytes from the slave.
        :raises I2CNackError: If a NACK is received during the transaction.
        """
        data = self.__make_header(address, 1)
        return self.raw_transaction(data, size, trigger)

    def write(self, data, address=None, trigger=None):
        """
        Perform an I2C write transaction.

        :param address: Slave device address. If None, self.address is used by
            default. If defined and addressing mode is 7 bits, LSB must be 0
            (this is the R/W bit). If defined and addressing mode is 10 bits,
            bit 8 must be 0.
        :type address: int or None
        :raises I2CNackError: If a NACK is received during the transaction.
        """
        data = self.__make_header(address, 0) + data
        self.raw_transaction(data, 0, trigger)

    @property
    def clock_stretching(self):
        """
        Enable or disable clock stretching support. When clock stretching is
        enabled, the I2C slave may hold SCL low during a transaction. In this
        mode, an external pull-up resistor on SCL is required. When clock
        stretching is disabled, SCL is always controlled by the master and the
        pull-up resistor is not required.

        :type: bool or int.
        """
        return self.reg_config.get_bit(self.__REG_CONFIG_BIT_CLOCK_STRETCHING)

    @clock_stretching.setter
    def clock_stretching(self, value):
        self.reg_config.set_bit(self.__REG_CONFIG_BIT_CLOCK_STRETCHING, value)

    @property
    def frequency(self):
        """
        Target I2C clock frequency.

        :getter: Returns current frequency.
        :setter: Set target frequency. Effective frequency may be different if
            target cannot be reached accurately.
        """
        return self.__cache_frequency

    @frequency.setter
    def frequency(self, value):
        d = round((self.parent.SYS_FREQ / (4 * value)) - 1)
        # Check that the divisor can be stored on 16 bits.
        if d > 0xffff:
            raise ValueError('Target frequency is too low.')
        if d < 1:
            raise ValueError('Target frequency is too high.')
        real = self.parent.SYS_FREQ / (d + 1)
        self.reg_divisor.set(d)
        self.__cache_frequency = real


class IO(Signal):
    """
    Board I/O.
    """
    def __init__(self, parent, path, index):
        """
        :param parent: Scaffold instance which the signal belongs to.
        :param path: Signal path string.
        :param index: I/O index.
        """
        super().__init__(parent, path)
        self.index = index
        self.__group = index // 8
        self.__group_index = index % 8
        base = 0xe000 + 0x10 * self.__group
        self.reg_value = Register(parent, 'rv', base + 0x00)
        self.reg_event = Register(parent, 'rwv', base + 0x01)

    @property
    def value(self):
        """
        Current IO logical state.

        :getter: Senses the input pin of the board and return either 0 or 1.
        :setter: Sets the output to 0, 1 or high-impedance state (None). This
            will disconnect the I/O from any already connected internal
            peripheral. Same effect can be achieved using << operator.
        """
        return (self.reg_value.get() >> self.__group_index) & 1

    @property
    def event(self):
        """
        I/O event register.

        :getter: Returns 1 if an event has been detected on this input, 0
            otherwise.
        :setter: Writing 0 to clears the event flag. Writing 1 has no effect.
        """
        result = (self.reg_event.get() >> self.__group_index) & 1
        return result

    def clear_event(self):
        """
        Clear event register.

        :warning: If an event is received during this call, it may be cleared
            without being took into account.
        """
        self.reg_event.set(0xff ^ (1 << self.__group_index))


class GroupIO(IO):
    """
    Board I/O in group A, B or C. Those I/Os are special since their operating
    voltage can be configured to either 3.3 V or 5.0 V by switching an on-board
    jumper. Voltage configuration applies to groups of I/Os, and as a side
    effect, all I/Os of a same group are in input only mode, or output only
    mode. This class allows setting the direction of the I/O and will check for
    conflicting configurations in a same group of I/Os.
    """
    @property
    def dir(self):
        """
        Current I/O direction.

        :getter: Returns :class:`IODir.INPUT` if the I/O is in input mode,
            :class:`IODir.OUTPUT` if the I/O is in output mode, or None if it
            is undecided (thus the mode will be the same as the other I/O of
            the same group).
        :setter: Changes the direction of the I/O. The API will verify that the
            new configuration does not conflict with the other I/O of the same
            group. Accepted values are :class:`IODir.INPUT`,
            :class:`IODir.OUTPUT` or None.
        """
        # TODO
        pass


class ScaffoldBusLazySection:
    """
    Helper class to be sure the opened lazy sections are closed at some time.
    """
    def __init__(self, bus):
        self.bus = bus

    def __enter__(self):
        self.bus.lazy_start()

    def __exit__(self, type, value, traceback):
        self.bus.lazy_end()


class ScaffoldBus:
    """
    Low level methods to drive the Scaffold device.
    """
    MAX_CHUNK = 255

    def __init__(self):
        self.ser = None
        self.__lazy_writes = []
        self.__lazy_stack = 0

    def connect(self, dev):
        """
        Connect to Scaffold board using the given serial port.
        :param dev: Serial port device path. For instance '/dev/ttyUSB0' on
            linux, 'COM0' on Windows.
        """
        self.ser = serial.Serial(dev, baudrate=2000000)

    def prepare_datagram(
            self, rw, addr, size, poll, poll_mask, poll_value):
        """
        Helper function to build the datagrams to be sent to the Scaffold
        device. Also performs basic check on arguments.
        :rw: 1 for a write command, 0 for a read command.
        :addr: Register address.
        :size: Size of the data to be sent or received. Maximum size is 255.
        :param poll: Register instance or address. None if polling is not
            required.
        :poll_mask: Register polling mask.
        :poll_value: Register polling value.
        :return: A bytearray.
        """
        if rw not in range(2):
            raise ValueError('Invalid rw argument')
        if size not in range(1, self.MAX_CHUNK+1):
            raise ValueError('Invalid size')
        if addr not in range(0x10000):
            raise ValueError('Invalid address')
        if isinstance(poll, Register):
            poll = poll.address
        if (poll is not None) and (poll not in range(0x10000)):
            raise ValueError('Invalid polling address')
        command = rw
        if size > 1:
            command |= 2
        if poll is not None:
            command |= 4
        datagram = bytearray()
        datagram.append(command)
        datagram.append(addr >> 8)
        datagram.append(addr & 0xff)
        if poll is not None:
            datagram.append(poll >> 8)
            datagram.append(poll & 0xff)
            datagram.append(poll_mask)
            datagram.append(poll_value)
        if size > 1:
            datagram.append(size)
        return datagram

    def write(
            self, addr, data, poll=None, poll_mask=0xff, poll_value=0x00):
        """
        Write data to a register.
        :param addr: Register address.
        :param data: Data to be written. Can be a byte, bytes or bytearray.
        :param poll: Register instance or address. None if polling is not
            required.
        :param poll_mask: Register polling mask.
        :param poll_value: Register polling value.
        """
        if self.ser is None:
            raise RuntimeError('Not connected to board')

        # If data is an int, convert it to bytes.
        if type(data) is int:
            data = bytes([data])

        offset = 0
        remaining = len(data)
        while remaining:
            chunk_size = min(self.MAX_CHUNK, remaining)
            datagram = self.prepare_datagram(
                1, addr, chunk_size, poll, poll_mask, poll_value)
            datagram += data[offset:offset + chunk_size]
            self.ser.write(datagram)
            if self.__lazy_stack == 0:
                # Check immediately the result of the write operation.
                ack = self.ser.read(1)[0]
                if ack != chunk_size:
                    assert poll is not None
                    # Timeout error !
                    raise TimeoutError(size=offset+ack)
            else:
                # Lazy-update section. The write result will be checked later,
                # when all lazy-sections are closed.
                self.__lazy_writes.append(chunk_size)
            remaining -= chunk_size
            offset += chunk_size

    def read(
            self, addr, size=1, poll=None, poll_mask=0xff,
            poll_value=0x00):
        """
        Read data from a register.
        :param addr: Register address.
        :param poll: Register instance or address. None if polling is not
            required.
        :param poll_mask: Register polling mask.
        :param poll_value: Register polling value.
        :return: bytearray
        """
        if self.ser is None:
            raise RuntimeError('Not connected to board')
        # Read operation not permitted during lazy-update sections
        if self.__lazy_stack > 0:
            raise RuntimeError('Read operations not allowed during lazy-update '
                'section.')
        result = bytearray()
        remaining = size
        while remaining:
            chunk_size = min(self.MAX_CHUNK, remaining)
            datagram = self.prepare_datagram(
                0, addr, chunk_size, poll, poll_mask, poll_value)
            self.ser.write(datagram)
            res = self.ser.read(chunk_size+1)
            ack = res[-1]
            if ack != chunk_size:
                assert poll is not None
                result += res[:ack]
                raise TimeoutError(data=result)
            result += res[:-1]
            remaining -= chunk_size
        return result

    def set_timeout(self, value):
        """
        Configure the polling timeout register.
        :param value: Timeout register value. If 0 the timeout is disabled.
        """
        if (value < 0) or (value > 0xffffffff):
            raise ValueError('Timeout value out of range')
        datagram = bytearray()
        datagram.append(0x08)
        datagram += value.to_bytes(4, 'big', signed=False)
        self.ser.write(datagram)
        # No response expected from the board

    @property
    def is_connected(self):
        return self.set is not None

    def lazy_start(self):
        """
        Enters lazy-check update block, or add a block level if already in
        lazy-check mode. When lazy-check is enabled, the result of write
        operations on Scaffold bus are not checked immediately, but only when
        leaving all blocks. This allows updating many different registers
        without the serial latency because all the responses will be checked at
        once.
        """
        self.__lazy_stack += 1

    def lazy_end(self):
        """
        Close current lazy-update block. If this was the last lazy section,
        fetch all responses from Scaffold and check that all write operations
        went good. If any write-operation timed-out, the last TimeoutError is
        thrown.
        """
        if self.__lazy_stack == 0:
            raise RuntimeError('No lazy section started')
        self.__lazy_stack -= 1
        last_error = None
        if self.__lazy_stack == 0:
            # We closes all update blocks, we must now check all responses of
            # write requests.
            for expected_size in self.__lazy_writes:
                ack = self.ser.read(1)[0]
                if ack != expected_size:
                    # Timeout error !
                    last_error = TimeoutError(size=ack)
        self.__lazy_writes.clear()
        if last_error is not None:
            raise last_error

    def lazy_section(self):
        """
        :return: ScaffoldBusLazySection to be used with the python 'with'
            tatement to start and close a lazy update section.
        """
        return ScaffoldBusLazySection(self)


class IODir(Enum):
    """
    I/O direction mode.
    """
    INPUT = 0
    OUTPUT = 1


class Scaffold:
    """
    This class connects to a Scaffold board and provides access to all the
    device parameters and peripherals.

    :ivar uarts: list of :class:`scaffold.UART` instance managing UART
        peripherals.
    :ivar i2cs: list of :class:`scaffold.I2C` instance managing I2C peripherals.
    :ivar iso7816: :class:`scaffold.ISO7816` instance managing the ISO7816
        peripheral.
    :ivar pgens: list of four :class:`scaffold.PulseGenerator` instance managing
        the FPGA pulse generators.
    :ivar power: :class:`scaffold.Power` instance, enabling control of the power
        supplies of DUT and platform sockets.
    :ivar leds: :class:`scaffold.LEDs` instance, managing LEDs brightness and
        lighting mode.
    :ivar [a0,a1,b0,b1,c0,c1,d0,d1,d2,d3,d4,d5]: :class:`scaffold.Signal`
        instances for connecting and controlling the corresponding I/Os of the
        board.
    """

    # FPGA frequency: 100 MHz
    SYS_FREQ = 100e6

    # Number of D outputs
    __IO_D_COUNT = 16
    # Number of UART peripherals
    __UART_COUNT = 2
    # Number of pulse generator peripherals
    __PULSE_GENERATOR_COUNT = 4
    # Number of I2C modules
    __I2C_COUNT = 1

    # How long in seconds one timeout unit is.
    __TIMEOUT_UNIT = (3.0/SYS_FREQ)

    __ADDR_MTXR_BASE = 0xf100
    __ADDR_MTXL_BASE = 0xf000

    def __init__(self, dev="/dev/scaffold"):
        """
        Create Scaffold API instance.

        :param dev: If specified, connect to the hardware Scaffold board using
            the given serial device. If None, call connect method later to
            establish the communication.
        """
        # Hardware version module
        # There is no need to expose it.
        self.__version_module = Version(self)
        # Cache the version string once read
        self.__version_string = None

        # Power module
        self.power = Power(self)
        # Leds module
        self.leds = LEDs(self)

        # Create the IO signals
        self.a0 = IO(self, '/io/a0', 0)
        self.a1 = IO(self, '/io/a1', 1)
        self.b0 = IO(self, '/io/b0', 2)
        self.b1 = IO(self, '/io/b1', 3)
        self.c0 = IO(self, '/io/c0', 4)
        self.c1 = IO(self, '/io/c1', 5)
        for i in range(self.__IO_D_COUNT):
            self.__setattr__(f'd{i}', IO(self, f'/io/d{i}', 6+i))

        # Create the UART modules
        self.uarts = []
        for i in range(self.__UART_COUNT):
            uart = UART(self, i)
            self.uarts.append(uart)
            self.__setattr__(f'uart{i}', uart)

        # Create the pulse generator modules
        self.pgens = []
        for i in range(self.__PULSE_GENERATOR_COUNT):
            pgen = PulseGenerator(self, i)
            self.pgens.append(pgen)
            self.__setattr__(f'pgen{i}', pgen)

        # Declare the I2C peripherals
        self.i2cs = []
        for i in range(self.__I2C_COUNT):
            i2c = I2C(self, i)
            self.i2cs.append(i2c)
            self.__setattr__(f'i2c{i}', i2c)

        # Create the ISO7816 module
        self.iso7816 = ISO7816(self)

        # Low-level management
        # Set as an attribute to avoid having all low level routines visible in
        # the higher API Scaffold class.
        self.bus = ScaffoldBus()
        if dev is not None:
            self.connect(dev)

        # Timeout value. This value can't be read from the board, so we cache
        # it there once set.
        self.__cache_timeout = None

        # Timeout stack for push_timeout and pop_timeout methods.
        self.__timeout_stack = []

        # FPGA left matrix input signals
        self.mtxl_in = [
            '0', '1', '/io/a0', '/io/a1', '/io/b0', '/io/b1', '/io/c0',
            '/io/c1']
        self.mtxl_in += list(f'/io/d{i}' for i in range(self.__IO_D_COUNT))

        # FPGA left matrix output signals
        self.mtxl_out = []
        for i in range(self.__UART_COUNT):
            self.mtxl_out.append(f'/uart{i}/rx')
        self.mtxl_out.append('/iso7816/io_in')
        for i in range(self.__PULSE_GENERATOR_COUNT):
            self.mtxl_out.append(f'/pgen{i}/start')
        for i in range(self.__I2C_COUNT):
            self.mtxl_out.append(f'/i2c{i}/sda_in')
            self.mtxl_out.append(f'/i2c{i}/scl_in')

        # FPGA right matrix input signals
        self.mtxr_in = [
            'z', '0', '1', '/power/dut_trigger', '/power/platform_trigger']
        for i in range(self.__UART_COUNT):
            self.mtxr_in += [
                f'/uart{i}/tx',
                f'/uart{i}/trigger']
        self.mtxr_in += [
            '/iso7816/io_out',
            '/iso7816/clk',
            '/iso7816/trigger']
        self.mtxr_in += list(
            f'/pgen{i}/out' for i in range(self.__PULSE_GENERATOR_COUNT))
        for i in range(self.__I2C_COUNT):
            self.mtxr_in += [
                f'/i2c{i}/sda_out',
                f'/i2c{i}/scl_out',
                f'/i2c{i}/trigger']

        # FPGA right matrix output signals
        self.mtxr_out = [
            '/io/a0',
            '/io/a1',
            '/io/b0',
            '/io/b1',
            '/io/c0',
            '/io/c1']
        self.mtxr_out += list(f'/io/d{i}' for i in range(self.__IO_D_COUNT))

    def connect(self, dev):
        """
        Connect to Scaffold board using the given serial port.
        :param dev: Serial port device path. For instance '/dev/ttyUSB0' on
            linux, 'COM0' on Windows.
        """
        self.bus.connect(dev)
        # Check hardware responds and has the correct version.
        self.__version_string = self.__version_module.get_string()
        if self.__version_string != 'scaffold-0.2':
            raise RuntimeError(
                'Invalid hardware version \'' + self.__version_string + '\'')
        # Reset to a default configuration
        self.timeout = 0
        for uart in self.uarts:
            uart.reset()
        self.leds.reset()
        self.iso7816.reset_config()
        for i2c in self.i2cs:
            i2c.reset_config()

    @property
    def version(self):
        """
        :return: Hardware version string. This string is queried and checked
            when connecting to the board. It is then cached and can be accessed
            using this property.
        """
        if self.__version_string is None:
            raise RuntimeError('Not connected to board')
        return self.__version_string

    def __signal_to_path(self, signal):
        """
        Convert a signal, 0, 1 or None to a path. Verify the signal belongs to
        the current Scaffold instance.
        :param signal: Signal, 0, 1 or None.
        :return: Path string.
        """
        if isinstance(signal, Signal):
            if signal.parent != self:
                raise ValueError('Signal belongs to another Scaffold instance')
            return signal.path
        elif type(signal) == int:
            if signal not in (0, 1):
                raise ValueError('Invalid signal value')
            return str(signal)
        elif type(signal) == int:
            return str(signal)
        elif signal is None:
            return 'z'  # High impedance
        else:
            raise ValueError('Invalid signal type')

    def sig_connect(self, a, b):
        # Check both signals belongs to the current board instance
        # Convert signals to path names
        dest_path = self.__signal_to_path(a)
        src_path = self.__signal_to_path(b)

        if dest_path in self.mtxr_out:
            # Connect a module output to an IO output
            src_index = self.mtxr_in.index(src_path)
            dst_index = self.mtxr_out.index(dest_path)
            self.bus.write(self.__ADDR_MTXR_BASE + dst_index, src_index)
        elif dest_path in self.mtxl_out:
            # Connect a module input to an IO input (or 0 or 1).
            src_index = self.mtxl_in.index(src_path)
            dst_index = self.mtxl_out.index(dest_path)
            self.bus.write(self.__ADDR_MTXL_BASE + dst_index, src_index)
        else:
            # Shall never happen unless there is a bug
            raise RuntimeError(f'Invalid destination path \'{dest_path}\'')

    @property
    def timeout(self):
        """
        Timeout in seconds for read and write commands. If set to 0, timeout is
        disabled.
        """
        if self.__cache_timeout is None:
            return RuntimeError('Timeout not set yet')
        return self.__cache_timeout * self.__TIMEOUT_UNIT

    @timeout.setter
    def timeout(self, value):
        n = int(value / self.__TIMEOUT_UNIT)
        self.bus.set_timeout(n)  # May throw is n out of range.
        self.__cache_timeout = n  # Must be after set_timeout

    def push_timeout(self, value):
        """
        Save previous timeout setting in a stack, and set a new timeout value.
        Call to `pop_timeout` will restore previous timeout value.

        :param value: New timeout value, in seconds.
        """
        self.__timeout_stack.append(self.timeout)
        self.timeout = value

    def pop_timeout(self):
        """
        Restore timeout setting from stack.

        :raises RuntimeError: if timeout stack is already empty.
        """
        if len(self.__timeout_stack) == 0:
            raise RuntimeError('Timeout setting stack is empty')
        self.timeout = self.__timeout_stack.pop()

    def lazy_section(self):
        """
        :return: ScaffoldBusLazySection to be used with the python 'with'
            tatement to start and close a lazy update section.
        """
        return self.bus.lazy_section()
