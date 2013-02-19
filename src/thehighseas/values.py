from base64 import b64encode, b64decode
import hashlib
import time
import struct

from bencode import bencode, bdecode
from bottle import request
import simplejson
import ipaddr

from constants import redis_connection, announce_url

class NoInfoException(Exception):
    pass

class NonIPv4AddressException(Exception):
    pass

class Swarm(object):
    def number_of_seeds(self):
        return sum(1 for p in redis_connection.hvals(self.info_hash + ".swarm")
                     if Peer.from_json(p).is_complete())

    def number_of_leechers(self):
        return sum(1 for p in redis_connection.hvals(self.info_hash + ".swarm")
                     if not Peer.from_json(p).is_complete())

    def times_downloaded(self):
        times = redis_connection.hget("completions", self.info_hash)
        if times is not None:
            return times
        else:
            return 0

    def name(self):
        try:
            return bdecode(redis_connection.get(self.info_hash + ".info"))["name"]
        except TypeError:
            return "-"

    def completed_by(self, peer):
        redis_connection.hincrby("completions", self.info_hash, 1)
        self.update(peer)

    def stopped_by(self, peer):
        redis_connection.hdel(self.info_hash + ".swarm", peer.peer_id)

    def update(self, peer):
        redis_connection.hset(self.info_hash + ".swarm", peer.peer_id, peer.to_json())

    def to_metainfo(self):
        try:
            info_as_dict = bdecode(redis_connection.get(self.info_hash + ".info"))
            return bencode({"announce": announce_url,
                            "info": info_as_dict })
        except TypeError:
            raise NoInfoException()

    def peers(self):
        values = redis_connection.hvals(self.info_hash + ".swarm")
        return [Peer.from_json(e).to_dict() for e in values]

    def _save_info_(self, info_as_dict):
        bencoded_info_dict = bencode(info_as_dict)
        h = hashlib.new("sha1")
        h.update(bencoded_info_dict)
        self.info_hash = h.hexdigest()
        print("info_hash = " + self.info_hash)
        redis_connection.set(self.info_hash + ".info", bencoded_info_dict)

    def _ensure_recorded_(self):
        redis_connection.sadd("hashes", self.info_hash)

    def is_secret(self):
        return redis_connection.get(self.info_hash + ".info") is None

    def stats(self):
        s = {"downloaded": self.times_downloaded(),
             "complete": self.number_of_seeds(),
             "incomplete": self.number_of_leechers()}
        if not self.is_secret():
            s["name"] = self.name()
        return s

    def listing(self, number_of_peers=None, compact=False):
        s = {"peers": self.peers()}
        s.update(self.stats())
        return s

    @classmethod
    def all(cls):
        return (cls.from_hex_hash(hash)
                for hash in redis_connection.smembers("hashes"))

    @classmethod
    def nonsecret(cls):
        for hexhash in redis_connection.smembers("hashes"):
            swarm = cls.from_hex_hash(hexhash)
            if not swarm.is_secret():
                yield swarm

    @classmethod
    def from_hex_hash(cls, hex_hash):
        s = Swarm()
        s.info_hash = hex_hash
        return s

    @classmethod
    def from_metainfo_file(cls, metainfo_file, filename):
        metainfo = bdecode(metainfo_file.read())
        s = Swarm()
        s._save_info_(metainfo["info"])
        s._ensure_recorded_()
        redis_connection.save()
        return s

    @classmethod
    def from_announcement(cls, announcement):
        s = Swarm()
        s.info_hash = announcement["info_hash"].encode("hex")
        s._ensure_recorded_()
        return s

class Clock(object):
    """A facade for Python's date and time libraries.  This is present
    to save having to monkeypatch things in tests."""
    def now(self):
        return int(time.time())

_clock_ = Clock()

class Peer(object):
    """A peer (in a swarm)."""
    def __eq__(self, other):
        return self.peer_id == other.peer_id

    def is_complete(self):
        """Return True if the peer has completed the fileset, False
        otherwise.

        """
        return self.left == 0

    def to_json(self):
        return simplejson.dumps(
            {"peer_id": b64encode(self.peer_id),
             "ip": self.ip.exploded,
             "port": self.port,
             "last_seen": self.last_seen,
             "uploaded": self.uploaded,
             "downloaded": self.downloaded,
             "left": self.left},
            separators=(',',':'))

    def to_dict(self):
        """Return a dict for bencoding in a non-compact listing"""
        return {"peer_id": self.peer_id,
                "ip": self.ip.exploded,
                "port": self.port}

    def to_binary(self):
        """Return binary for a compact listing"""
        if self.ip.version != 4:
            raise NonIPv4AddressException()
        return self.ip.packed + struct.pack("!H", self.port)

    @classmethod
    def from_json(cls, json_string):
        dict_ = simplejson.loads(json_string)
        peer = cls()
        peer.peer_id = b64decode(dict_["peer_id"])
        peer.ip = ipaddr.IPAddress(dict_["ip"])
        peer.port = dict_["port"]
        peer.last_seen = dict_["last_seen"]
        peer.uploaded = dict_["uploaded"]
        peer.downloaded = dict_["downloaded"]
        peer.left = dict_["left"]
        return peer

    @classmethod
    def from_announcement(cls, announcement, ip=None, _clock=_clock_):
        """Return the Peer making this announcement"""
        peer = cls()
        peer.peer_id = announcement["peer_id"]
        peer.port = int(announcement["port"])
        peer.last_seen = _clock.now()
        try:
            peer.ip = ipaddr.IPAddress(announcement["ip"])
        except KeyError:
            peer.ip = ipaddr.IPAddress(ip)
        peer.uploaded = int(announcement["uploaded"])
        peer.downloaded = int(announcement["downloaded"])
        peer.left = int(announcement["left"])
        return peer
