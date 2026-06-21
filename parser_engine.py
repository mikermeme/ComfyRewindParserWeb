# parser_engine.py
import os
import csv
import json
import struct
import re
import base64
import mmap
from io import BytesIO
from pathlib import Path
from collections import defaultdict

# --- COORDINATE HELPER ---

def safe_get_coord(obj, key, index, default=""):
    """
    Safely retrieves a coordinate value from either a dictionary or a list/tuple.
    Prevents crash variations if vectors/sectors are formatted as arrays.
    """
    if isinstance(obj, dict):
        return obj.get(key, default)
    elif isinstance(obj, (list, tuple)):
        try:
            return obj[index]
        except IndexError:
            return default
    return default


# --- REWIND DECODER (Originally rewind.py / input_file_4) ---

class Reader:
    def __init__(self, fp):
        self.fp = fp

    def read(self, n):
        return self.fp.read(n)

    def skip(self, n):
        self.fp.seek(n, 1)

    def u8(self):
        return struct.unpack("<B", self.read(1))[0]

    def s8(self):
        return struct.unpack("<b", self.read(1))[0]

    def u16(self):
        return struct.unpack("<H", self.read(2))[0]

    def u32(self):
        return struct.unpack("<I", self.read(4))[0]

    def s32(self):
        return struct.unpack("<i", self.read(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.read(8))[0]

    def s64(self):
        return struct.unpack("<q", self.read(8))[0]

    def f32(self):
        return struct.unpack("<f", self.read(4))[0]

    def tell(self):
        return self.fp.tell()


def read_leb128(r):
    value = 0
    shift = 0
    while True:
        b = r.u8()
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return value


def u32_to_signed(v):
    return struct.unpack("<i", struct.pack("<I", v))[0]


def strip_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'//.*', '', text)
    return text


def load_hexpat_mappings(hexpat_file):
    zdo_vars = {}
    prefabs = {}
    if not os.path.exists(hexpat_file):
        return zdo_vars, prefabs

    try:
        with open(hexpat_file, "r", encoding="utf-8") as f:
            content = strip_comments(f.read())
            
        enum_blocks = re.findall(r'enum\s+(\w+)\s*:\s*\w+\s*\{([^}]+)\}', content)
        for enum_name, enum_body in enum_blocks:
            for line in enum_body.split('\n'):
                line = line.strip()
                if not line:
                    continue
                match = re.match(r'(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)', line)
                if match:
                    var_name = match.group(1)
                    var_val_str = match.group(2)
                    var_val = int(var_val_str, 16) if var_val_str.startswith('0x') else int(var_val_str, 10)
                    signed_val = u32_to_signed(var_val)
                    
                    if enum_name == "ZDOVar":
                        existing = zdo_vars.get(signed_val)
                        if not existing or (existing.startswith('s_') and not var_name.startswith('s_')):
                            zdo_vars[signed_val] = var_name
                    elif enum_name == "Prefab":
                        prefabs[signed_val] = var_name
    except Exception:
        pass
    return zdo_vars, prefabs


def load_prefabs(csv_file):
    prefabs = {}
    if not os.path.exists(csv_file):
        return prefabs
    with open(csv_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                signed = int(row["prefab_hash_signed"])
                prefabs[signed] = row["prefab_name"]
            except Exception:
                pass
    return prefabs


def read_vector3(r):
    return {"x": r.f32(), "y": r.f32(), "z": r.f32()}


def read_quaternion(r):
    return {"x": r.f32(), "y": r.f32(), "z": r.f32(), "w": r.f32()}


def read_vector2i(r):
    return {"x": r.s32(), "y": r.s32()}


def read_string_entry(r):
    var_hash = r.u32()
    length = read_leb128(r)
    raw = r.read(length)
    try:
        value = raw.decode("utf-8")
    except:
        value = raw.decode("utf-8", errors="replace")
    return var_hash, value


def dump_rewind(rewind_file, prefab_csv, output_json, hexpat_file="rewind.hexpat"):
    zdo_vars, hexpat_prefabs = load_hexpat_mappings(hexpat_file)
    prefab_map = hexpat_prefabs.copy()
    csv_prefabs = load_prefabs(prefab_csv)
    prefab_map.update(csv_prefabs)

    results = []
    with open(rewind_file, "rb") as f:
        r = Reader(f)
        header = {
            "magic": r.u32(),
            "count": r.u32(),
            "offset": read_vector3(r)
        }

        for index in range(header["count"]):
            zdo = {}
            try:
                zdo["userID"] = r.u64()
                zdo["zdoID"] = r.u32()
                r.skip(6)
                zdo["ownerRevision"] = r.u16()
                zdo["dataRevision"] = r.u32()
                zdo["persistent"] = bool(r.u8())
                zdo["userKey"] = r.s64()
                zdo["timeCreated"] = r.s64()
                zdo["zero"] = r.u32()
                zdo["type"] = r.s8()
                zdo["distant"] = bool(r.u8())

                prefab_u32 = r.u32()
                prefab_signed = u32_to_signed(prefab_u32)
                zdo["prefabHash"] = prefab_signed
                zdo["prefabName"] = prefab_map.get(prefab_signed, str(prefab_signed))

                zdo["sector"] = read_vector2i(r)
                zdo["position"] = read_vector3(r)
                zdo["rotation"] = read_quaternion(r)

                floats = {}
                vec3s = {}
                quats = {}
                ints = {}
                longs = {}
                strings = {}
                bytes_dict = {}

                count = r.u8()
                for _ in range(count):
                    h = r.u32()
                    v = r.f32()
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    floats[key_str] = v

                count = r.u8()
                for _ in range(count):
                    h = r.u32()
                    v = read_vector3(r)
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    vec3s[key_str] = v
                    strings[f"vec3:{key_signed}"] = v

                count = r.u8()
                for _ in range(count):
                    h = r.u32()
                    v = read_quaternion(r)
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    quats[key_str] = v
                    strings[f"quat:{key_signed}"] = v

                count = r.u8()
                for _ in range(count):
                    h = r.u32()
                    v = r.s32()
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    ints[key_str] = v

                count = r.u8()
                for _ in range(count):
                    h = r.u32()
                    v = r.s64()
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    longs[key_str] = v

                count = r.u8()
                for _ in range(count):
                    h, v = read_string_entry(r)
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    strings[key_str] = v

                byte_count = r.u8()
                for _ in range(byte_count):
                    h = r.u32()
                    length = r.u32()
                    v = r.read(length)
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    bytes_dict[key_str] = base64.b64encode(v).decode('utf-8')

                zdo["floats"] = floats
                zdo["vec3s"] = vec3s
                zdo["quats"] = quats
                zdo["ints"] = ints
                zdo["longs"] = longs
                zdo["strings"] = strings
                if bytes_dict:
                    zdo["bytes"] = bytes_dict
                results.append(zdo)
            except Exception as e:
                raise e

    output_data = {
        "type": "DB",
        "zdoList": {
            "zdos": results
        }
    }
    with open(output_json, "w", encoding="utf-8") as out:
        json.dump(output_data, out, indent=2, ensure_ascii=False)


# --- EXPORTER & MATCHER (Originally table.py / input_file_5) ---

def get_stable_hash_code(s: str) -> int:
    hash_val = 5381
    for char in s:
        hash_val = ((hash_val << 5) + hash_val) ^ ord(char)
        hash_val = (hash_val & 0xFFFFFFFF)
    if hash_val >= 0x80000000:
        hash_val -= 0x100000000
    return hash_val

HASH_ITEMS = str(get_stable_hash_code("items"))
HASH_CREATOR = str(get_stable_hash_code("creator"))
HASH_HEALTH = str(get_stable_hash_code("health"))
HASH_TAG = str(get_stable_hash_code("tag"))
HASH_TEXT = str(get_stable_hash_code("text"))
HASH_NAME = str(get_stable_hash_code("name"))
HASH_CUSTOM_NAME = str(get_stable_hash_code("custom_name"))
HASH_LEVEL = str(get_stable_hash_code("level"))
HASH_CRAFTER_ID = str(get_stable_hash_code("crafterID"))


def get_zdo_value(zdo, category, field_name, hash_str):
    by_name = zdo.get(f"{category}ByName")
    if by_name and field_name in by_name:
        return by_name[field_name]
    
    normal = zdo.get(category)
    if normal:
        if field_name in normal:
            return normal[field_name]
        if hash_str in normal:
            return normal[hash_str]
        try:
            hash_int = int(hash_str)
            if hash_int in normal:
                return normal[hash_int]
        except ValueError:
            pass
    return None


def read_7bit_int(f):
    result = 0
    shift = 0
    while True:
        b = f.read(1)
        if not b:
            raise EOFError()
        b = b[0]
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result
        shift += 7


def read_string(f):
    length = read_7bit_int(f)
    if length == 0:
        return ""
    return f.read(length).decode("utf-8", errors="replace")


def read_bool(f):
    return struct.unpack("<?", f.read(1))[0]


def read_int(f):
    return struct.unpack("<i", f.read(4))[0]


def read_long(f):
    return struct.unpack("<q", f.read(8))[0]


def read_float(f):
    return struct.unpack("<f", f.read(4))[0]


def read_vector2i(f):
    return (read_int(f), read_int(f))


def parse_inventory(items_field):
    try:
        data = base64.b64decode(items_field)
    except Exception:
        return []

    f = BytesIO(data)
    try:
        version = read_int(f)
        item_count = read_int(f)
    except (EOFError, struct.error):
        return []

    items = []
    for _ in range(item_count):
        try:
            prefab = read_string(f)
            stack = read_int(f)
            durability = read_float(f)
            x, y = read_vector2i(f)
            equipped = read_bool(f)
            quality = read_int(f)
            variant = read_int(f)
            crafter_id = read_long(f)
            crafter_name = read_string(f)
            custom_count = read_int(f)

            custom_data = {}
            for _ in range(custom_count):
                key = read_string(f)
                value = read_string(f)
                custom_data[key] = value

            world_level = read_int(f)
            picked_up = read_bool(f)

            items.append({
                "prefab": prefab,
                "stack": stack,
                "durability": durability,
                "x": x,
                "y": y,
                "equipped": equipped,
                "quality": quality,
                "variant": variant,
                "crafter_id": crafter_id,
                "crafter_name": crafter_name,
                "world_level": world_level,
                "picked_up": picked_up,
                "custom_data_count": custom_count,
                "custom_data": custom_data,
            })
        except (EOFError, struct.error):
            break
    return items


ZDO_START_PAT = re.compile(b'\\{\\s*"userID"\\s*:')
ZDO_TOKEN_PAT = re.compile(b'"(?:[^"\\\\]|\\\\.)*"|\\{|\\}')

def get_zdo_bounds_fast(mm, start_pos):
    depth = 0
    for match in ZDO_TOKEN_PAT.finditer(mm, start_pos):
        token = match.group()
        if token == b'{':
            depth += 1
        elif token == b'}':
            depth -= 1
            if depth == 0:
                return match.end()
    return None


def iterate_zdos(file_path: Path):
    with open(file_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(0)
        if size == 0:
            return
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for match in ZDO_START_PAT.finditer(mm):
                start_pos = match.start()
                end_pos = get_zdo_bounds_fast(mm, start_pos)
                if end_pos is not None:
                    zdo_bytes = mm[start_pos:end_pos]
                    if (b'"items"' in zdo_bytes or 
                        b'"179721187"' in zdo_bytes or 
                        b'itemstand' in zdo_bytes or 
                        b'ArmorStand' in zdo_bytes or 
                        b'armorstand' in zdo_bytes):
                        try:
                            yield json.loads(zdo_bytes.decode("utf-8", errors="replace"))
                        except Exception:
                            pass


def iterate_all_zdos(file_path: Path):
    with open(file_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(0)
        if size == 0:
            return
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for match in ZDO_START_PAT.finditer(mm):
                start_pos = match.start()
                end_pos = get_zdo_bounds_fast(mm, start_pos)
                if end_pos is not None:
                    zdo_bytes = mm[start_pos:end_pos]
                    try:
                        yield json.loads(zdo_bytes.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        pass


def get_merged_properties(zdo, flat_key, by_name_key):
    res = {}
    flat_val = zdo.get(flat_key)
    if isinstance(flat_val, dict):
        res.update(flat_val)
    by_name_val = zdo.get(by_name_key)
    if isinstance(by_name_val, dict):
        res.update(by_name_val)
    return res


def serialize_property_map(prop_dict: dict) -> str:
    parts = []
    for k, v in prop_dict.items():
        if isinstance(v, dict):
            v_str = json.dumps(v, separators=(',', ':'))
        else:
            v_str = str(v)
        parts.append(f"{k}={v_str}")
    return "; ".join(parts)


def split_json(json_path: Path, breakables_path: Path, creatures_path: Path):
    b_count = 0
    c_count = 0
    with open(breakables_path, 'w', encoding='utf-8') as f_b, \
         open(creatures_path, 'w', encoding='utf-8') as f_c:
         f_b.write('{\n  "type": "DB",\n  "zdoList": {\n    "zdos": [\n')
         f_c.write('{\n  "type": "DB",\n  "zdoList": {\n    "zdos": [\n')
         
         for zdo in iterate_all_zdos(json_path):
             floats = zdo.get("floats", {})
             health_val = floats.get("health")
             max_health_val = floats.get("max_health")
             is_creature = (max_health_val is not None and 0 <= max_health_val <= 1000000)
             prefab_name = str(zdo.get("prefabName", "")).lower()
             is_stand = False
             is_health_valid = (health_val is None or (0 <= health_val <= 1000000))
             is_breakable = is_health_valid or is_stand
             
             zdo_str = json.dumps(zdo, indent=6)
             indented_zdo_str = "\n".join("      " + line for line in zdo_str.splitlines())
             
             if is_creature:
                 if c_count > 0:
                     f_c.write(",\n")
                 f_c.write(indented_zdo_str)
                 c_count += 1
             elif is_breakable:
                 if b_count > 0:
                     f_b.write(",\n")
                 f_b.write(indented_zdo_str)
                 b_count += 1
                 
         f_b.write('\n    ]\n  }\n}\n')
         f_c.write('\n    ]\n  }\n}\n')


def write_prefabs_csv(json_path: Path, output_csv: Path, extra_col=None):
    csv_headers = [
        "prefab_hash", "prefab_name", "position_x", "position_y", "position_z",
        "rotation_x", "rotation_y", "rotation_z", "rotation_w", "sector_x", "sector_y",
        "user_id", "zdo_id", "persistent", "type", "distant", "owner_revision",
        "data_revision", "user_key", "time_created", "floats", "vec3s", "quats",
        "ints", "longs", "strings", "bytes"
    ]
    if extra_col:
        csv_headers.append(extra_col)

    with open(output_csv, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=csv_headers)
        writer.writeheader()

        for zdo in iterate_all_zdos(json_path):
            prefab_hash = zdo.get("prefabHash") or zdo.get("prefab")
            prefab_name = zdo.get("prefabName") or (str(prefab_hash) if prefab_hash is not None else "")
            pos = zdo.get("position") or {}
            rot = zdo.get("rotation") or {}
            sec = zdo.get("sector") or {}

            # Robust safe retrieval
            pos_x = safe_get_coord(pos, "x", 0, "")
            pos_y = safe_get_coord(pos, "y", 1, "")
            pos_z = safe_get_coord(pos, "z", 2, "")
            
            rot_x = safe_get_coord(rot, "x", 0, "")
            rot_y = safe_get_coord(rot, "y", 1, "")
            rot_z = safe_get_coord(rot, "z", 2, "")
            rot_w = safe_get_coord(rot, "w", 3, "")
            
            sec_x = safe_get_coord(sec, "x", 0, "")
            sec_y = safe_get_coord(sec, "y", 1, "")

            floats_map = get_merged_properties(zdo, "floats", "floatsByName")
            vec3s_map = {}
            for k in ("vec3s", "vector3s"):
                val = zdo.get(k)
                if isinstance(val, dict):
                    vec3s_map.update(val)
                val_by_name = zdo.get("vector3sByName")
                if isinstance(val_by_name, dict):
                    vec3s_map.update(val_by_name)

            quats_map = get_merged_properties(zdo, "quats", "quatsByName")
            ints_map = get_merged_properties(zdo, "ints", "intsByName")
            longs_map = get_merged_properties(zdo, "longs", "longsByName")
            strings_map = get_merged_properties(zdo, "strings", "stringsByName")
            
            bytes_map = {}
            for k in ("bytes", "byteArrays"):
                val = zdo.get(k)
                if isinstance(val, dict):
                    bytes_map.update(val)
                val_by_name = zdo.get("byteArraysByName")
                if isinstance(val_by_name, dict):
                    bytes_map.update(val_by_name)

            persistent_val = zdo.get("persistent")
            persistent_str = "true" if persistent_val is True else ("false" if persistent_val is False else "")
            distant_val = zdo.get("distant")
            distant_str = "true" if distant_val is True else ("false" if distant_val is False else "")

            row_data = {
                "prefab_hash": prefab_hash if prefab_hash is not None else "",
                "prefab_name": prefab_name if prefab_name is not None else "",
                "position_x": pos_x, "position_y": pos_y, "position_z": pos_z,
                "rotation_x": rot_x, "rotation_y": rot_y, "rotation_z": rot_z, "rotation_w": rot_w,
                "sector_x": sec_x, "sector_y": sec_y,
                "user_id": zdo.get("userID", ""), "zdo_id": zdo.get("zdoID", ""),
                "persistent": persistent_str, "type": zdo.get("type", ""), "distant": distant_str,
                "owner_revision": zdo.get("ownerRevision", ""), "data_revision": zdo.get("dataRevision", ""),
                "user_key": zdo.get("userKey", ""), "time_created": zdo.get("timeCreated", ""),
                "floats": serialize_property_map(floats_map),
                "vec3s": serialize_property_map(vec3s_map),
                "quats": serialize_property_map(quats_map),
                "ints": serialize_property_map(ints_map),
                "longs": serialize_property_map(longs_map),
                "strings": serialize_property_map(strings_map),
                "bytes": serialize_property_map(bytes_map)
            }

            if extra_col:
                if extra_col == "level":
                    val = ints_map.get("level") or ints_map.get(HASH_LEVEL, "")
                elif extra_col == "creator":
                    val = longs_map.get("creator") or longs_map.get(HASH_CREATOR, "")
                else:
                    val = ""
                row_data[extra_col] = val

            writer.writerow(row_data)


def extract_items_csv(json_path: Path, output_items_csv: Path):
    csv_headers = [
        "container_prefab", "container_prefab_name", "container_x", "container_y", "container_z",
        "container_sector_x", "container_sector_y", "container_creator_id", "container_custom_name",
        "item_prefab", "item_stack", "item_durability", "item_grid_x", "item_grid_y",
        "item_quality", "item_variant", "item_crafter_id", "item_crafter_name", "item_custom_data"
    ]

    with open(output_items_csv, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=csv_headers)
        writer.writeheader()

        for zdo in iterate_zdos(json_path):
            prefab_name = str(zdo.get("prefabName", "")).lower()
            is_stand = "itemstand" in prefab_name or "armorstand" in prefab_name
            
            parsed_items = []
            
            if is_stand:
                strings_map = get_merged_properties(zdo, "strings", "stringsByName")
                ints_map = get_merged_properties(zdo, "ints", "intsByName")
                floats_map = get_merged_properties(zdo, "floats", "floatsByName")
                longs_map = get_merged_properties(zdo, "longs", "longsByName")
                
                for k, val in strings_map.items():
                    if k == "item" or (k.startswith("item_") and k[5:].isdigit()):
                        prefix = "" if k == "item" else f"{k}_"
                        
                        stack = ints_map.get(f"{prefix}stack", 1)
                        durability = floats_map.get(f"{prefix}durability", 0.0)
                        quality = ints_map.get(f"{prefix}quality", 1)
                        variant = ints_map.get(f"{prefix}variant", 0)
                        crafter_id = longs_map.get(f"{prefix}crafterID", "")
                        crafter_name = strings_map.get(f"{prefix}crafterName", "")
                        
                        parsed_items.append({
                            "prefab": val, "stack": stack, "durability": durability,
                            "x": 0, "y": 0, "quality": quality, "variant": variant,
                            "crafter_id": crafter_id, "crafter_name": crafter_name, "custom_data": {}
                        })
            else:
                items_blob = get_zdo_value(zdo, "strings", "items", HASH_ITEMS)
                if not items_blob:
                    continue
                parsed_items = parse_inventory(items_blob)
            
            if not parsed_items:
                continue

            prefab_id = zdo.get("prefab") or zdo.get("prefabHash") or ""
            pos = zdo.get("position") or {}
            sec = zdo.get("sector") or {}
            creator = get_zdo_value(zdo, "longs", "creator", HASH_CREATOR)
            
            custom_name = (
                get_zdo_value(zdo, "strings", "tag", HASH_TAG) or
                get_zdo_value(zdo, "strings", "text", HASH_TEXT) or
                get_zdo_value(zdo, "strings", "name", HASH_NAME) or
                get_zdo_value(zdo, "strings", "custom_name", HASH_CUSTOM_NAME) or ""
            )

            # Robust safe retrieval
            pos_x = safe_get_coord(pos, "x", 0, "")
            pos_y = safe_get_coord(pos, "y", 1, "")
            pos_z = safe_get_coord(pos, "z", 2, "")
            
            sec_x = safe_get_coord(sec, "x", 0, "")
            sec_y = safe_get_coord(sec, "y", 1, "")

            for item in parsed_items:
                flat_custom_data = "; ".join(f"{k}={v}" for k, v in item["custom_data"].items())

                writer.writerow({
                    "container_prefab": prefab_id,
                    "container_prefab_name": zdo.get("prefabName", ""),
                    "container_x": pos_x, "container_y": pos_y, "container_z": pos_z,
                    "container_sector_x": sec_x, "container_sector_y": sec_y,
                    "container_creator_id": creator if creator is not None else "",
                    "container_custom_name": custom_name,
                    "item_prefab": item["prefab"], "item_stack": item["stack"], "item_durability": item["durability"],
                    "item_grid_x": item["x"], "item_grid_y": item["y"],
                    "item_quality": item["quality"], "item_variant": item["variant"],
                    "item_crafter_id": item["crafter_id"], "item_crafter_name": item["crafter_name"],
                    "item_custom_data": flat_custom_data
                })


# --- POST-PROCESSING DATABASES (Originally condensed/breakables/creatures.py) ---

def load_translation_map(filepath='itemlist.csv'):
    translation_map = {}
    if not os.path.exists(filepath):
        return translation_map
    try:
        with open(filepath, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return translation_map
            cleaned_header = [col.strip() for col in header]
            if 'Item' not in cleaned_header or 'English Name' not in cleaned_header:
                return translation_map
            item_idx = cleaned_header.index('Item')
            name_idx = cleaned_header.index('English Name')
            for row in reader:
                if not row or len(row) <= max(item_idx, name_idx):
                    continue
                item_key = row[item_idx].strip()
                english_name = row[name_idx].strip()
                if item_key:
                    translation_map[item_key] = english_name
    except Exception:
        pass
    return translation_map


def condense_items_file(input_file, output_file, reference_file='itemlist.csv'):
    translation_map = load_translation_map(reference_file)
    item_counts = defaultdict(int)
    
    with open(input_file, mode='r', newline='', encoding='utf-8') as infile:
        reader = csv.reader(infile)
        try:
            header = next(reader)
        except StopIteration:
            return
        cleaned_header = [col.strip() for col in header]
        if 'item_prefab' not in cleaned_header or 'item_stack' not in cleaned_header:
            return
        prefab_index = cleaned_header.index('item_prefab')
        stack_index = cleaned_header.index('item_stack')
        
        for row in reader:
            if not row or len(row) <= max(prefab_index, stack_index):
                continue
            prefab = row[prefab_index].strip()
            if not prefab:
                continue
            stack_str = row[stack_index].strip()
            try:
                stack_val = int(stack_str) if stack_str else 0
            except ValueError:
                stack_val = 0
            item_counts[prefab] += stack_val

    with open(output_file, mode='w', newline='', encoding='utf-8') as outfile:
        writer = csv.writer(outfile)
        writer.writerow(['item_prefab', 'item_stack', 'English Name'])
        for prefab, total_stack in sorted(item_counts.items()):
            english_name = translation_map.get(prefab, '')
            writer.writerow([prefab, total_stack, english_name])


def load_item_translations(filepath='itemlist.csv'):
    translations = {}
    if not os.path.exists(filepath):
        return translations
    with open(filepath, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            item = row.get('Item', '').strip()
            english_name = row.get('English Name', '').strip()
            if item:
                translations[item] = english_name
    return translations


def load_breakables_loot(filepath='breakablesLoot.csv'):
    loot_db = {}
    if not os.path.exists(filepath):
        return None
    with open(filepath, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            prefab_name = row.get('prefab_name', '').strip()
            if prefab_name:
                loot_db[prefab_name] = row
    return loot_db


def process_single_breakables_file(input_file_path, output_path, reference_file='itemlist.csv', loot_db_file='breakablesLoot.csv'):
    translations = load_item_translations(reference_file)
    loot_db = load_breakables_loot(loot_db_file)
    if loot_db is None:
        return

    loot_totals = {}
    with open(input_file_path, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or 'prefab_name' not in reader.fieldnames or 'creator' not in reader.fieldnames:
            return

        for row in reader:
            prefab_name = row.get('prefab_name', '').strip()
            creator = row.get('creator', '').strip()
            if not prefab_name or prefab_name not in loot_db:
                continue
            
            breakable_data = loot_db[prefab_name]
            buildable_str = breakable_data.get('buildable', '').strip().upper()
            is_buildable = (buildable_str == 'TRUE')
            divisor = 3.0 if (not creator and is_buildable) else 1.0

            for i in range(1, 6):
                drop_item = breakable_data.get(f'Drop{i}', '').strip()
                drop_chance_str = breakable_data.get(f'Drop{i}Chance', '').strip()
                if drop_item:
                    try:
                        drop_chance = float(drop_chance_str) if drop_chance_str else 0.0
                    except ValueError:
                        drop_chance = 0.0
                    final_drop_amount = drop_chance / divisor
                    loot_totals[drop_item] = loot_totals.get(drop_item, 0.0) + final_drop_amount

    with open(output_path, mode='w', newline='', encoding='utf-8-sig') as out_f:
        writer = csv.writer(out_f)
        writer.writerow(['Item', 'Total Average Loot', 'English Name'])
        for item in sorted(loot_totals.keys()):
            if not item:
                continue
            total_loot = loot_totals[item]
            formatted_loot = int(total_loot) if total_loot.is_integer() else round(total_loot, 4)
            english_name = translations.get(item, "")
            writer.writerow([item, formatted_loot, english_name])


def load_enemy_loot(filepath='creatureLoot.csv'):
    loot_db = {}
    if not os.path.exists(filepath):
        return None
    with open(filepath, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            enemy_name = row.get('EnemyName', '').strip()
            if enemy_name:
                loot_db[enemy_name] = row
    return loot_db


def process_single_creatures_file(input_file_path, output_path, reference_file='itemlist.csv', loot_db_file='creatureLoot.csv'):
    translations = load_item_translations(reference_file)
    loot_db = load_enemy_loot(loot_db_file)
    if loot_db is None:
        return

    loot_totals = {}
    with open(input_file_path, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or 'prefab_name' not in reader.fieldnames or 'level' not in reader.fieldnames:
            return

        for row in reader:
            prefab_name = row.get('prefab_name', '').strip()
            level_str = row.get('level', '1').strip()
            if not prefab_name:
                continue

            try:
                level = max(1, int(level_str))
            except ValueError:
                level = 1

            if prefab_name not in loot_db:
                continue
            
            enemy_data = loot_db[prefab_name]
            scaling_str = enemy_data.get('Scaling', 'TRUE').strip().upper()
            scaling_enabled = (scaling_str != 'FALSE')
            effective_level = level if scaling_enabled else 1

            # 1. Trophy
            trophy_name = enemy_data.get('TrophyName', '').strip()
            trophy_chance_str = enemy_data.get('TrophyChance', '').strip()
            if trophy_name:
                try:
                    trophy_chance = float(trophy_chance_str) if trophy_chance_str else 0.0
                except ValueError:
                    trophy_chance = 0.0
                loot_totals[trophy_name] = loot_totals.get(trophy_name, 0.0) + trophy_chance

            # 2. Drops 1 to 5
            for i in range(1, 6):
                drop_item = enemy_data.get(f'Drop{i}', '').strip()
                drop_chance_str = enemy_data.get(f'Drop{i}Chance', '').strip()
                if drop_item:
                    try:
                        drop_chance = float(drop_chance_str) if drop_chance_str else 0.0
                    except ValueError:
                        drop_chance = 0.0
                    scaled_chance = drop_chance * (2 ** (effective_level - 1))
                    scaled_chance = min(scaled_chance, 100.0)
                    loot_totals[drop_item] = loot_totals.get(drop_item, 0.0) + scaled_chance

    with open(output_path, mode='w', newline='', encoding='utf-8-sig') as out_f:
        writer = csv.writer(out_f)
        writer.writerow(['Item', 'Total Average Loot', 'English Name'])
        for item in sorted(loot_totals.keys()):
            if not item:
                continue
            total_loot = loot_totals[item]
            formatted_loot = int(total_loot) if total_loot.is_integer() else round(total_loot, 4)
            english_name = translations.get(item, "")
            writer.writerow([item, formatted_loot, english_name])


# --- INTEGRATED PIPELINE EXECUTIVE ---

def run_full_pipeline(
    rewind_file_path, 
    output_dir, 
    prefabs_csv="prefabs.csv", 
    hexpat_file="rewind.hexpat", 
    itemlist_csv="itemlist.csv", 
    breakables_loot_csv="breakablesLoot.csv", 
    creature_loot_csv="creatureLoot.csv"
):
    os.makedirs(output_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(rewind_file_path))[0]
    
    json_path = os.path.join(output_dir, f"{base_name}.json")
    items_csv = os.path.join(output_dir, f"{base_name}_items.csv")
    break_json = os.path.join(output_dir, f"{base_name}_breakables.json")
    creat_json = os.path.join(output_dir, f"{base_name}_creatures.json")
    break_prefabs = os.path.join(output_dir, f"{base_name}_breakables_prefabs.csv")
    creat_prefabs = os.path.join(output_dir, f"{base_name}_creatures_prefabs.csv")
    
    condensed_csv = os.path.join(output_dir, f"condensed_{base_name}_items.csv")
    breakables_loot_csv_path = os.path.join(output_dir, f"breakablesLoot_{base_name}_breakables_prefabs.csv")
    creatures_loot_csv_path = os.path.join(output_dir, f"creatureLoot_{base_name}_creatures_prefabs.csv")

    # 1. Decode rewind format to JSON
    dump_rewind(rewind_file_path, prefabs_csv, json_path, hexpat_file=hexpat_file)
    
    # 2. Extract raw items
    extract_items_csv(Path(json_path), Path(items_csv))
    
    # 3. Filter JSON sets
    split_json(Path(json_path), Path(break_json), Path(creat_json))
    
    # 4. Generate Prefab tables
    write_prefabs_csv(Path(break_json), Path(break_prefabs), extra_col="creator")
    write_prefabs_csv(Path(creat_json), Path(creat_prefabs), extra_col="level")
    
    # 5. Build post-processed outputs
    condense_items_file(items_csv, condensed_csv, reference_file=itemlist_csv)
    process_single_breakables_file(break_prefabs, breakables_loot_csv_path, reference_file=itemlist_csv, loot_db_file=breakables_loot_csv)
    process_single_creatures_file(creat_prefabs, creatures_loot_csv_path, reference_file=itemlist_csv, loot_db_file=creature_loot_csv)

    # 6. Cleanup transient JSON files to free memory/disk
    for p in [json_path, break_json, creat_json]:
        if os.path.exists(p):
            os.remove(p)

    return {
        "Items Table (Raw)": items_csv,
        "Condensed Items (Processed)": condensed_csv,
        "Breakables Prefabs (Raw)": break_prefabs,
        "Breakables Loot (Processed)": breakables_loot_csv_path,
        "Creatures Prefabs (Raw)": creat_prefabs,
        "Creatures Loot (Processed)": creatures_loot_csv_path
    }