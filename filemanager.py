import argparse
import hashlib
import logging
from pathlib import Path
from time import sleep, time

from collections import namedtuple

from pyocd.core.helpers import ConnectHelper
from littlefs import LittleFS, LittleFSError


def sha256(data):
    return hashlib.sha256(data).digest()

_EMPTY_HASH_DIGEST = sha256(b"")
Variable = namedtuple("Variable", ['address', 'size'])

# fmt: off
# These addresses are fixed via a carefully crafted linker script.
comm = {
    "framebuffer1":            Variable(0x2400_0000, 320 * 240 * 2),
    "framebuffer2":            Variable(0x2402_5800, 320 * 240 * 2),
    "boot_magic":              Variable(0x2000_0000, 4),
    "log_idx":                 Variable(0x2000_0008, 4),
    "logbuf":                  Variable(0x2000_000c, 4096),
    "lfs_cfg":                 Variable(0x2000_1010, 4),
}

# Communication Variables
comm["flashapp_comm"] = comm["framebuffer2"]

comm["flashapp_state"]                 = Variable(comm["flashapp_comm"].address + 0,  4)
comm["program_start"]                  = Variable(comm["flashapp_comm"].address + 4,  4)
comm["program_status"]                 = Variable(comm["flashapp_comm"].address + 8,  4)
comm["program_size"]                   = Variable(comm["flashapp_comm"].address + 12, 4)
comm["program_address"]                = Variable(comm["flashapp_comm"].address + 16, 4)
comm["program_erase"]                  = Variable(comm["flashapp_comm"].address + 20, 4)
comm["program_erase_bytes"]            = Variable(comm["flashapp_comm"].address + 24, 4)
comm["program_chunk_idx"]              = Variable(comm["flashapp_comm"].address + 28, 4)
comm["program_chunk_count"]            = Variable(comm["flashapp_comm"].address + 32, 4)
comm["program_expected_sha256"]        = Variable(comm["flashapp_comm"].address + 36, 32)
comm["buffer_host_should_write_to"]    = Variable(comm["flashapp_comm"].address + 68, 4)
comm["work_buffer_1"]                  = Variable(comm["flashapp_comm"].address + 4096, 256 << 10)
comm["work_buffer_2"]                  = Variable(comm["work_buffer_1"].address + comm["work_buffer_1"].size, 256 << 10)
comm["work_buffer_3"]                  = Variable(comm["work_buffer_2"].address + comm["work_buffer_2"].size, 256 << 10)

# littlefs config struct elements
comm["lfs_cfg_context"]      = Variable(comm["lfs_cfg"].address + 0,  4)
comm["lfs_cfg_read"]         = Variable(comm["lfs_cfg"].address + 4,  4)
comm["lfs_cfg_prog"]         = Variable(comm["lfs_cfg"].address + 8,  4)
comm["lfs_cfg_erase"]        = Variable(comm["lfs_cfg"].address + 12, 4)
comm["lfs_cfg_sync"]         = Variable(comm["lfs_cfg"].address + 16, 4)
comm["lfs_cfg_read_size"]    = Variable(comm["lfs_cfg"].address + 20, 4)
comm["lfs_cfg_prog_size"]    = Variable(comm["lfs_cfg"].address + 24, 4)
comm["lfs_cfg_block_size"]   = Variable(comm["lfs_cfg"].address + 28, 4)
comm["lfs_cfg_block_count"]  = Variable(comm["lfs_cfg"].address + 32, 4)
# TODO: too lazy to add the other lfs_config attributes


_flashapp_state_enum_to_str = {
    0x00000000: "INIT",
    0x00000001: "IDLE",
    0x00000002: "START",
    0x00000003: "CHECK_HASH_RAM_NEXT",
    0x00000004: "CHECK_HASH_RAM",
    0x00000005: "ERASE_NEXT",
    0x00000006: "ERASE",
    0x00000007: "PROGRAM_NEXT",
    0x00000008: "PROGRAM",
    0x00000009: "CHECK_HASH_FLASH_NEXT",
    0x0000000a: "CHECK_HASH_FLASH",
    0x0000000b: "TEST_NEXT",
    0x0000000c: "TEST",
    0x0000000d: "FINAL",
    0x0000000e: "ERROR",
}
_flashapp_state_str_to_enum = {v: k for k, v in _flashapp_state_enum_to_str.items()}

_flashapp_status_enum_to_str  = {
    0         : "BOOTING",
    0xbad00001: "BAD_HASH_RAM",
    0xbad00002: "BAD_HAS_FLASH",
    0xbad00003: "NOT_ALIGNED",
    0xcafe0000: "IDLE",
    0xcafe0001: "DONE",
    0xcafe0002: "BUSY",
}
_flashapp_status_str_to_enum = {v: k for k, v in _flashapp_status_enum_to_str.items()}

# fmt: on


##############
# Exceptions #
##############

class TimeoutError(Exception):
    """Some operation timed out."""


class DataError(Exception):
    """Some data was not as expected."""


class StateError(Exception):
    """On-device flashapp is in the ERROR state."""


############
# LittleFS #
############
class LfsDriverContext:
    def __init__(self, offset) -> None:
        validate_extflash_offset(offset)
        self.offset = offset

    def read(self, cfg: 'LFSConfig', block: int, off: int, size: int) -> bytes:
        logging.getLogger(__name__).debug('LFS Read : Block: %d, Offset: %d, Size=%d' % (block, off, size))
        return extflash_read(self.offset + (block * cfg.block_size), size)

    def prog(self, cfg: 'LFSConfig', block: int, off: int, data: bytes) -> int:
        logging.getLogger(__name__).debug('LFS Prog : Block: %d, Offset: %d, Data=%r' % (block, off, data))
        extflash_write(self.offset + (block * cfg.block_size), data, erase=False)
        return 0

    def erase(self, cfg: 'LFSConfig', block: int) -> int:
        logging.getLogger(__name__).debug('LFS Erase: Block: %d' % block)
        extflash_erase(self.offset + (block * cfg.block_size), cfg.block_size)
        return 0

    def sync(self, cfg: 'LFSConfig') -> int:
        return 0

def chunk_bytes(data, chunk_size):
    return [data[i:i+chunk_size] for i in range(0, len(data), chunk_size)]

def read_int(key: str, signed: bool = False)-> int:
    return int.from_bytes(target.read_memory_block8(*comm[key]), byteorder='little', signed=signed)


def disable_debug():
    """Disables the Debug block, reducing battery consumption."""
    target.halt()
    target.write32(0x5C001004, 0x00000000)
    target.resume()


def write_chunk_idx(idx: int) -> None:
    target.write32(comm["program_chunk_idx"].address, idx)


def write_chunk_count(count: int) -> None:
    target.write32(comm["program_chunk_count"].address, count)

def write_state(state: str) -> None:
    target.write32(comm["flashapp_state"].address, _flashapp_state_str_to_enum[state])


def extflash_erase(offset: int, size: int, whole_chip:bool = False) -> None:
    """Erase a range of data on extflash.

    On-device flashapp will round up to nearest minimum erase size.
    ``program_chunk_idx`` must externally be set.

    Parameters
    ----------
    offset: int
        Offset into extflash to erase.
    size: int
        Number of bytes to erase.
    whole_chip: bool
        If ``True``, ``size`` is ignored and the entire chip is erased.
        Defaults to ``False``.
    """
    validate_extflash_offset(offset)
    if size <= 0 and not whole_chip:
        raise ValueError(f"Size must be >0; 0 erases the entire chip.")

    target.halt()
    target.write32(comm["program_address"].address, offset)
    target.write32(comm["program_erase"].address, 1)       # Perform an erase at `program_address`
    target.write32(comm["program_size"].address, 0)

    if whole_chip:
        target.write32(comm["program_erase_bytes"].address, 0)    # Note: a 0 value erases the whole chip
    else:
        target.write32(comm["program_erase_bytes"].address, size)

    target.write_memory_block8(comm["program_expected_sha256"].address, _EMPTY_HASH_DIGEST)

    target.write32(comm["program_start"].address, 1)       # move the flashapp state from IDLE to executing.
    target.resume()
    wait_for_program_start_0()
    wait_for("IDLE")


def extflash_read(offset: int, size: int) -> bytes:
    """Read data from extflash.

    Parameters
    ----------
    offset: int
        Offset into extflash to read.
    size: int
        Number of bytes to read.
    """
    validate_extflash_offset(offset)
    return bytes(target.read_memory_block8(0x9000_0000 + offset, size))


def extflash_write(offset:int, data: bytes, erase=True) -> None:
    """Write data to extflash.

    ``program_chunk_idx`` must externally be set.

    Parameters
    ----------
    offset: int
        Offset into extflash to write.
    size: int
        Number of bytes to write.
    erase: bool
        Erases flash prior to write.
        Defaults to ``True``.
    """
    validate_extflash_offset(offset)
    if not data:
        return

    target.halt()
    target.write32(comm["program_address"].address, offset)
    target.write32(comm["program_size"].address, len(data))

    if erase:
        target.write32(comm["program_erase"].address, 1)       # Perform an erase at `program_address`
        target.write32(comm["program_erase_bytes"].address, len(data))

    target.write_memory_block8(comm["program_expected_sha256"].address, sha256(data))
    target.write_memory_block8(comm["work_buffer_1"].address, data)

    target.write32(comm["program_start"].address, 1)
    target.resume()
    wait_for_program_start_0()
    wait_for("IDLE")


def read_logbuf():
    return bytes(target.read_memory_block8(*comm["logbuf"])[:read_int("log_idx")]).decode()


def start_flashapp():
    target.reset_and_halt()
    target.write32(comm["flashapp_state"].address, _flashapp_state_str_to_enum["INIT"])
    target.write32(comm["boot_magic"].address, 0xf1a5f1a5)  # Tell bootloader to boot into flashapp
    target.write32(comm["program_status"].address, 0)
    target.write32(comm["program_chunk_idx"].address, 1)  # Can be overwritten later
    target.write32(comm["program_chunk_count"].address, 100)  # Can be overwritten later
    target.resume()
    wait_for("IDLE")


def wait_for_program_start_0(timeout=10):
    t_deadline = time() + 10
    while read_int("program_start"):
        if time() > t_deadline:
            raise TimeoutError
        sleep(0.1)


def wait_for(status: str, timeout=10):
    """Block until the on-device status is matched."""
    t_deadline = time() + 10
    error_mask = 0xFFFF_0000

    while True:
        status_enum = read_int("program_status")
        status_str = _flashapp_status_enum_to_str.get(status_enum, "UNKNOWN")
        if status_str == status:
            break
        elif (status_enum & error_mask) == 0xbad0_0000:
            raise DataError(status_str)
        if time() > t_deadline:
            raise TimeoutError
        sleep(0.1)


def validate_extflash_offset(val):
    if val >= 0x9000_0000:
        raise ValueError(f"Provided extflash offset 0x{val:08X}, did you mean 0x{(val - 0x9000_0000):08X} ?")
    if val % 4096 != 0:
        raise ValueError(f"Extflash offset must be a multiple of 4096.")


def flash(args, fs, block_size, block_count):
    """Flash a binary to the external flash."""
    validate_extflash_offset(args.address)
    data = args.file.read_bytes()
    chunk_size = comm["work_buffer_1"].size
    chunks = chunk_bytes(data, chunk_size)
    write_chunk_count(len(chunks));
    for i, chunk in enumerate(chunks):
        print(f"Writing idx {i + 1}")
        write_chunk_idx(i + 1)
        extflash_write(args.address + (i * chunk_size), chunk)
    write_state("FINAL")


def erase(args, fs, block_size, block_count):
    """Erase the entire external flash."""
    extflash_erase(0, 0, whole_chip=True)
    wait_for("IDLE")


def main():
    commands = {}

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    def add_command(handler):
        """Add a subcommand, like "flash"."""
        subparser = subparsers.add_parser(handler.__name__)
        commands[handler.__name__] = handler
        return subparser

    subparser = add_command(flash)
    subparser.add_argument("file", type=Path,
           help="binary file to flash")
    subparser.add_argument("address", type=lambda x: int(x,0),
           help="Offset into external flash")

    subparser = add_command(erase)

    args = parser.parse_args()

    with ConnectHelper.session_with_chosen_probe() as session:
        global target
        board = session.board
        assert board is not None
        target = board.target

        start_flashapp()

        filesystem_offset = read_int("lfs_cfg_context") - 0x9000_0000
        block_size = read_int("lfs_cfg_block_size")
        block_count = read_int("lfs_cfg_block_count")

        if block_size==0 or block_count==0:
            raise DataError

        lfs_context = LfsDriverContext(filesystem_offset)
        fs = LittleFS(lfs_context, block_size=block_size, block_count=block_count)

        try:
            commands[args.command](args, fs, block_size, block_count)
        except KeyError:
            print(f"Unknown command \"{args.command}\"")
            parser.print_help()
            exit(1)

        # disable_debug()
        target.reset()

if __name__ == "__main__":
    main()
