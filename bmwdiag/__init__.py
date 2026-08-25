"""
bmwdiag - vehicle diagnostic mapping subsystem.

The mapping layer turns versioned data files into normalised telemetry
signals. It knows nothing about sockets, HSFZ, SQLite or HTTP; the
application wires a transport into it.
"""

__all__ = ["mapping", "protocol", "obd"]
