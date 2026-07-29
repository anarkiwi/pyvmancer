"""Transport interfaces shared by the MIDI and serial backends."""

import abc


class MidiTransport(abc.ABC):
    """A bidirectional stream of raw MIDI bytes."""

    @abc.abstractmethod
    def send(self, data):
        """Write raw MIDI bytes to the device."""

    @abc.abstractmethod
    def receive(self, timeout=0.0):
        """Read available MIDI bytes, waiting up to ``timeout`` seconds."""

    @abc.abstractmethod
    def close(self):
        """Release the underlying resource."""

    @property
    def name(self):
        """Human-readable identifier for this transport instance."""
        return type(self).__name__

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class ByteTransport(abc.ABC):
    """A bidirectional byte stream carrying the newline-delimited shell protocol."""

    @abc.abstractmethod
    def write(self, data):
        """Write raw bytes to the device."""

    @abc.abstractmethod
    def read(self, timeout=0.0):
        """Read available bytes, waiting up to ``timeout`` seconds."""

    @abc.abstractmethod
    def close(self):
        """Release the underlying resource."""

    @property
    def name(self):
        """Human-readable identifier for this transport instance."""
        return type(self).__name__

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
