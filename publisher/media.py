from __future__ import annotations

import hashlib
from io import BytesIO
import struct

from PIL import Image

from .storage import digest, encode


def content_digest(data, extension):
    if extension == ".png":
        with Image.open(BytesIO(data)) as image:
            info = {key: digest(value) if isinstance(value, bytes) else value for key, value in image.info.items()}
            header = encode({"format": "png-pixels", "mode": image.mode, "size": image.size, "info": info})
            pixels = image if image.mode == "I" or image.mode.startswith("I;16") else image.convert("RGBA")
            return digest(header + pixels.tobytes())
    if extension != ".ogg":
        raise ValueError("Unsupported media fingerprint")
    # https://xiph.org/ogg/doc/framing.html
    # https://xiph.org/vorbis/doc/Vorbis_I_spec.html
    result = hashlib.sha256(b"vorbis-packets\0")
    offset, packet_count, sequence = 0, 0, 0
    packet, serial, end = bytearray(), None, False
    while offset < len(data):
        page = data[offset:offset + 27]
        if len(page) != 27 or page[:5] != b"OggS\0" or end:
            raise ValueError("Invalid Ogg page")
        flags, granule, stream, number = struct.unpack_from("<BQII", page, 5)
        if serial is None:
            serial = stream
        if stream != serial or number != sequence or bool(flags & 1) != bool(packet):
            raise ValueError("Unsupported Ogg stream ordering")
        lengths = data[offset + 27:offset + 27 + page[26]]
        if len(lengths) != page[26] or offset + 27 + len(lengths) + sum(lengths) > len(data):
            raise ValueError("Truncated Ogg page")
        offset += 27 + len(lengths)
        for length in lengths:
            packet.extend(data[offset:offset + length])
            offset += length
            if length < 255:
                if packet_count < 3 and packet[:7] != bytes((1 + packet_count * 2,)) + b"vorbis":
                    raise ValueError("Invalid Vorbis headers")
                if packet_count == 1:
                    if len(packet) < 16:
                        raise ValueError("Truncated Vorbis comment")
                    vendor_size = struct.unpack_from("<I", packet, 7)[0]
                    if 11 + vendor_size + 5 > len(packet):
                        raise ValueError("Truncated Vorbis vendor")
                    del packet[7:11 + vendor_size]
                result.update(struct.pack("<I", len(packet)))
                result.update(packet)
                packet.clear()
                packet_count += 1
        if granule != 0xFFFFFFFFFFFFFFFF:
            result.update(struct.pack("<QQ", packet_count, granule))
        end = bool(flags & 4)
        sequence += 1
    if packet or packet_count <= 3 or not end:
        raise ValueError("Incomplete Vorbis stream")
    return result.hexdigest()
