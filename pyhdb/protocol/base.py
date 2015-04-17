# Copyright 2014 SAP SE
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import io

import struct
import logging
from io import BytesIO
from weakref import WeakValueDictionary

from pyhdb.exceptions import InterfaceError
from pyhdb._compat import with_metaclass
from pyhdb.protocol.constants import part_kinds

MAX_MESSAGE_SIZE = 2**17
MESSAGE_HEADER_SIZE = 32

MAX_SEGMENT_SIZE = MAX_MESSAGE_SIZE - MESSAGE_HEADER_SIZE
SEGMENT_HEADER_SIZE = 24

PART_HEADER_SIZE = 16

recv_log = logging.getLogger('receive')
debug = recv_log.debug


class Message(object):

    # Documentation Notation:
    # I8 I4 UI4 UI4 I2 I1 B[9]
    header_struct = struct.Struct('qiIIhb9B')

    _session_id = None
    _packet_count = None

    def __init__(self, connection, segments=None):
        self.connection = connection

        if segments is None:
            self.segments = []
        elif isinstance(segments, (list, tuple)):
            self.segments = segments
        else:
            self.segments = [segments]

    @property
    def session_id(self):
        """
        Identifer for session.
        """
        if self._session_id is not None:
            return self._session_id
        return self.connection.session_id

    @property
    def packet_count(self):
        """
        Sequence number for message inside of session.
        """
        if self._packet_count is None:
            self._packet_count = self.connection.get_next_packet_count()
        return self._packet_count

    def build_payload(self, payload):
        """ Build payload of message. """
        for segment in self.segments:
            segment.pack(payload, commit=self.connection.autocommit)

    def pack(self):
        """ Pack message to binary stream. """
        payload = io.BytesIO()
        # Advance num bytes equal to header size - the header is written later
        # after the payload of all segments and parts has been written:
        msg_header_size = self.header_struct.size
        payload.seek(msg_header_size, io.SEEK_CUR)

        # Write out payload of segments and parts:
        self.build_payload(payload)

        packet_length = len(payload.getvalue()) - msg_header_size
        total_space = MAX_MESSAGE_SIZE - MESSAGE_HEADER_SIZE
        count_of_segments = len(self.segments)

        header = self.header_struct.pack(
            self.session_id,
            self.packet_count,
            packet_length,
            total_space,
            count_of_segments,
            *[0] * 10    # Reserved
        )
        # Go back to begining of payload for writing message header:
        payload.seek(0)
        payload.write(header)
        payload.seek(0, io.SEEK_END)
        return payload

    def send(self):
        """
        Send message over connection and returns the reply message.
        """
        payload = self.pack()
        # from pyhdb.lib.stringlib import humanhexlify
        # print humanhexlify(payload.getvalue())
        return self.connection._send_message(payload.getvalue())

    @classmethod
    def unpack_reply(cls, connection, header, payload):
        """
        Takes already unpacked header and binary payload of received
        reply and creates Message object.
        """
        reply = Message(
            connection,
            tuple(ReplySegment.unpack_from(
                payload, expected_segments=header[4]
            ))
        )
        reply._session_id = header[0]
        reply._packet_count = header[1]
        return reply


class BaseSegment(object):

    # I4 I4 I2 I2 I1
    header_struct = struct.Struct('<iihhb')
    segment_kind = None

    def __init__(self, parts=None):
        if parts is None:
            self.parts = []
        elif isinstance(parts, (list, tuple)):
            self.parts = parts
        else:
            self.parts = [parts]

    @property
    def offset(self):
        return 0

    @property
    def number(self):
        return 1

    @property
    def header_size(self):
        return self.header_struct.size

    def build_payload(self, payload):
        """Build payload of all parts and write them into the payload buffer"""
        remaining_size = MAX_SEGMENT_SIZE - SEGMENT_HEADER_SIZE

        for part in self.parts:
            part_payload = part.pack(remaining_size)
            payload.write(part_payload)
            remaining_size -= len(part_payload)

    def pack(self, payload, **kwargs):

        # remember position in payload object:
        payload_pos = payload.tell()

        # Advance num bytes equal to header size. The header is written later
        # after the payload of all segments and parts has been written:
        payload.seek(self.header_size, io.SEEK_CUR)

        # Write out payload of parts:
        self.build_payload(payload)
        payload_length = payload.tell() - payload_pos  # calc length of parts payload

        header = self.header_struct.pack(
            payload_length,
            self.offset,
            len(self.parts),
            self.number,
            self.segment_kind
        ) + self.pack_additional_header(**kwargs)

        # Go back to beginning of payload header for writing segment header:
        payload.seek(payload_pos)
        payload.write(header)
        # Put file pointer at the end of the bffer so that next segment can be appended:
        payload.seek(0, io.SEEK_END)

    def pack_additional_header(self, **kwargs):
        raise NotImplemented


class RequestSegment(BaseSegment):

    segment_kind = 1
    # I1 I1 I1 B[8]
    request_header_struct = struct.Struct('bbb8x')

    def __init__(self, message_type, parts=None):
        super(RequestSegment, self).__init__(parts)
        self.message_type = message_type

    @property
    def header_size(self):
        return self.header_struct.size + self.request_header_struct.size

    @property
    def command_options(self):
        return 0

    def pack_additional_header(self, **kwargs):
        return self.request_header_struct.pack(
            self.message_type,
            int(kwargs.get('commit', 0)),
            self.command_options
        )


class ReplySegment(BaseSegment):

    segment_kind = 2
    # I1 I2 B[8]
    reply_header_struct = struct.Struct('<bh8B')

    def __init__(self, function_code, parts=None):
        super(ReplySegment, self).__init__(parts)
        self.function_code = function_code

    @property
    def header_size(self):
        return self.header_struct.size + self.reply_header_struct.size

    @classmethod
    def unpack_from(cls, payload, expected_segments):
        num_segments = 0

        while num_segments < expected_segments:
            try:
                base_segment_header = cls.header_struct.unpack(
                    payload.read(13)
                )
            except struct.error:
                raise Exception("No valid segment header")

            # Read additional header fields
            try:
                segment_header = \
                    base_segment_header + cls.reply_header_struct.unpack(
                        payload.read(11)
                    )
            except struct.error:
                raise Exception("No valid reply segment header")

            msg = 'Segment Header (%d/%d, 24 bytes): segmentlength: %d, ' \
                  'segmentofs: %d, noofparts: %d, segmentno: %d, rserved: %d,' \
                  ' segmentkind: %d, functioncode: %d'
            debug(msg, num_segments+1, expected_segments, *segment_header[:7])
            if expected_segments == 1:
                # If we just expects one segment than we can take the full
                # payload. This also a workaround of an internal bug.
                segment_payload_size = -1
            else:
                segment_payload_size = segment_header[0] - SEGMENT_HEADER_SIZE

            # Determinate segment payload
            pl = payload.read(segment_payload_size)
            segment_payload = BytesIO(pl)
            debug('Read %d bytes payload segment %d', len(pl), num_segments + 1)

            num_segments += 1

            if base_segment_header[4] == 2:  # Reply segment
                yield ReplySegment.unpack(segment_header, segment_payload)
            elif base_segment_header[4] == 5:  # Error segment
                error = ReplySegment.unpack(segment_header, segment_payload)
                if error.parts[0].kind == part_kinds.ROWSAFFECTED:
                    raise Exception("Rows affected %s" % (error.parts[0].values,))
                elif error.parts[0].kind == part_kinds.ERROR:
                    raise error.parts[0].errors[0]
            else:
                raise Exception("Invalid reply segment")

    @classmethod
    def unpack(cls, header, payload):
        """
        Takes unpacked header and payload of segment and
        create ReplySegment object.
        """

        return cls(
            header[6],
            tuple(Part.unpack_from(payload, expected_parts=header[2]))
        )


part_mapping = WeakValueDictionary()


class PartMeta(type):
    """
    Meta class for part classes which also add them into part_mapping.
    """

    def __new__(mcs, name, bases, attrs):
        part_class = super(PartMeta, mcs).__new__(mcs, name, bases, attrs)
        if part_class.kind:
            if not -128 <= part_class.kind <= 127:
                raise InterfaceError("%s part kind must be between -128 and 127" % part_class.__name__)
            # Register new part class is registry dictionary for later lookup:
            part_mapping[part_class.kind] = part_class
        return part_class


class Part(with_metaclass(PartMeta, object)):

    header_struct = struct.Struct('<bbhiii')
    attribute = 0
    kind = None
    bigargumentcount = 0  # what is this useful for? Seems to be always zero ...

    # Attribute to get source of part
    source = 'client'

    def pack(self, remaining_size):
        """Pack data of part into binary format"""
        arguments_count, payload = self.pack_data()
        payload_length = len(payload)

        # align payload length to multiple of 8
        if payload_length % 8 != 0:
            payload += b"\x00" * (8 - payload_length % 8)

        return self.header_struct.pack(self.kind, self.attribute, arguments_count, self.bigargumentcount,
                                       payload_length, remaining_size) + payload

    def pack_data(self):
        raise NotImplemented()

    @classmethod
    def unpack_from(cls, payload, expected_parts):
        """Unpack parts from payload"""
        num_parts = 0

        while expected_parts > num_parts:
            try:
                part_header = cls.header_struct.unpack(
                    payload.read(16)
                )
            except struct.error:
                raise InterfaceError("No valid part header")

            if part_header[4] % 8 != 0:
                part_payload_size = part_header[4] + 8 - (part_header[4] % 8)
            else:
                part_payload_size = part_header[4]
            part_payload = BytesIO(payload.read(part_payload_size))

            try:
                _PartClass = part_mapping[part_header[0]]
            except KeyError:
                raise InterfaceError(
                    "Unknown part kind %s" % part_header[0]
                )

            msg = 'Part Header (%d/%d, 16 bytes): partkind: %s(%d), ' \
                  'partattributes: %d, argumentcount: %d, bigargumentcount: %d'\
                  ', bufferlength: %d, buffersize: %d'
            debug(msg, num_parts+1, expected_parts, _PartClass.__name__,
                  *part_header[:6])
            debug('Read %d bytes payload for part %d',
                  part_payload_size, num_parts + 1)
            init_arguments = _PartClass.unpack_data(
                part_header[2], part_payload
            )
            debug('Part data: %s', init_arguments)
            part = _PartClass(*init_arguments)
            part.attribute = part_header[1]
            part.source = 'server'

            num_parts += 1
            yield part
