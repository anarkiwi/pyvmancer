"""Shell transport that drives the CDC-ACM interface directly over libusb.

Useful where ``/dev/tty*`` is not mapped into the environment (containers) but
``/dev/bus/usb`` is: the kernel ``cdc_acm`` driver is detached and the bulk
endpoints are used directly. Needs read-write access to the usbfs node.
"""

import time

from ..const import USB_PID, USB_VID
from ..errors import DeviceNotFoundError, TransportError, TransportUnavailableError
from .base import ByteTransport

CDC_COMM_CLASS = 0x02
CDC_DATA_CLASS = 0x0A
SET_CONTROL_LINE_STATE = 0x22
LINE_STATE_DTR_RTS = 0x03
_REQ_TYPE_CLASS_INTERFACE = 0x21


class UsbCdcTransport(ByteTransport):
    """Byte stream over the device's CDC-ACM bulk endpoints via :mod:`pyusb`."""

    def __init__(self, vid=USB_VID, pid=USB_PID, serial_number=None, detach_kernel_driver=True):
        try:
            import usb.core
            import usb.util
        except ImportError as err:
            raise TransportUnavailableError("pyusb is not installed") from err
        self._util = usb.util
        matcher = {"idVendor": vid, "idProduct": pid}
        if serial_number is not None:
            matcher["serial_number"] = serial_number
        try:
            device = usb.core.find(**matcher)
        except Exception as err:
            raise TransportUnavailableError(f"libusb backend unavailable: {err}") from err
        if device is None:
            raise DeviceNotFoundError(f"no USB device matching {vid:04x}:{pid:04x}")
        self._device = device
        self._claimed = None
        self._open(detach_kernel_driver)

    def _open(self, detach_kernel_driver):
        """Locate the CDC data interface, detach the kernel driver and claim it."""
        config = self._device.get_active_configuration()
        data_iface = self._find_interface(config, CDC_DATA_CLASS)
        if data_iface is None:
            raise TransportError("device exposes no CDC data interface")
        comm_iface = self._find_interface(config, CDC_COMM_CLASS)
        for iface in (comm_iface, data_iface):
            if iface is not None and detach_kernel_driver:
                self._detach(iface.bInterfaceNumber)
        try:
            self._util.claim_interface(self._device, data_iface.bInterfaceNumber)
        except Exception as err:
            raise TransportUnavailableError(f"cannot claim CDC data interface: {err}") from err
        self._claimed = data_iface.bInterfaceNumber
        self._out_ep = self._endpoint(data_iface, self._util.ENDPOINT_OUT)
        self._in_ep = self._endpoint(data_iface, self._util.ENDPOINT_IN)
        if self._out_ep is None or self._in_ep is None:
            raise TransportError("CDC data interface is missing bulk endpoints")
        if comm_iface is not None:
            self._assert_dtr(comm_iface.bInterfaceNumber)

    @staticmethod
    def _find_interface(config, cls):
        """Return the first interface with the given bInterfaceClass."""
        for iface in config:
            if iface.bInterfaceClass == cls:
                return iface
        return None

    def _endpoint(self, iface, direction):
        """Return the first bulk endpoint on ``iface`` in ``direction``."""
        for endpoint in iface:
            if self._util.endpoint_direction(endpoint.bEndpointAddress) == direction:
                return endpoint
        return None

    def _detach(self, number):
        """Detach the kernel driver from an interface if one is attached."""
        try:
            if self._device.is_kernel_driver_active(number):
                self._device.detach_kernel_driver(number)
        except Exception:
            pass

    def _assert_dtr(self, comm_number):
        """Raise DTR/RTS so the device starts emitting shell output."""
        try:
            self._device.ctrl_transfer(
                _REQ_TYPE_CLASS_INTERFACE, SET_CONTROL_LINE_STATE, LINE_STATE_DTR_RTS, comm_number, None
            )
        except Exception:
            pass

    @property
    def name(self):
        """Bus/address identifier of the claimed USB device."""
        return f"usb:{self._device.bus:03d}:{self._device.address:03d}"

    def write(self, data):
        """Write bytes to the bulk OUT endpoint."""
        if self._claimed is None:
            raise TransportError("transport is closed")
        try:
            self._out_ep.write(bytes(data), timeout=2000)
        except Exception as err:
            raise TransportError(f"USB bulk write failed: {err}") from err

    def read(self, timeout=0.0):
        """Read from the bulk IN endpoint, blocking up to ``timeout`` seconds."""
        if self._claimed is None:
            raise TransportError("transport is closed")
        deadline = time.monotonic() + max(0.0, timeout)
        out = bytearray()
        while True:
            chunk = self._read_once()
            if chunk:
                out.extend(chunk)
                continue
            if out or time.monotonic() >= deadline:
                return bytes(out)
            time.sleep(0.001)

    def _read_once(self):
        """Attempt one non-blocking bulk read, returning b"" on timeout."""
        import usb.core

        try:
            return bytes(self._in_ep.read(self._in_ep.wMaxPacketSize, timeout=10))
        except usb.core.USBTimeoutError:
            return b""
        except usb.core.USBError as err:
            if err.errno in (110, None):
                return b""
            raise TransportError(f"USB bulk read failed: {err}") from err

    def close(self):
        """Release the claimed interface and free the device handle."""
        if self._claimed is not None:
            try:
                self._util.release_interface(self._device, self._claimed)
            except Exception:
                pass
            self._claimed = None
        try:
            self._util.dispose_resources(self._device)
        except Exception:
            pass
