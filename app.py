import os
import queue
import re
import threading
import time
import uuid
import json
import base64
import hashlib
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from flask import Flask, Response, jsonify, render_template, request

try:
    import wasmtime as _wt
    _WASMTIME_OK = True
except ImportError:
    _wt = None  # type: ignore
    _WASMTIME_OK = False

app = Flask(__name__)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
JOB_META_FILE = "job.json"

LEVEL7_WASM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "level7.wasm")
_wasm_level7 = None
_wasm_level7_init_error = ""
_wasm_lock = threading.Lock()


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def concat_file_path(path: str) -> str:
    # ffmpeg concat demuxer uses backslash escaping inside single-quoted paths.
    return path.replace("\\", "/").replace("'", "\\'")


def shell_quote(path: str) -> str:
    return '"' + path.replace('"', '\\"') + '"'


def update_state(st: dict, **updates):
    with jobs_lock:
        st.update(updates)


def get_state_value(st: dict, key: str):
    with jobs_lock:
        return st.get(key)


def request_json() -> dict:
    return request.get_json(silent=True) or {}


def headers_from_payload(data: dict) -> dict:
    headers = {
        "User-Agent": (data.get("user_agent") or DEFAULT_USER_AGENT).strip(),
    }
    referer = (data.get("referer") or "").strip()
    if referer:
        headers["Referer"] = referer
    cookie = (data.get("cookie") or "").strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _parse_stream_inf(line: str) -> dict:
    attrs = {}
    for key, value in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]+)', line):
        value = value.strip('"')
        if key == "BANDWIDTH":
            try:
                attrs[key] = int(value)
            except ValueError:
                attrs[key] = 0
        else:
            attrs[key] = value
    return attrs


def _select_child_playlist(lines: list[str], base_url: str) -> str | None:
    variants = []
    pending_attrs = None

    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF"):
            pending_attrs = _parse_stream_inf(line)
            continue
        if pending_attrs is not None and line and not line.startswith("#"):
            url = urljoin(base_url, line)
            if url.lower().split("?", 1)[0].endswith(".m3u8"):
                variants.append((pending_attrs.get("BANDWIDTH", 0), url))
            pending_attrs = None

    if not variants:
        return None
    return max(variants, key=lambda item: item[0])[1]


def make_ffmpeg_cmd(file_list: str, output_folder: str, output_mp4: str = "output.mp4") -> str:
    abs_file_list = os.path.abspath(file_list)
    abs_output = os.path.abspath(os.path.join(output_folder, output_mp4))
    return (
        f"ffmpeg -y -f concat -safe 0 "
        f"-i {shell_quote(abs_file_list)} "
        f"-c:v libx264 -c:a aac "
        f"-movflags +faststart "
        f"{shell_quote(abs_output)}"
    )


def write_ffmpeg_cmd(output_folder: str, cmd: str) -> str:
    path = os.path.join(output_folder, "ffmpeg.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(cmd)
        f.write("\n")
    return path


def make_job() -> dict:
    return {
        "state": {
            "running": False,
            "progress": 0,
            "total": 0,
            "last_output_folder": "",
            "last_file_list": "",
            "ffmpeg_cmd": "",
        },
        "log_queue": queue.Queue(),
    }


def same_job_meta(left: dict, right: dict) -> bool:
    if left.get("mode") != right.get("mode"):
        return False
    if left.get("mode") == "m3u8":
        return (
            left.get("m3u8_url") == right.get("m3u8_url")
        )
    if left.get("mode") == "template":
        return (
            left.get("url_template") == right.get("url_template")
            and left.get("start") == right.get("start")
            and left.get("end") == right.get("end")
        )
    return False


def find_existing_job_folder(output_folder: str, meta: dict) -> tuple[str, str] | None:
    if not os.path.isdir(output_folder):
        return None

    for name in os.listdir(output_folder):
        folder = os.path.join(output_folder, name)
        meta_path = os.path.join(folder, JOB_META_FILE)
        if not os.path.isdir(folder) or not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                existing_meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if same_job_meta(existing_meta, meta):
            return name, folder
    return None


def write_job_meta(output_folder: str, meta: dict):
    os.makedirs(output_folder, exist_ok=True)
    meta_path = os.path.join(output_folder, JOB_META_FILE)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def is_reusable_file(path: str) -> bool:
    try:
        return os.path.exists(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def is_probably_ts_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            data = f.read(940)
    except OSError:
        return False
    return detect_media_payload(data) is not None


def is_probably_ts_data(data: bytes) -> bool:
    if len(data) < 188 * 5 or data[0] != 0x47:
        return False

    packet_count = min(len(data) // 188, 5)
    return all(data[i * 188] == 0x47 for i in range(packet_count))


def is_probably_fmp4_data(data: bytes) -> bool:
    if len(data) < 8:
        return False
    box_type = data[4:8]
    return box_type in (b"ftyp", b"styp", b"moof")


def detect_media_format(data: bytes) -> str | None:
    if is_probably_ts_data(data):
        return "ts"
    if is_probably_fmp4_data(data):
        return "fmp4"
    return None


def detect_media_payload(data: bytes) -> tuple[str, int] | None:
    media_format = detect_media_format(data)
    if media_format:
        return media_format, 0

    max_offset = min(len(data), 512)
    for offset in range(1, max_offset):
        media_format = detect_media_format(data[offset:])
        if media_format:
            return media_format, offset
    return None


def _parse_key_attr(line: str, base_url: str) -> dict | None:
    m = re.search(r'METHOD=([^,\s]+)', line)
    if not m or m.group(1) == "NONE":
        return None
    uri_m = re.search(r'URI="([^"]+)"', line)
    if not uri_m:
        return None
    iv_m = re.search(r'IV=0x([0-9a-fA-F]+)', line, re.IGNORECASE)
    return {
        "method": m.group(1),
        "uri": urljoin(base_url, uri_m.group(1)),
        "iv_hex": iv_m.group(1) if iv_m else None,
    }


def _key_request_uri(uri: str) -> str:
    parts = urlsplit(uri)
    if parts.path not in ("/v/key7", "/v/key2"):
        return uri

    params = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "mode" for k, _ in params):
        params.append(("mode", "obfuscated"))
    params.append(("_", str(int(time.time() * 1000))))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def _b64url_decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    pad = (4 - len(s) % 4) % 4
    return base64.b64decode(s + "=" * pad)



def _wt_mem_read(memory, store, ptr: int, n: int) -> bytes:
    try:
        return bytes(memory.read(store, ptr, ptr + n))
    except AttributeError:
        import ctypes
        base = ctypes.addressof(memory.data_ptr(store).contents)
        return bytes((ctypes.c_ubyte * n).from_address(base + ptr))


def _wt_mem_write(memory, store, ptr: int, data: bytes):
    try:
        memory.write(store, data, ptr)
    except AttributeError:
        import ctypes
        import struct
        base = ctypes.addressof(memory.data_ptr(store).contents)
        arr = (ctypes.c_ubyte * len(data)).from_address(base + ptr)
        for i, b in enumerate(data):
            arr[i] = b


class _WasmLevel7:
    """wasm-bindgen bridge for level7.wasm decode_level7 function."""

    _HEAP_BASE = 132

    # String subclass that carries a reference to its source dict
    # so that Reflect.get(name_string, prop) resolves to source_dict[prop].
    class _P(str):
        _parent: dict = None

    def __init__(self, wasm_path: str):
        if not _WASMTIME_OK:
            raise ImportError("wasmtime 미설치 — pip install wasmtime")
        with open(wasm_path, "rb") as f:
            wasm_bytes = f.read()
        engine = _wt.Engine()
        self._store = _wt.Store(engine)
        module = _wt.Module(engine, wasm_bytes)
        # JS object heap
        self._heap: dict[int, object] = {
            128: None,  # undefined
            129: None,
            130: True,
            131: False,
        }
        self._next = self._HEAP_BASE
        self._free_list: list[int] = []
        self._memory = None
        imports = self._build_imports(module)
        instance = _wt.Instance(self._store, module, imports)
        exports = instance.exports(self._store)
        self._ex: dict[str, object] = {}
        for exp in module.exports:          # list attribute, not method
            try:
                self._ex[exp.name] = exports[exp.name]
            except Exception:
                pass
        if self._memory is None:
            self._memory = self._ex.get("memory")
        # __wbindgen_export = malloc, export2 = realloc, export4 = free
        self._malloc_fn = (self._ex.get("__wbindgen_malloc")
                          or self._ex.get("__wbindgen_export"))
        self._free_fn = (self._ex.get("__wbindgen_free")
                        or self._ex.get("__wbindgen_export4"))
        self._stack_fn = self._ex.get("__wbindgen_add_to_stack_pointer")
        if "decode_level7" not in self._ex:
            raise ValueError("decode_level7 미 export — WASM 파일 확인 필요")
        self._decode_fn = self._ex["decode_level7"]
        self._decrypt_segment_fn = self._ex.get("decrypt_segment")
        # detect calling convention
        self._retptr_conv = self._detect_conv(module)

    def _detect_conv(self, module) -> bool:
        for exp in module.exports:          # list attribute
            if exp.name == "decode_level7" and isinstance(exp.type, _wt.FuncType):
                ps = list(exp.type.params)
                rs = list(exp.type.results)
                return len(ps) >= 2 and len(rs) == 0
        return False

    # ── heap ─────────────────────────────────────────────────────────────────

    def _hadd(self, obj) -> int:
        if self._free_list:
            idx = self._free_list.pop()
        else:
            idx = self._next
            self._next += 1
        self._heap[idx] = obj
        return idx

    def _hget(self, idx: int):
        return self._heap.get(idx)

    def _htake(self, idx: int):
        val = self._heap.pop(idx, None)
        if idx >= self._HEAP_BASE:
            self._free_list.append(idx)
        return val

    # ── memory I/O ───────────────────────────────────────────────────────────

    def _read(self, ptr: int, n: int) -> bytes:
        return _wt_mem_read(self._memory, self._store, ptr, n)

    def _write(self, ptr: int, data: bytes):
        _wt_mem_write(self._memory, self._store, ptr, data)

    def _ri32(self, ptr: int) -> int:
        import struct
        return struct.unpack_from("<i", self._read(ptr, 4))[0]

    def _wi32(self, ptr: int, val: int):
        import struct
        self._write(ptr, struct.pack("<i", val))

    def _alloc_str(self, s: str) -> tuple[int, int]:
        data = s.encode("utf-8")
        ptr = self._malloc_fn(self._store, len(data), 1)
        self._write(ptr, data)
        return ptr, len(data)

    def _rstr(self, ptr: int, n: int) -> str:
        return self._read(ptr, n).decode("utf-8")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _reflect_get(self, obj, key):
        """Emulate JS Reflect.get(obj, key) with parent-proxy support."""
        if isinstance(obj, dict) and isinstance(key, str):
            v = obj.get(key)
            if isinstance(v, str):
                # Wrap so that subsequent Reflect.get(v, prop) resolves in obj
                p = self._P(v)
                p._parent = obj
                return p
            return v
        if isinstance(obj, self._P) and isinstance(key, str):
            # layer.name.some_prop → look in parent layer dict
            return obj._parent.get(key) if obj._parent else None
        return None

    # ── import builder ────────────────────────────────────────────────────────

    def _build_imports(self, module) -> list:
        store = self._store
        b = self

        def stub_for(ty):
            rl = len(list(ty.results))
            def _stub(*args):
                return None if rl == 0 else (0 if rl == 1 else tuple(0 for _ in range(rl)))
            return _stub

        result = []
        for imp in module.imports:          # list attribute, not method
            ty = imp.type
            nm = imp.name

            if isinstance(ty, _wt.MemoryType):
                m = _wt.Memory(store, ty)
                b._memory = m
                result.append(m)
                continue
            if isinstance(ty, _wt.TableType):
                try:
                    result.append(_wt.Table(store, ty, None))
                except Exception:
                    try:
                        result.append(_wt.Table(store, ty, _wt.Val.externref(None)))
                    except Exception:
                        result.append(_wt.Func(store, _wt.FuncType([], []), lambda: None))
                continue
            if not isinstance(ty, _wt.FuncType):
                result.append(_wt.Func(store, _wt.FuncType([], []), lambda: None))
                continue

            rl = len(list(ty.results))
            fn = None

            # ── exact wbindgen names ──────────────────────────────────────────
            if nm == "__wbindgen_object_drop_ref":
                def fn(h: int, _b=b): _b._htake(h)

            elif nm == "__wbindgen_string_new":
                def fn(ptr: int, n: int, _b=b) -> int:
                    return _b._hadd(_b._rstr(ptr, n))

            elif nm in ("__wbindgen_string_get", "__wbindgen_string_get_string"):
                def fn(rp: int, h: int, _b=b):
                    s = _b._hget(h)
                    if isinstance(s, str):
                        ptr, n = _b._alloc_str(s)
                        _b._wi32(rp, ptr); _b._wi32(rp + 4, n)
                    else:
                        _b._wi32(rp, 0); _b._wi32(rp + 4, 0)

            elif nm == "__wbindgen_json_parse":
                def fn(ptr: int, n: int, _b=b) -> int:
                    return _b._hadd(json.loads(_b._rstr(ptr, n)))

            elif nm == "__wbindgen_json_serialize":
                def fn(rp: int, h: int, _b=b):
                    s = json.dumps(_b._hget(h), separators=(",", ":"), ensure_ascii=False)
                    ptr, n = _b._alloc_str(s)
                    _b._wi32(rp, ptr); _b._wi32(rp + 4, n)

            elif nm in ("__wbindgen_throw", "__wbindgen_error_new"):
                if rl == 0:
                    def fn(ptr: int, n: int, _b=b):
                        raise RuntimeError(f"WASM: {_b._rstr(ptr, n)}")
                else:
                    def fn(ptr: int, n: int, _b=b) -> int:
                        return _b._hadd(RuntimeError(_b._rstr(ptr, n)))

            elif nm == "__wbindgen_object_clone_ref":
                def fn(h: int, _b=b) -> int: return _b._hadd(_b._hget(h))

            elif nm == "__wbindgen_number_new":
                def fn(v: float, _b=b) -> int: return _b._hadd(v)

            elif nm == "__wbindgen_cb_drop":
                def fn(h: int, _b=b) -> int: _b._htake(h); return 1

            # ── pattern matching on mangled __wbg_* names ────────────────────
            # __wbindgen_cast_* = string_new from WASM memory (ptr, len -> handle)
            elif "cast" in nm and len(list(ty.params)) == 2 and rl == 1:
                def fn(ptr: int, n: int, _b=b) -> int:
                    try:
                        return _b._hadd(_b._rstr(ptr, n))
                    except Exception:
                        return _b._hadd(_b._read(ptr, n).hex())

            # __wbg_get_* (2 params -> handle):
            #   if param2 >= _HEAP_BASE  → Reflect.get(obj, key_handle)
            #   if param2 < _HEAP_BASE   → arr[param2] direct integer index
            elif "__wbg_get_" in nm and len(list(ty.params)) == 2 and rl == 1:
                def fn(obj_h: int, param2: int, _b=b) -> int:
                    if param2 >= _b._HEAP_BASE:
                        v = _b._reflect_get(_b._hget(obj_h), _b._hget(param2))
                        return _b._hadd(v)
                    obj = _b._hget(obj_h)
                    if isinstance(obj, (list, bytes, bytearray)) and 0 <= param2 < len(obj):
                        return _b._hadd(obj[param2])
                    return _b._hadd(None)

            # __wbg_from_* = Array.from(obj) (1 param -> handle)
            elif "__wbg_from_" in nm and len(list(ty.params)) == 1:
                def fn(h: int, _b=b) -> int:
                    obj = _b._hget(h)
                    if isinstance(obj, (list, tuple, bytes, bytearray, str)):
                        return _b._hadd(list(obj))
                    return _b._hadd([])

            # __wbg___wbindgen_string_get_* (ret_ptr, handle -> void)
            elif "__wbg___wbindgen_string_get" in nm or "__wbindgen_string_get" in nm:
                def fn(rp: int, h: int, _b=b):
                    s = _b._hget(h)
                    if isinstance(s, str):
                        ptr, n = _b._alloc_str(s)
                        _b._wi32(rp, ptr); _b._wi32(rp + 4, n)
                    else:
                        _b._wi32(rp, 0); _b._wi32(rp + 4, 0)

            # __wbg___wbindgen_number_get_* (ret_ptr, handle -> void)
            elif "__wbg___wbindgen_number_get" in nm or "__wbindgen_number_get" in nm:
                def fn(rp: int, h: int, _b=b):
                    import struct
                    obj = _b._hget(h)
                    if isinstance(obj, (int, float)):
                        _b._wi32(rp, 1)
                        _b._write(rp + 8, struct.pack("<d", float(obj)))
                    else:
                        _b._wi32(rp, 0)
                        _b._write(rp + 8, struct.pack("<d", 0.0))

            # __wbg___wbindgen_is_null_* / __wbg___wbindgen_is_undefined_*
            elif "__wbindgen_is_null" in nm or "__wbindgen_is_undefined" in nm:
                def fn(h: int, _b=b) -> int: return 1 if _b._hget(h) is None else 0

            # __wbg___wbindgen_throw_*
            elif "__wbindgen_throw" in nm:
                def fn(ptr: int, n: int, _b=b):
                    raise RuntimeError(f"WASM: {_b._rstr(ptr, n)}")

            # __wbg_length_* (1 param -> int)
            elif "__wbg_length_" in nm and len(list(ty.params)) == 1:
                def fn(h: int, _b=b) -> int:
                    obj = _b._hget(h)
                    return len(obj) if isinstance(obj, (bytes, bytearray, list, str)) else 0

            # __wbg_new_with_length_* = new Uint8Array(len)
            elif "__wbg_new_with_length_" in nm:
                def fn(n: int, _b=b) -> int:
                    return _b._hadd(bytearray(n))

            # __wbg_get_index_* = typed_array[idx] -> raw int
            elif "__wbg_get_index_" in nm:
                def fn(h: int, idx: int, _b=b) -> int:
                    obj = _b._hget(h)
                    if isinstance(obj, (bytes, bytearray, list)) and 0 <= idx < len(obj):
                        return int(obj[idx]) & 0xFF
                    return 0

            # __wbg_set_index_* = typed_array[idx] = val
            elif "__wbg_set_index_" in nm:
                def fn(h: int, idx: int, val: int, _b=b):
                    obj = _b._hget(h)
                    if isinstance(obj, bytearray) and 0 <= idx < len(obj):
                        obj[idx] = val & 0xFF

            # __wbg_buffer_* = typed_array.buffer -> handle to same
            elif "__wbg_buffer_" in nm and len(list(ty.params)) == 1:
                def fn(h: int, _b=b) -> int: return _b._hadd(_b._hget(h))

            # instanceof checks
            elif "instanceof" in nm and len(list(ty.params)) == 1:
                def fn(h: int) -> int: return 1

            # subarray
            elif "subarray" in nm and len(list(ty.params)) == 3:
                def fn(h: int, s: int, e: int, _b=b) -> int:
                    obj = _b._hget(h)
                    if isinstance(obj, (bytes, bytearray)):
                        return _b._hadd(bytes(obj[s:e]))
                    return _b._hadd(None)

            # random values
            elif "getrandomvalues" in nm.lower() or "random_values" in nm.lower():
                def fn(h: int, _b=b):
                    obj = _b._hget(h)
                    if isinstance(obj, bytearray):
                        rand = os.urandom(len(obj))
                        for i, v in enumerate(rand):
                            obj[i] = v

            else:
                fn = stub_for(ty)

            result.append(_wt.Func(store, ty, fn))

        return result

    # ── public API ────────────────────────────────────────────────────────────

    def decode_level7(self, json_body: dict) -> bytes:
        store = self._store
        h = self._hadd(json_body)
        try:
            if self._retptr_conv:
                if not self._stack_fn:
                    raise RuntimeError("__wbindgen_add_to_stack_pointer not exported")
                rp = self._stack_fn(store, -16)
                try:
                    self._decode_fn(store, rp, h)
                    ptr = self._ri32(rp)
                    n = self._ri32(rp + 4)
                    data = self._read(ptr, n)
                    if self._free_fn:
                        self._free_fn(store, ptr, n, 1)
                    return data
                finally:
                    self._stack_fn(store, 16)
            else:
                rh = self._decode_fn(store, h)
                result = self._htake(rh)
                if isinstance(result, (bytes, bytearray)):
                    return bytes(result)
                raise ValueError(f"decode_level7 예상치 못한 반환 타입: {type(result)}")
        finally:
            self._htake(h)

    def decrypt_segment(self, data: bytes, key: bytes) -> bytes:
        if self._decrypt_segment_fn is None:
            raise RuntimeError("decrypt_segment 미 export")
        if not data or not key:
            raise ValueError("decrypt_segment 입력 데이터가 비어 있습니다")

        store = self._store
        data_ptr = self._malloc_fn(store, len(data), 1)
        key_ptr = self._malloc_fn(store, len(key), 1)
        self._write(data_ptr, data)
        self._write(key_ptr, key)
        rp = self._stack_fn(store, -16)
        try:
            self._decrypt_segment_fn(store, rp, data_ptr, len(data), key_ptr, len(key))
            out_ptr = self._ri32(rp)
            out_len = self._ri32(rp + 4)
            if out_ptr <= 1 or out_len <= 0:
                raise ValueError(f"decrypt_segment 반환 없음 (ptr={out_ptr}, len={out_len})")
            out = self._read(out_ptr, out_len)
            if self._free_fn:
                self._free_fn(store, out_ptr, out_len, 1)
            return out
        finally:
            self._stack_fn(store, 16)
            if self._free_fn:
                self._free_fn(store, data_ptr, len(data), 1)
                self._free_fn(store, key_ptr, len(key), 1)


def _get_wasm_level7() -> "_WasmLevel7":
    global _wasm_level7, _wasm_level7_init_error
    if _wasm_level7 is not None:
        return _wasm_level7
    if _wasm_level7_init_error:
        raise RuntimeError(_wasm_level7_init_error)
    with _wasm_lock:
        if _wasm_level7 is None and not _wasm_level7_init_error:
            try:
                _wasm_level7 = _WasmLevel7(LEVEL7_WASM_PATH)
            except Exception as e:
                _wasm_level7_init_error = str(e)
                raise
    return _wasm_level7


def _wasm_decode_key(body: dict) -> bytes:
    key = _get_wasm_level7().decode_level7(body)
    if len(key) != 16:
        raise ValueError(f"WASM decode_level7 반환 길이 오류: {len(key)} (예상 16)")
    return key


def _wasm_decrypt_segment(data: bytes, key: bytes) -> bytes:
    return _get_wasm_level7().decrypt_segment(data, key)


def _decode_obfuscated_key(resp: dict) -> bytes:
    current = _b64url_decode(resp["encrypted_key"])
    for layer in reversed(resp.get("layers", [])):
        name = layer["name"]
        if name == "final_encrypt":
            mask = _b64url_decode(layer["xor_mask"])
            pad_len = layer.get("pad_len", 0)
            xored = bytes(current[i] ^ mask[i % len(mask)] for i in range(len(current)))
            current = xored[:-pad_len] if pad_len else xored
        elif name == "decoy_shuffle":
            perm = layer["perm"]
            real_positions = layer["real_positions"]
            seg_lens = layer.get("segment_lengths", [])
            if len(current) == len(perm):
                slots = list(current)
            else:
                slots, pos = [], 0
                for sl in seg_lens:
                    slots.append(current[pos])
                    pos += 1 + sl
            inv_perm = [0] * len(perm)
            for i, p in enumerate(perm):
                inv_perm[p] = i
            intermediate = [slots[inv_perm[i]] for i in range(len(perm))]
            selected = [intermediate[p] for p in real_positions]
            if selected and isinstance(selected[0], (bytes, bytearray)):
                current = b"".join(selected)
            else:
                current = bytes(selected)
        elif name == "xor_chain":
            init = _b64url_decode(layer["init_key"])
            result = bytearray(len(current))
            for i in range(len(current)):
                result[i] = current[i] ^ (init[0] if i == 0 else current[i - 1])
            current = bytes(result)
        elif name == "segment_noise":
            perm = layer["perm"]
            n = len(perm)
            inv_perm = [0] * n
            for i, p in enumerate(perm):
                inv_perm[p] = i
            if len(current) == n:
                current = bytes(current[inv_perm[i]] for i in range(n))
            else:
                real, pos = [], 0
                for nl in layer["noise_lens"]:
                    real.append(current[pos])
                    pos += 1 + nl
                result = [0] * n
                for i, byte_val in enumerate(real):
                    result[inv_perm[i]] = byte_val
                current = bytes(result)
        elif name == "bit_rotate":
            result = bytearray(16)
            for i, rot in enumerate(layer["rotations"]):
                val = (current[i * 2] << 8) | current[i * 2 + 1]
                val = ((val >> rot) | (val << (16 - rot))) & 0xFFFF
                result[i * 2] = (val >> 8) & 0xFF
                result[i * 2 + 1] = val & 0xFF
            current = bytes(result)
        elif name == "sbox":
            table = _b64url_decode(layer["inverse_sbox"])
            current = bytes(table[b] for b in current)
        elif name == "bit_interleave":
            perm = layer["perm"]
            bits = []
            for byte in current:
                for bp in range(7, -1, -1):
                    bits.append((byte >> bp) & 1)
            out_bits = [bits[perm[i]] for i in range(128)]
            result = bytearray(16)
            for i in range(16):
                byte = 0
                for j in range(8):
                    byte = (byte << 1) | out_bits[i * 8 + j]
                result[i] = byte
            current = bytes(result)
    expected_len = resp.get("key_length", 16)
    if len(current) != expected_len:
        raise ValueError(f"복호화 결과 {len(current)}바이트 (예상 {expected_len})")
    return current


def _fetch_key(uri: str, headers: dict, cache: dict) -> bytes:
    if uri not in cache:
        resp = requests.get(_key_request_uri(uri), timeout=15, headers=headers)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        key: bytes | None = None
        wasm_err = ""
        py_err = ""

        if "json" in ct or resp.content[:1] == b"{":
            try:
                body = resp.json()
            except Exception as e:
                raise ValueError(f"키 JSON 파싱 실패: {e}") from e

            if _WASMTIME_OK and os.path.exists(LEVEL7_WASM_PATH):
                try:
                    key = _wasm_decode_key(body)
                except Exception as e:
                    wasm_err = str(e)

            if key is None or len(key) != 16:
                try:
                    key = _decode_obfuscated_key(body)
                except Exception as e:
                    py_err = str(e)
        else:
            key = resp.content

        if not isinstance(key, bytes) or len(key) != 16:
            details = "; ".join(x for x in [wasm_err, py_err] if x)
            raise ValueError(f"키 복호화 실패 (len={len(key) if isinstance(key, bytes) else 'n/a'}): {details}")

        cache[uri] = {
            "key": key,
            "wasm_err": wasm_err,
            "py_err": py_err,
            "sha256": hashlib.sha256(key).hexdigest()[:16],
        }
    return cache[uri]["key"]


def _key_debug(cache: dict, uri: str) -> str:
    meta = cache.get(uri)
    if not isinstance(meta, dict):
        return "key_meta=missing"
    return (
        f"key_sha256={meta.get('sha256')}, "
        f"wasm_err={meta.get('wasm_err') or 'none'}, "
        f"py_err={meta.get('py_err') or 'none'}"
    )


def _decrypt_aes128(data: bytes, key: bytes, iv: bytes) -> bytes:
    decrypted = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    try:
        return unpad(decrypted, AES.block_size)
    except ValueError:
        return decrypted


def _iv_candidates(key_info: dict) -> list[tuple[str, bytes]]:
    candidates = [("playlist", key_info["iv"])]
    seq = key_info.get("seq")
    if seq is not None:
        candidates.append(("sequence", int(seq).to_bytes(16, "big")))
    candidates.append(("zero", b"\x00" * 16))

    deduped = []
    seen = set()
    for name, iv in candidates:
        if iv in seen:
            continue
        seen.add(iv)
        deduped.append((name, iv))
    return deduped


def _ciphertext_candidates(data: bytes, key_info: dict) -> list[tuple[str, bytes, bytes]]:
    candidates = [(iv_name, iv, data) for iv_name, iv in _iv_candidates(key_info)]
    if len(data) > 16 and (len(data) - 16) % 16 == 0:
        candidates.insert(0, ("segment_prefix", data[:16], data[16:]))
    return candidates


def _decrypt_segment_auto(data: bytes, key_info: dict, key_cache: dict) -> tuple[bytes, str, str, str, int]:
    if len(data) % 16 != 0:
        raise ValueError(f"AES-CBC 입력 길이가 16의 배수가 아닙니다 (len={len(data)})")

    key = key_cache[key_info["uri"]]["key"]
    preview_len = min(len(data), 188 * 8)
    preview_len -= preview_len % 16
    if preview_len <= 0:
        raise ValueError("복호화할 세그먼트 데이터가 너무 짧습니다")

    attempts = []
    for iv_name, iv, ciphertext in _ciphertext_candidates(data, key_info):
        preview_source = ciphertext[:preview_len]
        preview_source = preview_source[:len(preview_source) - (len(preview_source) % 16)]
        if not preview_source:
            continue
        preview_dec = _decrypt_aes128(preview_source, key, iv)
        media = detect_media_payload(preview_dec)
        if media:
            media_format, offset = media
            decrypted = _decrypt_aes128(ciphertext, key, iv)
            if detect_media_format(decrypted[offset:offset + 188 * 8]) != media_format:
                attempts.append(f"{iv_name}:unstable@{offset}")
                continue
            return decrypted[offset:], "decoded", iv_name, media_format, offset
        attempts.append(f"{iv_name}:{preview_dec[:16].hex()}")

    if _WASMTIME_OK and os.path.exists(LEVEL7_WASM_PATH):
        try:
            wasm_dec = _wasm_decrypt_segment(data, key)
            media = detect_media_payload(wasm_dec[:188 * 8])
            if media:
                media_format, offset = media
                return wasm_dec[offset:], "wasm_segment", "wasm", media_format, offset
            attempts.append(f"wasm_segment:{wasm_dec[:16].hex()}")
        except Exception as e:
            attempts.append(f"wasm_segment_error:{e}")

    shown = ";".join(attempts[:6])
    raise ValueError(
        "복호화 결과가 TS 형식으로 보이지 않습니다 "
        f"(key_uri={key_info['uri']}, iv={key_info['iv'].hex()}, attempts={shown})"
    )



def fetch_segments(m3u8_url: str, headers: dict, output_folder: str | None = None, depth: int = 0) -> list[tuple[str, dict | None]]:
    if depth > 3:
        raise ValueError("m3u8 참조가 너무 깊습니다.")

    resp = requests.get(m3u8_url, timeout=15, headers=headers)
    resp.raise_for_status()

    if output_folder:
        filename = "playlist.m3u8" if depth == 0 else f"playlist_{depth}.m3u8"
        playlist_path = os.path.join(output_folder, filename)
        with open(playlist_path, "w", encoding="utf-8") as f:
            f.write(resp.text)

    base_url = m3u8_url.rsplit("/", 1)[0] + "/"
    lines = [line.strip() for line in resp.text.splitlines()]

    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        child_playlist = _select_child_playlist(lines, base_url)
        if not child_playlist:
            raise ValueError("master m3u8에서 하위 playlist를 찾을 수 없습니다.")
        return fetch_segments(child_playlist, headers, output_folder, depth + 1)

    seq = 0
    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            break

    segments: list[tuple[str, dict | None]] = []
    current_key_attr: dict | None = None
    for line in lines:
        if line.startswith("#EXT-X-KEY:"):
            current_key_attr = _parse_key_attr(line, base_url)
        elif line and not line.startswith("#"):
            key_info = None
            if current_key_attr and current_key_attr["method"] == "AES-128":
                iv_hex = current_key_attr["iv_hex"] or format(seq, "032x")
                key_info = {
                    "uri": current_key_attr["uri"],
                    "iv": bytes.fromhex(iv_hex.zfill(32)),
                    "seq": seq,
                }
            segments.append((urljoin(base_url, line), key_info))
            seq += 1

    return segments


def _finalize_job(st: dict, lq: queue.Queue, downloaded_files: list, total: int, output_folder: str):
    abs_folder = os.path.abspath(output_folder)
    list_path = os.path.join(abs_folder, "file_list.txt")
    if downloaded_files:
        try:
            with open(list_path, "w", encoding="utf-8") as f:
                for abs_file in downloaded_files:
                    f.write(f"file '{concat_file_path(abs_file)}'\n")
        except OSError as e:
            lq.put(f"SYS|file_list.txt를 만들 수 없습니다: {e}")
            lq.put("JOB_ERROR")
            return
        ffmpeg_cmd = make_ffmpeg_cmd(list_path, abs_folder)
        update_state(
            st,
            last_output_folder=output_folder,
            last_file_list=list_path,
            ffmpeg_cmd=ffmpeg_cmd,
        )
        try:
            ffmpeg_path = write_ffmpeg_cmd(abs_folder, ffmpeg_cmd)
        except OSError as e:
            lq.put(f"SYS|ffmpeg.txt를 만들 수 없습니다: {e}")
            lq.put("JOB_ERROR")
            return
        lq.put(f"SYS|file_list.txt 생성 완료 ({len(downloaded_files)}줄): {list_path}")
        lq.put(f"SYS|ffmpeg.txt 생성 완료: {ffmpeg_path}")
        if len(downloaded_files) < total:
            lq.put(f"SYS|일부 다운로드가 실패하여 성공한 파일 {len(downloaded_files)}개만 변환 목록에 포함했습니다.")
        lq.put(f"FFMPEG_CMD|{ffmpeg_cmd}")
    else:
        lq.put("SYS|성공한 다운로드가 없어 file_list.txt를 만들지 않았습니다.")
        lq.put("JOB_ERROR")


def download_m3u8_task(job_id: str, m3u8_url: str, output_folder: str, headers: dict):
    job = jobs.get(job_id)
    if not job:
        return
    st = job["state"]
    lq = job["log_queue"]

    update_state(st, progress=0)
    downloaded_files = []
    stopped = False

    try:
        os.makedirs(output_folder, exist_ok=True)
    except OSError as e:
        update_state(st, running=False)
        lq.put(f"SYS|저장 폴더를 만들 수 없습니다: {output_folder} — {e}")
        lq.put("JOB_ERROR")
        lq.put("DONE")
        return

    lq.put("SYS|m3u8 파싱 중...")
    try:
        segments = fetch_segments(m3u8_url, headers, output_folder)
    except (requests.exceptions.RequestException, ValueError, OSError) as e:
        update_state(st, running=False)
        lq.put(f"SYS|m3u8 다운로드 실패: {e}")
        lq.put("JOB_ERROR")
        lq.put("DONE")
        return

    total = len(segments)
    if total == 0:
        update_state(st, running=False)
        lq.put("SYS|세그먼트를 찾을 수 없습니다.")
        lq.put("JOB_ERROR")
        lq.put("DONE")
        return

    update_state(st, total=total)
    encrypted_count = sum(1 for _, k in segments if k is not None)
    if encrypted_count:
        lq.put(f"SYS|AES-128 암호화 감지 — 다운로드 시 자동 복호화합니다 ({encrypted_count}/{total}개 세그먼트)")
    lq.put(f"SYS|세그먼트 {total}개 발견")

    key_cache: dict[str, dict] = {}

    for i, (seg_url, key_info) in enumerate(segments, start=1):
        if not get_state_value(st, "running"):
            stopped = True
            lq.put("STOPPED")
            break

        filename = f"index{i}.ts"
        filepath = os.path.join(output_folder, filename)
        seg_debug = ""
        if is_reusable_file(filepath):
            if not key_info or is_probably_ts_file(filepath):
                downloaded_files.append(os.path.abspath(filepath))
                lq.put(f"SKIP|{i}|{total}|{filename}")
                update_state(st, progress=i)
                continue
            lq.put(f"SYS|기존 파일이 복호화된 TS로 보이지 않아 다시 다운로드합니다: {filename}")

        try:
            resp = requests.get(seg_url, timeout=30, headers=headers)
            resp.raise_for_status()
            data = resp.content
            seg_debug = (
                f"seg_status={resp.status_code}, seg_type={resp.headers.get('Content-Type', '(none)')}, "
                f"seg_body_len={len(data)}, seg_first16={data[:16].hex()}"
            )
            if key_info:
                _fetch_key(key_info["uri"], headers, key_cache)
                data, key_label, iv_name, media_format, media_offset = _decrypt_segment_auto(data, key_info, key_cache)
                if iv_name != "playlist" or media_offset:
                    lq.put(
                        f"SYS|복호화: key={key_label}, iv={iv_name}, "
                        f"format={media_format}, offset={media_offset}, file={filename}"
                    )
                if detect_media_format(data[:188 * 8]) != media_format:
                    raise ValueError(f"복호화 최종 검증 실패: format={media_format}, first16={data[:16].hex()}")
            with open(filepath, "wb") as f:
                f.write(data)
            downloaded_files.append(os.path.abspath(filepath))
            msg = f"OK|{i}|{total}|{filename}"
        except (requests.exceptions.RequestException, OSError, ValueError) as e:
            if key_info:
                lq.put(f"SYS|키 응답 정보: {_key_debug(key_cache, key_info['uri'])}")
            if seg_debug:
                lq.put(f"SYS|세그먼트 응답 정보: {seg_debug}")
            msg = f"ERR|{i}|{total}|{seg_url} — {e}"

        lq.put(msg)
        update_state(st, progress=i)

    update_state(st, running=False)
    if not stopped and get_state_value(st, "progress") == total:
        _finalize_job(st, lq, downloaded_files, total, output_folder)

    lq.put("DONE")


def download_task(job_id: str, url_template: str, start: int, end: int, output_folder: str, headers: dict):
    job = jobs.get(job_id)
    if not job:
        return
    st = job["state"]
    lq = job["log_queue"]

    total = end - start + 1
    update_state(
        st,
        total=total,
        progress=0,
        last_output_folder="",
        last_file_list="",
        ffmpeg_cmd="",
    )
    downloaded_files = []
    stopped = False

    try:
        os.makedirs(output_folder, exist_ok=True)
    except OSError as e:
        update_state(st, running=False)
        lq.put(f"SYS|저장 폴더를 만들 수 없습니다: {output_folder} — {e}")
        lq.put("JOB_ERROR")
        lq.put("DONE")
        return

    for i in range(start, end + 1):
        if not get_state_value(st, "running"):
            stopped = True
            lq.put("STOPPED")
            break

        current = i - start + 1
        try:
            url = url_template.format(i)
            filename = f"index{i}.ts"
        except (IndexError, KeyError, ValueError) as e:
            lq.put(f"ERR|{current}|{total}|템플릿 형식 오류: {e}")
            update_state(st, progress=current)
            continue

        filepath = os.path.join(output_folder, filename)
        if is_reusable_file(filepath):
            downloaded_files.append(os.path.abspath(filepath))
            lq.put(f"SKIP|{current}|{total}|{filename}")
            update_state(st, progress=current)
            continue

        try:
            resp = requests.get(url, timeout=30, headers=headers)
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                f.write(resp.content)
            downloaded_files.append(os.path.abspath(filepath))
            msg = f"OK|{current}|{total}|{filename}"
        except (requests.exceptions.RequestException, OSError) as e:
            msg = f"ERR|{current}|{total}|{url} — {e}"

        lq.put(msg)
        update_state(st, progress=current)

    update_state(st, running=False)
    if not stopped and get_state_value(st, "progress") == total:
        _finalize_job(st, lq, downloaded_files, total, output_folder)

    lq.put("DONE")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    data = request_json()
    m3u8_url    = (data.get("m3u8_url")    or "").strip()
    url_template = (data.get("url_template") or "").strip()

    output_folder = (data.get("output_folder") or "./downloads").strip()
    headers = headers_from_payload(data)

    if m3u8_url:
        task_target = download_m3u8_task
        task_args = (m3u8_url,)
        job_meta = {
            "mode": "m3u8",
            "m3u8_url": m3u8_url,
        }
        total = 0
    elif url_template:
        if "{" not in url_template:
            return jsonify({"error": "URL 템플릿에 {} 또는 {:06d} 자리표시자가 필요합니다."}), 400
        try:
            start_idx = int(data.get("start", 1))
            end_idx   = int(data.get("end", 10))
        except (TypeError, ValueError):
            return jsonify({"error": "시작 번호와 끝 번호는 숫자여야 합니다."}), 400
        if start_idx > end_idx:
            return jsonify({"error": "시작 번호는 끝 번호보다 클 수 없습니다."}), 400
        task_target = download_task
        task_args = (url_template, start_idx, end_idx)
        job_meta = {
            "mode": "template",
            "url_template": url_template,
            "start": start_idx,
            "end": end_idx,
        }
        total = end_idx - start_idx + 1
    else:
        return jsonify({"error": "m3u8 URL 또는 URL 템플릿을 입력하세요."}), 400

    with jobs_lock:
        existing_job = find_existing_job_folder(output_folder, job_meta)
        if existing_job:
            job_id, job_output_folder = existing_job
            reused_job = True
            if job_id in jobs and jobs[job_id]["state"]["running"]:
                return jsonify({"error": f"이미 실행 중인 작업입니다: {job_id}"}), 400
        else:
            job_id = uuid.uuid4().hex[:8]
            while job_id in jobs or os.path.exists(os.path.join(output_folder, job_id)):
                job_id = uuid.uuid4().hex[:8]
            job_output_folder = os.path.join(output_folder, job_id)
            reused_job = False
        jobs[job_id] = make_job()

    try:
        write_job_meta(job_output_folder, job_meta)
    except OSError as e:
        with jobs_lock:
            jobs.pop(job_id, None)
        return jsonify({"error": f"작업 메타데이터를 저장할 수 없습니다: {e}"}), 400

    st = jobs[job_id]["state"]
    update_state(
        st,
        running=True,
        progress=0,
        total=total,
        last_output_folder=job_output_folder,
        last_file_list="",
        ffmpeg_cmd="",
    )
    if reused_job:
        jobs[job_id]["log_queue"].put(f"SYS|기존 작업 폴더를 재사용합니다: {job_output_folder}")

    if task_target is download_m3u8_task:
        args = (job_id, task_args[0], job_output_folder, headers)
    else:
        args = (job_id, task_args[0], task_args[1], task_args[2], job_output_folder, headers)

    t = threading.Thread(target=task_target, args=args, daemon=True)
    t.start()
    return jsonify({"status": "started", "job_id": job_id})


@app.route("/stop/<job_id>", methods=["POST"])
def stop(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "작업을 찾을 수 없습니다."}), 404
    update_state(job["state"], running=False)
    return jsonify({"status": "stopped"})


@app.route("/status/<job_id>")
def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "작업을 찾을 수 없습니다."}), 404
    with jobs_lock:
        st = dict(job["state"])
    return jsonify({
        "running": st["running"],
        "progress": st["progress"],
        "total": st["total"],
        "file_list": st["last_file_list"],
        "ffmpeg_cmd": st["ffmpeg_cmd"],
    })


@app.route("/stream/<job_id>")
def stream(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return Response("job not found", status=404)

    lq = job["log_queue"]
    st = job["state"]

    def generate():
        while True:
            try:
                msg = lq.get(timeout=1.5)
                yield f"data: {msg}\n\n"
                if msg in ("DONE", "STOPPED"):
                    break
            except queue.Empty:
                if not get_state_value(st, "running"):
                    break
                yield "data: PING\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/debug/key", methods=["POST"])
def debug_key():
    data = request_json()
    key_uri = (data.get("key_uri") or "").strip()
    seg_url = (data.get("seg_url") or "").strip()
    iv_hex  = (data.get("iv_hex") or "").strip()
    if not key_uri:
        return jsonify({"error": "key_uri 필요"}), 400

    headers = headers_from_payload(data)

    out: dict = {}

    try:
        kr = requests.get(_key_request_uri(key_uri), timeout=15, headers=headers)
        kr.raise_for_status()
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"키 서버 요청 실패: {e}"}), 502

    ct = kr.headers.get("Content-Type", "")
    out["key_status"] = kr.status_code
    out["key_content_type"] = ct
    out["wasm_available"] = _WASMTIME_OK and os.path.exists(LEVEL7_WASM_PATH)

    key_bytes: bytes | None = None

    if "json" in ct or kr.content[:1] == b"{":
        try:
            body = kr.json()
        except Exception as e:
            out["key_json_parse_error"] = str(e)
            out["key_raw_hex"] = kr.content[:256].hex()
            return jsonify(out)

        out["layers"] = [l.get("name") for l in body.get("layers", [])]

        if out["wasm_available"]:
            try:
                wasm_key = _wasm_decode_key(body)
                out["wasm_key_hex"] = wasm_key.hex()
                if wasm_key != b"\x00" * 16:
                    key_bytes = wasm_key
            except Exception as e:
                out["wasm_error"] = str(e)
        else:
            out["wasm_error"] = "wasmtime 미설치" if not _WASMTIME_OK else "level7.wasm 없음"

        try:
            py_key = _decode_obfuscated_key(body)
            out["python_key_hex"] = py_key.hex()
            if key_bytes is None:
                key_bytes = py_key
        except Exception as e:
            out["python_error"] = str(e)
    else:
        key_bytes = kr.content
        out["raw_key_hex"] = key_bytes.hex()
        out["raw_key_len"] = len(key_bytes)

    if seg_url and isinstance(key_bytes, bytes) and len(key_bytes) == 16:
        try:
            sr = requests.get(seg_url, timeout=30, headers=headers)
            sr.raise_for_status()
            out["seg_status"] = sr.status_code
            ivs: list[tuple[str, bytes]] = []
            if iv_hex:
                ivs.append(("provided", bytes.fromhex(iv_hex.zfill(32))))
            ivs += [("zeros", b"\x00" * 16), ("seq_0", (0).to_bytes(16, "big"))]
            seg_results = {}
            for iv_name, iv in ivs:
                try:
                    dec = _decrypt_aes128(sr.content, key_bytes, iv)
                    seg_results[iv_name] = {
                        "sync_byte_ok": len(dec) > 0 and dec[0] == 0x47,
                        "first_32_hex": dec[:32].hex(),
                    }
                except Exception as e:
                    seg_results[iv_name] = {"error": str(e)}
            out["seg_decrypt"] = seg_results
        except requests.exceptions.RequestException as e:
            out["seg_error"] = str(e)

    return jsonify(out)


@app.route("/setup/wasm", methods=["POST"])
def setup_wasm():
    """
    Save level7.wasm from base64 string extracted from player JS.
    POST body: {"wasm_b64": "<base64 string from atob(...)>"}
    The base64 string should be the raw content inside atob("...") in the player JS.
    """
    global _wasm_level7, _wasm_level7_init_error
    data = request.get_json(silent=True) or {}
    wasm_b64 = (data.get("wasm_b64") or "").strip()
    if not wasm_b64:
        return jsonify({"error": "wasm_b64 필드 필요"}), 400
    try:
        wasm_bytes = base64.b64decode(wasm_b64)
    except Exception as e:
        return jsonify({"error": f"base64 디코딩 실패: {e}"}), 400
    if wasm_bytes[:4] != b"\x00asm":
        return jsonify({"error": f"WASM 매직 바이트 오류: {wasm_bytes[:4].hex()} (예상: 00 61 73 6d)"}), 400
    with _wasm_lock:
        try:
            with open(LEVEL7_WASM_PATH, "wb") as f:
                f.write(wasm_bytes)
            _wasm_level7 = None
            _wasm_level7_init_error = ""
        except OSError as e:
            return jsonify({"error": f"파일 저장 실패: {e}"}), 500
    try:
        _get_wasm_level7()
        return jsonify({
            "status": "ok",
            "path": LEVEL7_WASM_PATH,
            "size": len(wasm_bytes),
        })
    except Exception as e:
        return jsonify({"status": "saved_but_init_failed", "error": str(e), "path": LEVEL7_WASM_PATH})


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
