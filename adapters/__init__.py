"""Channel adapters. Only email exists; every other channel is refused."""

from .base import ChannelAdapter, SendBlocked
from .smtp import SmtpAdapter

__all__ = ["ChannelAdapter", "SendBlocked", "SmtpAdapter", "adapter_for"]


class _UnavailableChannel(ChannelAdapter):
    name = "unavailable"

    def __init__(self, channel: str) -> None:
        self.channel = channel


def adapter_for(channel: str) -> ChannelAdapter:
    key = (channel or "").strip().lower()
    if key == "email":
        return SmtpAdapter()
    if key == "whatsapp":
        return _UnavailableChannel("whatsapp")
    raise ValueError(f"unsupported channel: {channel}")
