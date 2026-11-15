"""audiosync - sample-locked audio from any device to any other on the LAN.

Only PCM/FLAC audio crosses the network; the control plane is Snapcast's
JSON-RPC over HTTP plus a small mDNS peer announcement.
"""

__version__ = "1.0.1"
