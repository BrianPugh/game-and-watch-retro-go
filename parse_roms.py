#!/usr/bin/env python3
import argparse
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

ROM_ENTRIES_TEMPLATE = """
const retro_emulator_file_t {name}[] EMU_DATA = {{
{body}
}};
const uint32_t {name}_count = {rom_count};
"""

# Note: this value is not easily changed as it's assumed in some memory optimizations
MAX_GAME_GENIE_CODES = 32

ROM_ENTRY_TEMPLATE = """\t{{
#if GAME_GENIE == 1
\t\t.id = {rom_id},
#endif
\t\t.name = "{name}",
\t\t.ext = "{extension}",
\t\t.address = {rom_entry},
\t\t.size = {size},
\t\t#if COVERFLOW != 0
\t\t.img_address = {img_entry},
\t\t.img_size = {img_size},
\t\t#endif
\t\t.save_address = {save_entry},
\t\t.save_size = {save_size},
\t\t.system = &{system},
\t\t.region = {region},
\t\t.mapper = {mapper},
#if GAME_GENIE == 1
\t\t.game_genie_codes = {game_genie_codes},
\t\t.game_genie_descs = {game_genie_descs},
\t\t.game_genie_count = {game_genie_count},
#endif
\t}},"""

SYSTEM_PROTO_TEMPLATE = """
#if !defined (COVERFLOW)
  #define COVERFLOW 0
#endif /* COVERFLOW */
#if !defined (BIG_BANK)
#define BIG_BANK 1
#endif
#if BIG_BANK == 1
#define EMU_DATA 
#else
#define EMU_DATA __attribute__((section(".extflash_emu_data")))
#endif
extern const rom_system_t {name};
"""

SYSTEM_TEMPLATE = """
const rom_system_t {name} EMU_DATA = {{
\t.system_name = "{system_name}",
\t.roms = {variable_name},
\t.extension = "{extension}",
\t#if COVERFLOW != 0
\t.cover_width = {cover_width},
\t.cover_height = {cover_height},
\t#endif 
\t.roms_count = {roms_count},
}};
"""

SAVE_SIZES = {
    "nes": 24 * 1024,
    "sms": 60 * 1024,
    "gg": 60 * 1024,
    "col": 60 * 1024,
    "sg": 60 * 1024,
    "pce": 76 * 1024,
    "msx": 272 * 1024,
    "gw": 4 * 1024,
}


# TODO: Find a better way to find this before building
MAX_COMPRESSED_NES_SIZE = 0x00081000
MAX_COMPRESSED_PCE_SIZE = 0x00049000

"""
All ``compress_*`` functions must be decorated ``@COMPRESSIONS`` and have the
following signature:

Positional argument:
    data : bytes

Optional argument:
    level : ``None`` for default value,  depends on compression algorithm.
            Can be the special ``DONT_COMPRESS`` sentinel value, in which the
            returned uncompressed data is properly framed to be handled by the
            decompressor.

And return compressed bytes.
"""

DONT_COMPRESS = object()


class CompressionRegistry(dict):
    prefix = "compress_"

    def __call__(self, f):
        name = f.__name__
        assert name.startswith(self.prefix)
        key = name[len(self.prefix) :]
        self[key] = f
        self["." + key] = f
        return f


COMPRESSIONS = CompressionRegistry()


@COMPRESSIONS
def compress_lz4(data, level=None):
    if level == DONT_COMPRESS:
        frame = []

        # Write header
        # write MAGIC WORD
        magic = b"\x04\x22\x4D\x18"
        frame.append(magic)

        # write FLG, BD, HC
        flg = b"\x68"  # independent blocks, no checksum, content-size enabled
        # the uncompressed size of data included within the frame will be present
        # as an 8 bytes unsigned little endian value, after the flags
        frame.append(flg)

        bd = b"\x40"
        frame.append(bd)

        # write uncompressed frame size
        content_size = len(data).to_bytes(8, "little")
        frame.append(content_size)

        if len(data) == 16384:
            # Hardcode in the checksum for this length to reduce dependencies
            hc = b"\x25"
        else:
            from xxhash import xxh32

            hc = xxh32(b"".join(frame[1:])).digest()[2].to_bytes(1, "little")
        frame.append(hc)

        # Write block data
        # Block size in bytes with the highest bit set to 1 to mark the
        # data as uncompressed.
        block_size = (len(data) + 2 ** 31).to_bytes(4, "little")
        frame.append(block_size)

        frame.append(data)

        # Write footer
        # write END_MARK 0x0000
        footer = b"\x00\x00\x00\x00"
        frame.append(footer)

        return b"".join(frame)

    if level is None:
        # TODO: test out lz4.COMPRESSIONLEVEL_MAX
        level = 9

    try:
        import lz4.frame as lz4

        return lz4.compress(
            data,
            compression_level=level,
            block_size=lz4.BLOCKSIZE_MAX1MB,
            block_linked=False,
        )
    except ImportError:
        pass

    # CLI fallback, WARNING: this is slow for individually compressing GB banks
    lz4_path = os.environ["LZ4_PATH"] if "LZ4_PATH" in os.environ else "lz4"
    if not shutil.which(lz4_path):
        raise ImportError
    with TemporaryDirectory() as d:
        d = Path(d)
        file_in = d / "in"
        file_out = d / "out"
        file_in.write_bytes(data)
        cmd = [
            lz4_path,
            "-" + str(level),
            "--content-size",
            "--no-frame-crc",
            file_in,
            file_out,
        ]
        subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        compressed_data = file_out.read_bytes()
    return compressed_data


@COMPRESSIONS
def compress_zopfli(data, level=None):
    if level == DONT_COMPRESS:
        assert 0 <= len(data) <= 65535
        frame = []

        frame.append(b"\x01")  # Not compressed block

        data_len = len(data).to_bytes(2, "little")
        frame.append(data_len)

        data_nlen = (len(data) ^ 0xFFFF).to_bytes(2, "little")
        frame.append(data_nlen)

        frame.append(data)

        return b"".join(frame)

    # Actual zopfli compression is temporarily disabled until a GB/GBC bank
    # swapping bug is resolved.
    # Slightly less efficient vanilla deflate compression is applied

    # import zopfli
    # c = zopfli.ZopfliCompressor(zopfli.ZOPFLI_FORMAT_DEFLATE)

    import zlib

    c = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15, memLevel=9)

    compressed_data = c.compress(data) + c.flush()
    return compressed_data


@COMPRESSIONS
def compress_lzma(data, level=None):
    if level == DONT_COMPRESS:
        if args.compress_gb_speed:
            raise NotImplementedError
        # This currently assumes this will only be applied to GB Bank 0
        return data
    import lzma

    compressed_data = lzma.compress(
        data,
        format=lzma.FORMAT_ALONE,
        filters=[
            {
                "id": lzma.FILTER_LZMA1,
                "preset": 6,
                "dict_size": 16 * 1024,
            }
        ],
    )

    compressed_data = compressed_data[13:]

    return compressed_data


def write_covart(srcfile, fn, w, h, jpg_quality):
    from PIL import Image, ImageOps
    img = Image.open(srcfile).convert(mode="RGB").resize((w, h), Image.ANTIALIAS)
    img.save(fn,format="JPEG",optimize=True,quality=jpg_quality)

# def write_rgb565(srcfile, fn, v):
#     from PIL import Image, ImageOps
#     #print(srcfile)
#     img = Image.open(srcfile).convert(mode="RGB")
#     img = img.resize((w, h), Image.ANTIALIAS)
#     pixels = list(img.getdata())
#     with open(fn, "wb") as f:
#         #no file header
#         for pix in pixels:
#             r = (pix[0] >> 3) & 0x1F
#             g = (pix[1] >> 2) & 0x3F
#             b = (pix[2] >> 3) & 0x1F
#             f.write(struct.pack("H", (r << 11) + (g << 5) + b))

class NoArtworkError(Exception):
    """No artwork found for this ROM"""


def is_valid_game_genie_code(code):
    if '+' in code:
        subcodes = code.split('+')
        if len(subcodes) > 3:
            return False
        for subcode in subcodes:
            if not is_valid_game_genie_code(subcode):
                return False
        return True

    valid_characters = "APZLGITYEOXUKSVN"
    if all(c in valid_characters for c in code) == False:
        return False
    if len(code) != 6 and len(code) != 8:
        return False

    return True


class ROM:
    def __init__(self, system_name: str, filepath: str, extension: str, romdefs: dict):
        filepath = Path(filepath)

        self.rom_id = 0 
        self.path = filepath
        self.filename = filepath
        # Remove compression extension from the name in case it ends with that
        if filepath.suffix in COMPRESSIONS or filepath.suffix == '.cdk':
            self.filename = filepath.with_suffix("").stem
        else :
            self.filename = filepath.stem
        romdefs.setdefault(self.filename, {})
        self.romdef = romdefs[self.filename]
        self.romdef.setdefault('name', self.filename)
        self.romdef.setdefault('publish', '1')
        self.romdef.setdefault('enable_save', '0')
        self.publish = (self.romdef['publish'] == '1')
        self.enable_save = (self.romdef['enable_save'] == '1') or args.save
        self.name = self.romdef['name']
        print("Found rom " + self.filename +" will display name as: " + self.romdef['name'])
        if not (self.publish):
            print("& will not Publish !")
        obj_name = "".join([i if i.isalnum() else "_" for i in self.path.name])
        self.obj_path = "build/roms/" + obj_name + ".o"
        symbol_path = str(self.path.parent) + "/" + obj_name
        self.symbol = (
            "_binary_"
            + "".join([i if i.isalnum() else "_" for i in symbol_path])
            + "_start"
        )

        self.img_path = self.path.parent / (self.filename + ".img")
        obj_name = "".join([i if i.isalnum() else "_" for i in self.img_path.name])
        symbol_path = str(self.path.parent) + "/" + obj_name
        self.obj_img = "build/roms/" + obj_name + "_" + extension + ".o"
        self.img_symbol = (
            "_binary_"
            + "".join([i if i.isalnum() else "_" for i in symbol_path])
            + "_start"
        )

    def __str__(self) -> str:
        return f"id: {self.rom_id} name: {self.name} size: {self.size} ext: {self.ext}"

    def __repr__(self):
        return str(self)

    def read(self):
        return self.path.read_bytes()

    def get_rom_patchs(self):
        #get pce rompatchs files
        pceplus = Path(self.path.parent, self.filename + ".pceplus")

        if not os.path.exists(pceplus):
            return []

        codes_and_descs = []
        for line in pceplus.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                continue
            parts = line.split(',')
            cmd_count = 0
            cmd_str = ""
            for i in range(len(parts) - 1):
                part = parts[i].strip()
                #get cmd byte count
                x_str = part[0:2]
                x = (int(x_str, 16) >> 4) + 1
                one_cmd = ""
                for y in range(3):
                    one_cmd = one_cmd + "\\x" + part[y * 2: y * 2 + 2] 
                for y in range(x):
                    one_cmd = one_cmd + "\\x" + part[y * 2 + 6: y * 2 + 8]
                #got one cmd
                cmd_count += 1
                cmd_str = cmd_str + one_cmd
            cmd_str = "\\x%x" % (cmd_count) + cmd_str
            desc = parts[len(parts) - 1]
            if desc is not None:
                desc = desc[:40]
                desc = desc.replace('\\', r'\\\\')
                desc = desc.replace('"', r'\"')
                desc = desc.strip()

            codes_and_descs.append((cmd_str, desc))

        if len(codes_and_descs) > MAX_GAME_GENIE_CODES:
            print(
                f"INFO: {self.name} has more than {MAX_GAME_GENIE_CODES} Game Genie codes. Truncating..."
            )
            codes_and_descs = codes_and_descs[:MAX_GAME_GENIE_CODES]

        return codes_and_descs

    def get_game_genie_codes(self):
        # Get game genie code file path
        gg_path = Path(self.path.parent, self.filename + ".ggcodes")

        if not os.path.exists(gg_path):
            return self.get_rom_patchs()

        codes_and_descs = []
        for line in gg_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(',', 1)
            code = parts[0]
            desc = None
            if len(parts)>1:
                desc = parts[1]

            # Remove whitespace
            code = "".join(code.split())
            # Remove empty lines
            if code == "":
                continue
            # Capitalize letters
            code = code.upper()
            # Remove invalid codes
            if not is_valid_game_genie_code(code):
                continue

            # Shorten description
            if desc is not None:
                desc = desc[:40]
                desc = desc.replace('\\', r'\\\\')
                desc = desc.replace('"', r'\"')
                desc = desc.strip()

            codes_and_descs.append((code, desc))

        if len(codes_and_descs) > MAX_GAME_GENIE_CODES:
            print(
                f"INFO: {self.name} has more than {MAX_GAME_GENIE_CODES} Game Genie codes. Truncating..."
            )
            codes_and_descs = codes_and_descs[:MAX_GAME_GENIE_CODES]

        return codes_and_descs

    @property
    def ext(self):
        return self.path.suffix[1:].lower()

    @property
    def size(self):
        return self.path.stat().st_size

    @property
    def mapper(self):
        return int(subprocess.check_output([sys.executable, "./tools/findblueMsxMapper.py", "roms/msx_bios/msxromdb.xml", str(self.path)]))

    @property
    def img_size(self):
        try:
            return self.img_path.stat().st_size
        except FileNotFoundError:
            return 0


class ROMParser:
    def find_roms(self, system_name: str, folder: str, extension: str, romdefs: dict) -> [ROM]:
        extension = extension.lower()
        ext = extension
        if not extension.startswith("."):
            extension = "." + extension

        script_path = Path(__file__).parent
        roms_folder = script_path / "roms" / folder

        # find all files that end with extension (case-insensitive)
        rom_files = list(roms_folder.iterdir())
        rom_files = [r for r in rom_files if r.name.lower().endswith(extension)]
        rom_files.sort()
        found_roms = [ROM(system_name, rom_file, ext, romdefs) for rom_file in rom_files]
        for rom in found_roms:
            suffix = "_no_save"
            if rom.name.endswith(suffix) :
                rom.name = rom.name[:-len(suffix)]
                rom.enable_save = False

        return found_roms

    def generate_rom_entries(
        self, name: str, roms: [ROM], save_prefix: str, system: str, game_genie_codes_prefix: str
    ) -> str:
        body = ""
        pubcount = 0
        for i in range(len(roms)):
            rom = roms[i]
            if not (rom.publish):
                continue
            is_pal = any(
                substring in rom.filename
                for substring in [
                    "(E)",
                    "(Europe)",
                    "(Sweden)",
                    "(Germany)",
                    "(Italy)",
                    "(France)",
                    "(A)",
                    "(Australia)",
                ]
            )
            region = "REGION_PAL" if is_pal else "REGION_NTSC"
            gg_count_name = "%s%s_COUNT" % (game_genie_codes_prefix, i)
            gg_code_array_name = "%sCODE_%s" % (game_genie_codes_prefix, i)
            gg_desc_array_name = "%sDESC_%s" % (game_genie_codes_prefix, i)
            body += ROM_ENTRY_TEMPLATE.format(
                rom_id=rom.rom_id,
                name=str(rom.name),
                size=rom.size,
                rom_entry=rom.symbol,
                img_size=rom.img_size,
                img_entry=rom.img_symbol if rom.img_size else "NULL",
                save_entry=(save_prefix + str(i)) if rom.enable_save else "NULL",
                save_size=("sizeof(" + save_prefix + str(i) + ")") if rom.enable_save else "0",
                region=region,
                extension=rom.ext,
                system=system,
                game_genie_codes=gg_code_array_name if game_genie_codes_prefix else "NULL",
                game_genie_descs=gg_desc_array_name if game_genie_codes_prefix else 0,
                game_genie_count=gg_count_name if game_genie_codes_prefix else 0,
                mapper=rom.mapper,
            )
            body += "\n"
            pubcount += 1

        return ROM_ENTRIES_TEMPLATE.format(name=name, body=body, rom_count=pubcount)

    def generate_object_file(self, rom: ROM) -> str:
        # convert rom to an .o file and place the data in the .extflash_game_rom section
        prefix = ""
        if "GCC_PATH" in os.environ:
            prefix = os.environ["GCC_PATH"]
        prefix = Path(prefix)

        subprocess.check_output(
            [
                prefix / "arm-none-eabi-objcopy",
                "--rename-section",
                ".data=.extflash_game_rom,alloc,load,readonly,data,contents",
                "-I",
                "binary",
                "-O",
                "elf32-littlearm",
                "-B",
                "armv7e-m",
                rom.path,
                rom.obj_path,
            ]
        )
        subprocess.check_output(
            [
                prefix / "arm-none-eabi-ar",
                "-cr",
                "build/roms.a",
                rom.obj_path,
            ]
        )
        template = "extern const uint8_t {name}[];\n"
        return template.format(name=rom.symbol)

    def generate_img_object_file(self, rom: ROM, w, h) -> str:
        # convert rom_img to an .o file and place the data in the .extflash_game_rom section
        prefix = ""
        if "GCC_PATH" in os.environ:
            prefix = os.environ["GCC_PATH"]

        prefix = Path(prefix)

        imgs = []
        imgs.append(str(rom.img_path.with_suffix(".png")))
        imgs.append(str(rom.img_path.with_suffix(".PNG")))
        imgs.append(str(rom.img_path.with_suffix(".Png")))
        imgs.append(str(rom.img_path.with_suffix(".jpg")))
        imgs.append(str(rom.img_path.with_suffix(".JPG")))
        imgs.append(str(rom.img_path.with_suffix(".Jpg")))
        imgs.append(str(rom.img_path.with_suffix(".jpeg")))
        imgs.append(str(rom.img_path.with_suffix(".JPEG")))
        imgs.append(str(rom.img_path.with_suffix(".Jpeg")))
        imgs.append(str(rom.img_path.with_suffix(".bmp")))
        imgs.append(str(rom.img_path.with_suffix(".BMP")))
        imgs.append(str(rom.img_path.with_suffix(".Bmp")))

        for img in imgs:
            if Path(img).exists():
                write_covart(Path(img), rom.img_path, w, h, args.jpg_quality)
                break

        if not rom.img_path.exists():
            raise NoArtworkError

        print(f"INFO: Packing {rom.name} Cover> {rom.img_path} ...")
        subprocess.check_output(
            [
                prefix / "arm-none-eabi-objcopy",
                "--rename-section",
                ".data=.extflash_game_rom,alloc,load,readonly,data,contents",
                "-I",
                "binary",
                "-O",
                "elf32-littlearm",
                "-B",
                "armv7e-m",
                rom.img_path,
                rom.obj_img,
            ]
        )
        subprocess.check_output(
            [
                prefix / "arm-none-eabi-ar",
                "-cru",
                "build/roms.a",
                rom.obj_img,
            ]
        )
        template = "extern const uint8_t {name}[];\n"
        return template.format(name=rom.img_symbol)

    def generate_save_entry(self, name: str, save_size: int) -> str:
        return f'uint8_t {name}[{save_size}]  __attribute__((section (".saveflash"))) __attribute__((aligned(4096)));\n'

    def generate_game_genie_entry(self, name: str, num: int, game_genie_codes_and_descs: []) -> str:
        str = "";

        codes = "{%s}" % ",".join(f'"{c}"' for (c,d) in game_genie_codes_and_descs)
        descs = "{%s}" % ",".join(f'NULL' if d is None else f'"{d}"' for (c,d) in game_genie_codes_and_descs)
        number_of_codes = len(game_genie_codes_and_descs)

        count_name = "%s%s_COUNT" % (name, num)
        code_array_name = "%sCODE_%s" % (name, num)
        desc_array_name = "%sDESC_%s" % (name, num)
        str += f'#if GAME_GENIE == 1\n'
        str += f'const char* {code_array_name}[{number_of_codes}] = {codes};\n'
        str += f'const char* {desc_array_name}[{number_of_codes}] = {descs};\n'
        str += f'const int {count_name} = {number_of_codes};\n'
        str += f'#endif\n'

        return str

    def get_gameboy_save_size(self, file: Path):
        total_size = 4096
        file = Path(file)

        if file.suffix in COMPRESSIONS:
            file = file.with_suffix("")  # Remove compression suffix

        with open(file, "rb") as f:
            # cgb
            f.seek(0x143)
            cgb = ord(f.read(1))

            # 0x80 = Gameboy color but supports old gameboy
            # 0xc0 = Gameboy color only
            if cgb & 0x80 or cgb == 0xC0:
                # Bank 0 + 1-7 for gameboy color work ram
                total_size += 8 * 4096  # irl

                # Bank 0 + 1 for gameboy color video ram, 2*8KiB
                total_size += 4 * 4096  # vrl
            else:
                # Bank 0 + 1 for gameboy classic work ram
                total_size += 2 * 4096  # irl

                # Bank 0 only for gameboy classic video ram, 1*8KiB
                total_size += 2 * 4096  # vrl

            # Cartridge ram size
            f.seek(0x149)
            total_size += [1, 1, 1, 4, 16, 8][ord(f.read(1))] * 8 * 1024
            return total_size

        return 0

    def _compress_rom(self, variable_name, rom, compress_gb_speed=False, compress=None):
        """This will create a compressed rom file next to the original rom."""
        if not (rom.publish):
            return
        if compress is None:
            compress = "lz4"

        if compress not in COMPRESSIONS:
            raise ValueError(f'Unknown compression method: "{compress}"')

        if compress[0] != ".":
            compress = "." + compress
        output_file = Path(str(rom.path) + compress)
        compress = COMPRESSIONS[compress]
        data = rom.read()

        if "nes_system" in variable_name:  # NES
            if rom.path.stat().st_size > MAX_COMPRESSED_NES_SIZE:
                print(
                    f"INFO: {rom.name} is too large to compress, skipping compression!"
                )
                return
            compressed_data = compress(data)
            output_file.write_bytes(compressed_data)
        elif "pce_system" in variable_name:  # PCE
            if rom.path.stat().st_size > MAX_COMPRESSED_PCE_SIZE:
                print(
                    f"INFO: {rom.name} is too large to compress, skipping compression!"
                )
                return
            compressed_data = compress(data)
            output_file.write_bytes(compressed_data)
        elif "gb_system" in variable_name:  # GB/GBC
            BANK_SIZE = 16384
            banks = [data[i : i + BANK_SIZE] for i in range(0, len(data), BANK_SIZE)]
            compressed_banks = [compress(bank) for bank in banks]

            # For ROM having continous bank switching we can use 'partial' compression
            # a mix of comcompressed and uncompress
            # compress empty banks and the bigger compress ratio
            compress_its = [True] * len(banks)
            compress_its[0] = False  # keep bank0 uncompressed

            # START : ALTERNATIVE COMPRESSION STRATEGY
            if compress_gb_speed:
                # the larger banks only are compressed.
                # It shoul fit exactly in the cache reducing the SWAP cache feequency to 0.
                # any empty bank is compressed (=98bytes). considered never used by MBC.

                # Ths is the cache size used as a compression credit
                # TODO : can we the value from the linker ?
                compression_credit = 26
                compress_size = [len(bank) for bank in compressed_banks[1:]]

                # to keep empty banks compressed (size=98)
                compress_size = [i for i in compress_size if i > 98]

                ordered_size = sorted(compress_size)

                if compression_credit > len(ordered_size):
                    compression_credit = len(ordered_size) - 1

                compress_threshold = ordered_size[int(compression_credit)]

                for i, bank in enumerate(compressed_banks):
                    if len(bank) >= compress_threshold:
                        # Don't compress banks with poor compression
                        compress_its[i] = False
            # END : ALTERNATIVE COMPRESSION STRATEGY

            # Reassemble all banks back into one file
            output_banks = []
            for bank, compressed_bank, compress_it in zip(
                banks, compressed_banks, compress_its
            ):
                if compress_it:
                    output_banks.append(compressed_bank)
                else:
                    output_banks.append(compress(bank, level=DONT_COMPRESS))
            output_data = b"".join(output_banks)

            output_file.write_bytes(output_data)

    def _convert_dsk(self, variable_name, dsk, compress):
        """This will convert dsk image to cdk."""
        if not (dsk.publish):
            return
        if compress is None:
            compress="none"

        if "msx_system" in variable_name:  # MSX
            subprocess.check_output("python3 tools/dsk2lzma.py \""+str(dsk.path)+"\" "+compress, shell=True)

    def generate_system(
        self,
        file: str,
        system_name: str,
        variable_name: str,
        folder: str,
        extensions: List[str],
        save_prefix: str,
        romdefs: dict,
        game_genie_codes_prefix: str,
        current_id: int,
        compress: str = None,
        compress_gb_speed: bool = False,
    ) -> int:
        import json;
        script_path = Path(__file__).parent
        json_file = script_path / "roms" / str(folder + ".json")
        print(json_file)
        if Path(json_file).exists():
            with open(json_file,'r') as load_f:
                try:
                    romdef = json.load(load_f)
                    load_f.close()
                    romdefs = romdef
                except: 
                    load_f.close()
 
        roms_raw = []
        for e in extensions:
            roms_raw += self.find_roms(system_name, folder, e, romdefs)

        def find_compressed_roms():
            if not compress:
                return []

            roms = []
            for e in extensions:
                roms += self.find_roms(system_name, folder, e + "." + compress, romdefs)
            return roms

        def find_disks():
            disks = self.find_roms(system_name, folder, "dsk", romdefs)
            # If a disk name ends with _no_save then it means that we shouldn't
            # allocate save space for this disk (to use with multi disks games
            # as they only need to get a save for the first disk)
            for disk in disks:
                suffix = "_no_save"
                if disk.name.endswith(suffix) :
                    disk.name = disk.name[:-len(suffix)]
                    disk.enable_save = False
            return disks

        def find_cdk_disks():
            disks = self.find_roms(system_name, folder, "cdk", romdefs)
            for disk in disks:
                suffix = "_no_save"
                if disk.name.endswith(suffix) :
                    disk.name = disk.name[:-len(suffix)]
                    disk.enable_save = False
            return disks

        def contains_rom_by_name(rom, roms):
            for r in roms:
                if r.name == rom.name:
                    return True
            return False

        cdk_disks = find_cdk_disks()

        disks_raw = [r for r in roms_raw if not contains_rom_by_name(r, cdk_disks)]
        disks_raw = [r for r in disks_raw if r.ext == "dsk"]

        if disks_raw:
            pbar = tqdm(disks_raw) if tqdm else disks_raw
            for r in pbar:
                if tqdm:
                    pbar.set_description(f"Converting: {system_name} / {r.name}")
                self._convert_dsk(
                    variable_name,
                    r,
                    compress)
            # Re-generate the cdk disks list
            cdk_disks = find_cdk_disks()
        #remove .dsk from list
        roms_raw = [r for r in roms_raw if not r.ext == "dsk"]
        #add .cdk disks to list
        roms_raw.extend(cdk_disks)

        roms_compressed = find_compressed_roms()

        roms_raw = [r for r in roms_raw if not contains_rom_by_name(r, roms_compressed)]
        if roms_raw and compress is not None:
            pbar = tqdm(roms_raw) if tqdm else roms_raw
            for r in pbar:
                if tqdm:
                    pbar.set_description(f"Compressing: {system_name} / {r.name}")
                self._compress_rom(
                    variable_name,
                    r,
                    compress_gb_speed=compress_gb_speed,
                    compress=compress,
                )
            # Re-generate the compressed rom list
            roms_compressed = find_compressed_roms()

        # Create a list with all compressed roms and roms that
        # don't have a compressed counterpart.
        roms = roms_compressed[:]
        for r in roms_raw:
            if not contains_rom_by_name(r, roms_compressed):
                roms.append(r)

        for rom in roms:
            rom.rom_id = current_id
            current_id += 1

        total_save_size = 0
        total_rom_size = 0
        total_img_size = 0
        pubcount = 0
        for i, rom in enumerate(roms):
            if not (rom.publish):
                continue
            else:
               pubcount += 1

        save_size = SAVE_SIZES.get(folder, 0)
        romdefs.setdefault("_cover_width", 128)
        romdefs.setdefault("_cover_height", 96)
        cover_width = romdefs["_cover_width"]
        cover_height = romdefs["_cover_height"]
        cover_width = 180 if cover_width > 180 else 64 if cover_width < 64 else cover_width
        cover_height = 136 if cover_height > 136 else 64 if cover_height < 64 else cover_height

        img_max = cover_width * cover_height

        if img_max > 18600:
            print(f"Error: {system_name} Cover art image [width:{cover_width} height: {cover_height}] will overflow!")
            exit(-1)        

        with open(file, "w", encoding = args.codepage) as f:
            f.write(SYSTEM_PROTO_TEMPLATE.format(name=variable_name))

            for i, rom in enumerate(roms):
                if not (rom.publish):
                    continue
                if folder == "gb":
                    save_size = self.get_gameboy_save_size(rom.path)

                # Aligned
                aligned_size = 4 * 1024
                if rom.enable_save:
                    total_save_size += (
                        (save_size + aligned_size - 1) // (aligned_size)
                    ) * aligned_size
                total_rom_size += rom.size
                if (args.coverflow != 0) :
                    total_img_size += rom.img_size

                f.write(self.generate_object_file(rom))
                if (args.coverflow != 0) :
                    try:
                        f.write(self.generate_img_object_file(rom, cover_width, cover_height))
                    except NoArtworkError:
                        pass
                if rom.enable_save:
                    f.write(self.generate_save_entry(save_prefix + str(i), save_size))

                game_genie_codes_and_descs = rom.get_game_genie_codes();
                if game_genie_codes_prefix:
                    f.write(self.generate_game_genie_entry(game_genie_codes_prefix, i, game_genie_codes_and_descs))

            rom_entries = self.generate_rom_entries(
                folder + "_roms", roms, save_prefix, variable_name, game_genie_codes_prefix
            )
            f.write(rom_entries)

            f.write(
                SYSTEM_TEMPLATE.format(
                    name=variable_name,
                    system_name=system_name,
                    variable_name=folder + "_roms",
                    extension=folder,
                    cover_width=cover_width,
                    cover_height=cover_height,
                    roms_count=pubcount,
                )
            )

        return total_save_size, total_rom_size, total_img_size, current_id

    def write_if_changed(self, path: str, data: str):
        path = Path(path)
        old_data = None
        if path.exists():
            old_data = path.read_text()
        if data != old_data:
            path.write_text(data)

    def parse(self, args):
        total_save_size = 0
        total_rom_size = 0
        total_img_size = 0
        build_config = ""
        current_id = 0

        import json;
        script_path = Path(__file__).parent
        json_file = script_path / "roms" / "roms.json"
        if Path(json_file).exists():
            with open(json_file,'r') as load_f:
                try:
                    romdef = json.load(load_f)
                    load_f.close()
                except: 
                    romdef = {}
                    load_f.close()
        else :
            romdef = {}

        romdef.setdefault('gb', {})
        romdef.setdefault('nes', {})
        romdef.setdefault('sms', {})
        romdef.setdefault('gg', {})
        romdef.setdefault('col', {})
        romdef.setdefault('sg', {})
        romdef.setdefault('pce', {})
        romdef.setdefault('gw', {})
        romdef.setdefault('msx', {})
        romdef.setdefault('msx_bios', {})

        save_size, rom_size, img_size, current_id = self.generate_system(
            "Core/Src/retro-go/gb_roms.c",
            "Nintendo Gameboy",
            "gb_system",
            "gb",
            ["gb", "gbc"],
            "SAVE_GB_",
            romdef["gb"],
            None,
            current_id,
            args.compress,
            args.compress_gb_speed,
        )
        total_save_size += save_size
        total_rom_size += rom_size
        total_img_size += img_size
        build_config += "#define ENABLE_EMULATOR_GB\n" if rom_size > 0 else ""

        save_size, rom_size, img_size, current_id = self.generate_system(
            "Core/Src/retro-go/nes_roms.c",
            "Nintendo Entertainment System",
            "nes_system",
            "nes",
            ["nes"],
            "SAVE_NES_",
            romdef["nes"],
            "GG_NES_",
            current_id,
            args.compress,
        )
        total_save_size += save_size
        total_rom_size += rom_size
        total_img_size += img_size
        build_config += "#define ENABLE_EMULATOR_NES\n" if rom_size > 0 else ""

        save_size, rom_size, img_size, current_id = self.generate_system(
            "Core/Src/retro-go/sms_roms.c",
            "Sega Master System",
            "sms_system",
            "sms",
            ["sms"],
            "SAVE_SMS_",
            romdef["sms"],
            None,
            current_id,
        )
        total_save_size += save_size
        total_rom_size += rom_size
        total_img_size += img_size
        build_config += "#define ENABLE_EMULATOR_SMS\n" if rom_size > 0 else ""

        save_size, rom_size, img_size, current_id = self.generate_system(
            "Core/Src/retro-go/gg_roms.c",
            "Sega Game Gear",
            "gg_system",
            "gg",
            ["gg"],
            "SAVE_GG_",
            romdef["gg"],
            None,
            current_id,
        )
        total_save_size += save_size
        total_rom_size += rom_size
        total_img_size += img_size
        build_config += "#define ENABLE_EMULATOR_GG\n" if rom_size > 0 else ""

        save_size, rom_size, img_size, current_id = self.generate_system(
            "Core/Src/retro-go/col_roms.c",
            "Colecovision",
            "col_system",
            "col",
            ["col"],
            "SAVE_COL_",
            romdef["col"],
            None,
            current_id,
        )
        total_save_size += save_size
        total_rom_size += rom_size
        total_img_size += img_size
        build_config += "#define ENABLE_EMULATOR_COL\n" if rom_size > 0 else ""

        save_size, rom_size, img_size, current_id = self.generate_system(
            "Core/Src/retro-go/sg1000_roms.c",
            "Sega SG-1000",
            "sg1000_system",
            "sg",
            ["sg"],
            "SAVE_SG1000_",
            romdef["sg"],
            None,
            current_id,
        )
        total_save_size += save_size
        total_rom_size += rom_size
        total_img_size += img_size
        build_config += "#define ENABLE_EMULATOR_SG1000\n" if rom_size > 0 else ""

        save_size, rom_size, img_size, current_id = self.generate_system(
            "Core/Src/retro-go/pce_roms.c",
            "PC Engine",
            "pce_system",
            "pce",
            ["pce"],
            "SAVE_PCE_",
            romdef["pce"],
            "GG_PCE_",
            current_id,
            args.compress,
        )

        total_save_size += save_size
        total_rom_size += rom_size
        total_img_size += img_size
        build_config += "#define ENABLE_EMULATOR_PCE\n" if rom_size > 0 else ""

        save_size, rom_size, img_size, current_id = self.generate_system(
            "Core/Src/retro-go/gw_roms.c",
            "Game & Watch",
            "gw_system",
            "gw",
            ["gw"],
            "SAVE_GW_",
            romdef["gw"],
            None,
            current_id,
        )
        total_save_size += save_size
        total_rom_size += rom_size
        total_img_size += img_size
        build_config += "#define ENABLE_EMULATOR_GW\n" if rom_size > 0 else ""

        save_size, rom_size, img_size = self.generate_system(
            "Core/Src/retro-go/msx_roms.c",
            "MSX",
            "msx_system",
            "msx",
            ["rom","mx1","mx2","dsk"],
            "SAVE_MSX_",
            romdef["msx"],
            args.compress
        )
        total_save_size += save_size
        total_rom_size += rom_size
        total_img_size += img_size
        #bios
        save_size, rom_size, img_size = self.generate_system(
            "Core/Src/retro-go/msx_bios.c",
            "MSX_BIOS",
            "msx_bios",
            "msx_bios",
            ["rom","sha"],
            "SAVE_MSXB_",
            romdef["msx_bios"]
        )
        total_save_size += save_size
        total_rom_size += rom_size
        total_img_size += img_size
        build_config += "#define ENABLE_EMULATOR_MSX\n" if rom_size > 0 else ""

        total_size = total_save_size + total_rom_size + total_img_size

        if total_size == 0:
            print(
                "No roms found! Please add at least one rom to one of the the directories in roms/"
            )
            exit(-1)

        if args.verbose:
            print(
                f"Save data:\t{total_save_size} bytes\nROM data:\t{total_rom_size} bytes\n"
                f"Cover images:\t{total_img_size} bytes\n"
                f"Total:\t\t{total_size} / {args.flash_size} bytes (plus some metadata)."
            )
        if total_size > args.flash_size:
            print(f"Error: External flash will overflow! Need at least {total_size / 1024 / 1024 :.2f} MB")
            # Delete build/roms.a - the makefile will run parse_roms.py if this file is outdated or missing.
            try:
                Path("build/roms.a").unlink()
            except FileNotFoundError as e:
                pass
            exit(-1)

        build_config += "#define ROM_COUNT %d\n" % current_id
        build_config += "#define MAX_GAME_GENIE_CODES %d\n" % MAX_GAME_GENIE_CODES

        self.write_if_changed(
            "build/saveflash.ld", f"__SAVEFLASH_LENGTH__ = {total_save_size};\n"
        )
        self.write_if_changed("build/config.h", build_config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import ROMs to the build environment")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--flash-size",
        "-s",
        type=int,
        default=1024 * 1024,
        help="Size of external SPI flash in bytes.",
    )
    parser.add_argument(
        "--codepage",
        type=str,
        default="ansi",
        help="save file's code page.",
    )
    parser.add_argument(
        "--coverflow",
        type=int,
        default=0,
        help="set coverflow image file pack",
    )
    parser.add_argument(
        "--jpg_quality",
        type=int,
        default=90,
        help="skip convert cover art image jpg quality",
    )
    compression_choices = [t for t in COMPRESSIONS if not t[0] == "."]
    parser.add_argument(
        "--compress",
        choices=compression_choices,
        type=str,
        default=None,
        help="Compression method. Defaults to no compression.",
    )
    parser.add_argument(
        "--compress_gb_speed",
        dest="compress_gb_speed",
        action="store_true",
        help="Apply only selective compression to gameboy banks. Only apply "
        "if bank decompression during switching is too slow.",
    )
    parser.add_argument(
        "--no-compress_gb_speed", dest="compress_gb_speed", action="store_false"
    )
    parser.set_defaults(compress_gb_speed=False)
    parser.add_argument("--no-save", dest="save", action="store_false")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose prints",
    )
    args = parser.parse_args()
    
    if args.compress and "." + args.compress not in COMPRESSIONS:
        raise ValueError(f"Unknown compression method specified: {args.compress}")

    roms_path = Path("build/roms")
    roms_path.mkdir(mode=0o755, parents=True, exist_ok=True)

    # Check for zip and 7z files. If found, tell user to extract and delete them.
    zip_files = []
    zip_files.extend(Path("roms").glob("*/*.zip"))
    zip_files.extend(Path("roms").glob("*/*.7z"))
    if zip_files:
        print("\nzip and/or 7z rom files found. Please extract and delete them:")
        for f in zip_files:
            print(f"    {f}")
        print("")
        exit(-1)

    try:
        ROMParser().parse(args)
    except ImportError as e:
        print(e)
        print("Missing dependencies. Run:")
        print("    python -m pip install -r requirements.txt")
        exit(-1)
