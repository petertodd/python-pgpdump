from datetime import datetime, timedelta
import hashlib
import re
from warnings import warn

from .utils import get_int2, get_int4, get_mpi, get_key_id


class Packet(object):
    '''The base packet object containing various fields pulled from the packet
    header as well as a slice of the packet data.'''
    def __init__(self, raw, name, new, data):
        self.raw = raw
        self.name = name
        self.new = new
        self.length = len(data)
        self.data = data

        # now let subclasses work their magic
        self.parse()

    def parse(self):
        '''Perform any parsing necessary to populate fields on this packet.
        This method is called as the last step in __init__(). The base class
        method is a no-op; subclasses should use this as required.'''
        pass

    def __repr__(self):
        new = "old"
        if self.new:
            new = "new"
        return "<%s: %s (%d), %s, length %d>" % (
                self.__class__.__name__, self.name, self.raw, new, self.length)


class AlgoLookup(object):
    '''Mixin class containing algorithm lookup methods.'''
    pub_algorithms = {
        1:  "RSA Encrypt or Sign",
        2:  "RSA Encrypt-Only",
        3:  "RSA Sign-Only",
        16: "ElGamal Encrypt-Only",
        17: "DSA Digital Signature Algorithm",
        18: "Elliptic Curve",
        19: "ECDSA",
        20: "Formerly ElGamal Encrypt or Sign",
        21: "Diffie-Hellman",
    }

    @classmethod
    def lookup_pub_algorithm(cls, alg):
        return cls.pub_algorithms.get(alg, "Unknown")

    hash_algorithms = {
        1:  "MD5",
        2:  "SHA1",
        3:  "RIPEMD160",
        8:  "SHA256",
        9:  "SHA384",
        10: "SHA512",
        11: "SHA224",
    }

    @classmethod
    def lookup_hash_algorithm(cls, alg):
        # reserved values check
        if alg in (4, 5, 6, 7):
            return "Reserved"
        return cls.hash_algorithms.get(alg, "Unknown")


class SignatureSubpacket(object):
    '''A signature subpacket containing a type, type name, some flags, and the
    contained data.'''
    CRITICAL_BIT = 0x80
    CRITICAL_MASK = 0x7f

    def __init__(self, raw, hashed, data):
        self.raw = raw
        self.subtype = raw & self.CRITICAL_MASK
        self.hashed = hashed
        self.critical = bool(raw & self.CRITICAL_BIT)
        self.length = len(data)
        self.data = data

    subpacket_types = {
        2:  "Signature Creation Time",
        3:  "Signature Expiration Time",
        4:  "Exportable Certification",
        5:  "Trust Signature",
        6:  "Regular Expression",
        7:  "Revocable",
        9:  "Key Expiration Time",
        10: "Placeholder for backward compatibility",
        11: "Preferred Symmetric Algorithms",
        12: "Revocation Key",
        16: "Issuer",
        20: "Notation Data",
        21: "Preferred Hash Algorithms",
        22: "Preferred Compression Algorithms",
        23: "Key Server Preferences",
        24: "Preferred Key Server",
        25: "Primary User ID",
        26: "Policy URI",
        27: "Key Flags",
        28: "Signer's User ID",
        29: "Reason for Revocation",
        30: "Features",
        31: "Signature Target",
        32: "Embedded Signature",
    }

    @property
    def name(self):
        if self.subtype in (0, 1, 8, 13, 14, 15, 17, 18, 19):
            return "Reserved"
        return self.subpacket_types.get(self.subtype, "Unknown")

    def __repr__(self):
        extra = ""
        if self.hashed:
            extra += "hashed, "
        if self.critical:
            extra += "critical, "
        return "<%s: %s, %slength %d>" % (
                self.__class__.__name__, self.name, extra, self.length)


class SignaturePacket(Packet, AlgoLookup):
    def __init__(self, *args, **kwargs):
        self.sig_version = None
        self.raw_sig_type = None
        self.raw_pub_algorithm = None
        self.raw_hash_algorithm = None
        self.raw_creation_time = None
        self.creation_time = None
        self.raw_expiration_time = None
        self.expiration_time = None
        self.key_id = None
        self.hash2 = None
        self.subpackets = []
        super(SignaturePacket, self).__init__(*args, **kwargs)

    def parse(self):
        self.sig_version = self.data[0]
        offset = 1
        if self.sig_version == 3:
            # 00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f
            # |  |  [  ctime  ] [ key_id                 ] |
            # |  |-type                           pub_algo-|
            # |-hash material
            # 10 11 12
            # |  [hash2]
            # |-hash_algo

            # "hash material" byte must be 0x05
            if self.data[offset] != 0x05:
                raise Exception("Invalid v3 signature packet")
            offset += 1

            self.raw_sig_type = self.data[offset]
            offset += 1

            self.raw_creation_time = get_int4(self.data, offset)
            self.creation_time = datetime.utcfromtimestamp(
                    self.raw_creation_time)
            offset += 4

            self.key_id = get_key_id(self.data, offset)
            offset += 8

            self.raw_pub_algorithm = self.data[offset]
            offset += 1

            self.raw_hash_algorithm = self.data[offset]
            offset += 1

            self.hash2 = self.data[offset:offset + 2]
            offset += 2

        elif self.sig_version == 4:
            # 00 01 02 03 ... <hashedsubpackets..> <subpackets..> [hash2]
            # |  |  |-hash_algo
            # |  |-pub_algo
            # |-type

            self.raw_sig_type = self.data[offset]
            offset += 1

            self.raw_pub_algorithm = self.data[offset]
            offset += 1

            self.raw_hash_algorithm = self.data[offset]
            offset += 1

            # next is hashed subpackets
            length = get_int2(self.data, offset)
            offset += 2
            self.parse_subpackets(offset, length, True)
            offset += length

            # followed by subpackets
            length = get_int2(self.data, offset)
            offset += 2
            self.parse_subpackets(offset, length, False)
            offset += length

            self.hash2 = self.data[offset:offset + 2]
            offset += 2

    def parse_subpackets(self, outer_offset, outer_length, hashed=False):
        offset = outer_offset
        while offset < outer_offset + outer_length:
            # each subpacket is [variable length] [subtype] [data]
            sub_offset, sub_len, sub_part = new_tag_length(self.data, offset)
            # sub_len includes the subtype single byte, knock that off
            sub_len -= 1
            # initial length bytes
            offset += 1 + sub_offset

            subtype = self.data[offset]
            offset += 1

            sub_data = self.data[offset:offset + sub_len]
            subpacket = SignatureSubpacket(subtype, hashed, sub_data)
            if subpacket.subtype == 2:
                self.raw_creation_time = get_int4(subpacket.data, 0)
                self.creation_time = datetime.utcfromtimestamp(
                        self.raw_creation_time)
            elif subpacket.subtype == 3:
                self.raw_expiration_time = get_int4(subpacket.data, 0)
            elif subpacket.subtype == 16:
                self.key_id = get_key_id(subpacket.data, 0)
            offset += sub_len
            self.subpackets.append(subpacket)

        if self.raw_expiration_time:
            self.expiration_time = self.creation_time + timedelta(
                    seconds=self.raw_expiration_time)

    @property
    def datetime(self):
        warn("deprecated, use creation_time", DeprecationWarning)
        return self.creation_time

    sig_types = {
        0x00: "Signature of a binary document",
        0x01: "Signature of a canonical text document",
        0x02: "Standalone signature",
        0x10: "Generic certification of a User ID and Public Key packet",
        0x11: "Persona certification of a User ID and Public Key packet",
        0x12: "Casual certification of a User ID and Public Key packet",
        0x13: "Positive certification of a User ID and Public Key packet",
        0x18: "Subkey Binding Signature",
        0x19: "Primary Key Binding Signature",
        0x1f: "Signature directly on a key",
        0x20: "Key revocation signature",
        0x28: "Subkey revocation signature",
        0x30: "Certification revocation signature",
        0x40: "Timestamp signature",
        0x50: "Third-Party Confirmation signature",
    }

    @property
    def sig_type(self):
        return self.sig_types.get(self.raw_sig_type, "Unknown")

    @property
    def pub_algorithm(self):
        return self.lookup_pub_algorithm(self.raw_pub_algorithm)

    @property
    def hash_algorithm(self):
        return self.lookup_hash_algorithm(self.raw_hash_algorithm)

    def __repr__(self):
        return "<%s: %s, %s, length %d>" % (
                self.__class__.__name__, self.pub_algorithm,
                self.hash_algorithm, self.length)


class PublicKeyPacket(Packet, AlgoLookup):
    def __init__(self, *args, **kwargs):
        self.pubkey_version = None
        self.fingerprint = None
        self.key_id = None
        self.raw_creation_time = None
        self.creation_time = None
        self.raw_pub_algorithm = None
        self.pub_algorithm_type = None
        self.modulus = None
        self.exponent = None
        self.prime = None
        self.group_order = None
        self.group_gen = None
        self.key_value = None
        super(PublicKeyPacket, self).__init__(*args, **kwargs)

    def parse(self):
        self.pubkey_version = self.data[0]
        offset = 1
        if self.pubkey_version == 4:
            sha1 = hashlib.sha1()
            seed_bytes = (0x99, (self.length >> 8) & 0xff, self.length & 0xff)
            sha1.update(bytearray(seed_bytes))
            sha1.update(self.data)
            self.fingerprint = sha1.hexdigest().upper().encode('ascii')
            self.key_id = self.fingerprint[24:]

            self.raw_creation_time = get_int4(self.data, offset)
            self.creation_time = datetime.utcfromtimestamp(
                    self.raw_creation_time)
            offset += 4

            self.raw_pub_algorithm = self.data[offset]
            offset += 1

            if self.raw_pub_algorithm in (1, 2, 3):
                self.pub_algorithm_type = "rsa"
                # n, e
                self.modulus, offset = get_mpi(self.data, offset)
                self.exponent, offset = get_mpi(self.data, offset)
            elif self.raw_pub_algorithm == 17:
                self.pub_algorithm_type = "dsa"
                # p, q, g, y
                self.prime, offset = get_mpi(self.data, offset)
                self.group_order, offset = get_mpi(self.data, offset)
                self.group_gen, offset = get_mpi(self.data, offset)
                self.key_value, offset = get_mpi(self.data, offset)
            elif self.raw_pub_algorithm == 16:
                self.pub_algorithm_type = "elgamal"
                # p, g, y
                self.prime, offset = get_mpi(self.data, offset)
                self.group_gen, offset = get_mpi(self.data, offset)
                self.key_value, offset = get_mpi(self.data, offset)

    @property
    def datetime(self):
        warn("deprecated, use creation_time", DeprecationWarning)
        return self.creation_time

    @property
    def pub_algorithm(self):
        return self.lookup_pub_algorithm(self.raw_pub_algorithm)

    def __repr__(self):
        return "<%s: 0x%s, %s, length %d>" % (
                self.__class__.__name__, self.key_id.decode('ascii'),
                self.pub_algorithm, self.length)


class PublicSubkeyPacket(PublicKeyPacket):
    '''A Public-Subkey packet (tag 14) has exactly the same format as a
    Public-Key packet, but denotes a subkey.'''
    pass


class UserIDPacket(Packet):
    '''A User ID packet consists of UTF-8 text that is intended to represent
    the name and email address of the key holder. By convention, it includes an
    RFC 2822 mail name-addr, but there are no restrictions on its content.'''
    def __init__(self, *args, **kwargs):
        self.user = None
        self.user_name = None
        self.user_email = None
        super(UserIDPacket, self).__init__(*args, **kwargs)

    def parse(self):
        self.user = self.data.decode('utf8')
        matches = re.match(r'^([^<]+)? ?<([^>]*)>?', self.user)
        if matches:
            self.user_name = matches.group(1).strip()
            self.user_email = matches.group(2).strip()

    def __repr__(self):
        return "<%s: %r (%r), length %d>" % (
                self.__class__.__name__, self.user_name, self.user_email,
                self.length)


class UserAttributePacket(Packet):
    def __init__(self, *args, **kwargs):
        self.raw_image_format = None
        self.image_format = None
        self.image_data = None
        super(UserAttributePacket, self).__init__(*args, **kwargs)

    def parse(self):
        offset = sub_offset = sub_len = 0
        while offset + sub_len < len(self.data):
            # each subpacket is [variable length] [subtype] [data]
            sub_offset, sub_len, sub_part = new_tag_length(self.data, offset)
            # sub_len includes the subtype single byte, knock that off
            sub_len -= 1
            # initial length bytes
            offset += 1 + sub_offset

            sub_type = self.data[offset]
            offset += 1

            # there is only one currently known type- images (1)
            if sub_type == 1:
                # the only little-endian encoded value in OpenPGP
                hdr_size = self.data[offset] + (self.data[offset + 1] << 8)
                hdr_version = self.data[offset + 2]
                self.raw_image_format = self.data[offset + 3]
                offset += hdr_size

                self.image_data = self.data[offset:]
                if self.raw_image_format == 1:
                    self.image_format = "jpeg"
                else:
                    self.image_format = "unknown"


class TrustPacket(Packet):
    def __init__(self, *args, **kwargs):
        self.trust = None
        super(TrustPacket, self).__init__(*args, **kwargs)

    def parse(self):
        '''GnuPG public keyrings use a 2-byte trust value that appears to be
        integer values into some internal enumeration.'''
        if self.length == 2:
            self.trust = get_int2(self.data, 0)


TAG_TYPES = {
    # (Name, PacketType) tuples
    0:  ("Reserved", None),
    1:  ("Public-Key Encrypted Session Key Packet", None),
    2:  ("Signature Packet", SignaturePacket),
    3:  ("Symmetric-Key Encrypted Session Key Packet", None),
    4:  ("One-Pass Signature Packet", None),
    5:  ("Secret Key Packet", None),
    6:  ("Public Key Packet", PublicKeyPacket),
    7:  ("Secret Subkey Packet", None),
    8:  ("Compressed Data Packet", None),
    9:  ("Symmetrically Encrypted Data Packet", None),
    10: ("Marker Packet", None),
    11: ("Literal Data Packet", None),
    12: ("Trust Packet", TrustPacket),
    13: ("User ID Packet", UserIDPacket),
    14: ("Public Subkey Packet", PublicSubkeyPacket),
    17: ("User Attribute Packet", UserAttributePacket),
    18: ("Symmetrically Encrypted and MDC Packet", None),
    19: ("Modification Detection Code Packet", None),
    60: ("Private", None),
    61: ("Private", None),
    62: ("Private", None),
    63: ("Private", None),
}


def new_tag_length(data, start):
    '''Takes a bytearray of data as input, as well as an offset of where to
    look. Returns a derived (offset, length, partial) tuple.'''
    first = data[start]
    offset = length = 0
    partial = False

    if first < 192:
        length = first
    elif first < 224:
        offset = 1
        length = ((first - 192) << 8) + data[start + 1] + 192
    elif first == 255:
        offset = 4
        length = get_int4(data, start + 1)
    else:
        # partial length, 224 <= l < 255
        length = 1 << (first & 0x1f)
        partial = True

    return (offset, length, partial)


def old_tag_length(data, start):
    '''Takes a bytearray of data as input, as well as an offset of where to
    look. Returns a derived (offset, length) tuple.'''
    offset = length = 0
    temp_len = data[start] & 0x03

    if temp_len == 0:
        offset = 1
        length = data[start + 1]
    elif temp_len == 1:
        offset = 2
        length = get_int2(data, start + 1)
    elif temp_len == 2:
        offset = 4
        length = get_int4(data, start + 1)
    elif temp_len == 3:
        length = len(data) - start - 1

    return (offset, length)


def construct_packet(data, start):
    '''Returns a (length, packet) tuple constructed from 'data' at index
    'start'. If there is a next packet, it will be found at start + length.'''
    tag = data[start] & 0x3f
    new = bool(data[start] & 0x40)
    if new:
        data_offset, length, partial = new_tag_length(data, start + 1)
        data_offset += 1
    else:
        tag >>= 2
        data_offset, length = old_tag_length(data, start)
        partial = False
    data_offset += 1
    start += data_offset
    name, PacketType = TAG_TYPES.get(tag, ("Unknown", None))
    end = start + length
    packet_data = data[start:end]
    if not PacketType:
        PacketType = Packet
    packet = PacketType(tag, name, new, packet_data)
    return (data_offset + length, packet)
