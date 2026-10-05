# CRI format references: vgmstream/vgmstream, audited source snapshot
# e6afeaacf433bfafd38d873f80c94517e09d5b96: src/util/cri_utf.c,
# src/meta/awb.c, and src/coding/libs/clhca.c.
# Python implementation is limited to metadata and adds ACB/HCA consistency checks.
# See THIRD_PARTY_NOTICES.md for source roles and LICENSES/vgmstream-COPYING.txt
# for the complete upstream copyright, permission, and disclaimer notices.
"""Read CRI's public binary metadata; no playback, codec, or audio decryption is needed."""
import struct


def utf_rows(data):
    def u16(offset):
        return struct.unpack_from(">H", data, offset)[0]

    def u32(offset):
        return struct.unpack_from(">I", data, offset)[0]

    if len(data) < 32 or data[:4] != b"@UTF" or u32(4) + 8 != len(data):
        raise ValueError("Invalid CRI UTF table")
    row_offset, strings_offset, binary_offset = u16(10) + 8, u32(12) + 8, u32(16) + 8
    columns, width, count = u16(24), u16(26), u32(28)
    if not 32 <= row_offset <= strings_offset <= binary_offset <= len(data) or row_offset + width * count > strings_offset:
        raise ValueError("Invalid CRI UTF offsets")

    def text(offset):
        start = strings_offset + offset
        end = data.find(b"\0", start, binary_offset)
        if not strings_offset <= start <= end < binary_offset:
            raise ValueError("Invalid CRI UTF string")
        return data[start:end].decode("utf-8")

    formats = {0: ">B", 1: ">b", 2: ">H", 3: ">h", 4: ">I", 5: ">i", 6: ">Q", 7: ">q", 8: ">f", 9: ">d", 10: ">I", 11: ">II", 12: ">QQ"}

    def value(kind, position):
        values = struct.unpack_from(formats[kind], data, position)
        if kind == 10:
            return text(values[0])
        if kind == 11:
            offset, size = values
            start = binary_offset + offset
            if start < binary_offset or start + size > len(data):
                raise ValueError("Invalid CRI UTF binary reference")
            return data[start:start + size]
        return values[0] if len(values) == 1 else values

    schema, position, row_position = [], 32, 0
    for _ in range(columns):
        flag = data[position]
        name, kind = text(u32(position + 1)), flag & 15
        position += 5
        if kind not in formats:
            raise ValueError("Unknown CRI UTF value type")
        size = struct.calcsize(formats[kind])
        storage = flag & 0xF0
        if storage == 0x30:
            schema.append((name, kind, False, value(kind, position)))
            position += size
        elif storage == 0x50:
            schema.append((name, kind, True, row_position))
            row_position += size
        elif storage == 0x10:
            schema.append((name, kind, False, b"" if kind == 11 else 0))
        else:
            raise ValueError("Unknown CRI UTF column storage")
    # CRI HCA v3 single-row headers place three trailing zero columns after row_width.
    # Respect the complete bounded row section, matching the format's observed layout.
    allowed_row_width = strings_offset - row_offset if count == 1 else width
    if position > row_offset or row_position > allowed_row_width:
        raise ValueError("Invalid CRI UTF schema size")
    return [{name: value(kind, row_offset + row * width + entry) if per_row else entry for name, kind, per_row, entry in schema} for row in range(count)]


def acb_duration(data):
    headers = utf_rows(data)
    if len(headers) != 1:
        raise ValueError("Music ACB must have a single header")
    waveforms = utf_rows(headers[0]["WaveformTable"])
    if len(waveforms) != 1:
        raise ValueError("Music ACB contains multiple waveforms; duration needs an explicit cue mapping")
    waveform = waveforms[0]
    samples, rate = waveform["NumSamples"], waveform["SamplingRate"]
    if not isinstance(samples, int) or not isinstance(rate, int) or samples <= 0 or rate <= 0:
        raise ValueError("Invalid music sample count/rate")
    bank = headers[0]["AwbFile"]
    if len(bank) < 16 or bank[:4] != b"AFS2":
        raise ValueError("Music ACB has no embedded AFS2 waveform")
    offset_size, id_size = bank[5], int.from_bytes(bank[6:8], "little")
    count, alignment = int.from_bytes(bank[8:12], "little"), int.from_bytes(bank[12:14], "little")
    if count != 1 or offset_size not in (2, 4) or id_size not in (2, 4) or not alignment:
        raise ValueError("Unsupported music AFS2 layout")
    table = 16 + id_size
    start = int.from_bytes(bank[table:table + offset_size], "little")
    end = int.from_bytes(bank[table + offset_size:table + offset_size * 2], "little")
    start = (start + alignment - 1) // alignment * alignment
    if not 16 <= start < end <= len(bank):
        raise ValueError("Invalid music AFS2 waveform bounds")
    hca = bank[start:end]
    mask = lambda raw: bytes(c & 0x7F for c in raw)
    if len(hca) < 30 or mask(hca[:4]) != b"HCA\0" or mask(hca[8:12]) != b"fmt\0" or mask(hca[24:28]) not in (b"comp", b"dec\0"):
        raise ValueError("Unsupported music waveform codec/header")
    hca_rate = int.from_bytes(hca[13:16], "big")
    frames = int.from_bytes(hca[16:20], "big")
    delay, padding = int.from_bytes(hca[20:22], "big"), int.from_bytes(hca[22:24], "big")
    header_size, frame_size = int.from_bytes(hca[6:8], "big"), int.from_bytes(hca[28:30], "big")
    if hca_rate != rate or frames * 1024 - delay - padding != samples:
        raise ValueError("ACB sample count disagrees with HCA duration")
    if header_size + frames * frame_size != len(hca):
        raise ValueError("Music HCA is incomplete or has an unsupported frame layout")
    return round(samples / rate, 6)
