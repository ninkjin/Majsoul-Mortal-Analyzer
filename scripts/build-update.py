"""Publisher-only tools. Produces signed assets; never uploads or publishes them."""
import argparse
import base64
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/paipu_server"))
from app_updater import REPOSITORY, ROOT_FILES, app_info, managed_file, safe_name, version_tuple


def dpapi(data, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("The default publishing key is protected by Windows DPAPI.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def default_key_path():
    return Path(os.environ["LOCALAPPDATA"]) / "MortalAnalyzer/Publisher/update-signing-key.dpapi"


def signing_key(path, create=False):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = Path(path).resolve()
    if ROOT == path or ROOT in path.parents:
        raise ValueError("Publishing private keys must be stored outside the project directory.")
    if create and not path.exists():
        key = Ed25519PrivateKey.generate()
        encrypted = dpapi(key.private_bytes_raw())
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(encrypted)
    return Ed25519PrivateKey.from_private_bytes(dpapi(path.read_bytes(), decrypt=True))


def source_files(root):
    candidates = [root / name for name in ROOT_FILES]
    candidates.extend((root / "tools").glob("*.py"))
    for directory in ("mortal", "tools/paipu_server", "scripts", "log-viewer/files"):
        candidates.extend((root / directory).rglob("*"))
    result = {}
    for path in candidates:
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if not managed_file(name):
            continue
        if path.is_symlink() or root.resolve() not in path.resolve().parents:
            raise ValueError("Refusing linked publishing source.")
        result[name] = path.read_bytes()
    return result


def build(root, output, version, key, notes="", wheel_dir=None, delete=()):
    from app_updater import verify_manifest, unpack_verified
    version_tuple(version)
    root, output = Path(root).resolve(), Path(output).resolve()
    info = app_info(root)
    if version_tuple(version) < version_tuple(info["version"]):
        raise ValueError("Cannot publish a version older than the source version.")
    public_key = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
    trust = json.loads((root / "update-trust.json").read_text(encoding="utf8"))
    if trust["public_key"] != public_key:
        raise ValueError("Signing key does not match the pinned release public key.")
    output.mkdir(parents=True, exist_ok=False)
    files = source_files(root)
    files["app-version.json"] = json.dumps({**info, "version": version}, indent=2).encode() + b"\n"
    if wheel_dir:
        wheels = list(Path(wheel_dir).glob("*.whl"))
        if not wheels:
            raise ValueError("No offline wheels found.")
        for wheel in wheels:
            name = "update-wheels/" + wheel.name
            safe_name(name)
            files[name] = wheel.read_bytes()
    archive = output / f"mortal-update-{version}.zip"
    manifest_files = {}
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for name, data in sorted(files.items()):
            package.writestr(name, data)
            manifest_files[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    now = int(time.time())
    manifest = {
        "schema": 1, "updater_protocol": 1, "version": version, "runtime_abi": info["runtime_abi"],
        "created": now, "expires": now + 180 * 86400, "notes": notes,
        "url": f"https://github.com/{REPOSITORY}/releases/download/v{version}/{archive.name}",
        "size": archive.stat().st_size, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "files": manifest_files, "delete": list(delete),
    }
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")
    signature = base64.b64encode(key.sign(raw))
    # Validate exactly what clients will consume before exposing the output artifacts.
    checked = verify_manifest(raw, signature, public_key, current={**info, "version": "0.0.0"})
    import tempfile
    with tempfile.TemporaryDirectory(prefix="mortal-package-check-") as temp:
        unpack_verified(archive, Path(temp) / "app", checked)
    (output / "mortal-update.json").write_bytes(raw)
    (output / "mortal-update.sig").write_bytes(signature)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init-key", "build", "export-key"))
    parser.add_argument("--key", type=Path, default=default_key_path())
    parser.add_argument("--version")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--notes", default="")
    parser.add_argument("--wheels", type=Path)
    parser.add_argument("--delete", action="append", default=[])
    parser.add_argument("--allow-dirty", action="store_true", help="Local testing only; never publish unreviewed changes.")
    args = parser.parse_args()
    key = signing_key(args.key, create=args.command == "init-key")
    if args.command == "init-key":
        # Only the public half is printed. Pin this value with a normal reviewed source edit.
        print(json.dumps({"schema": 1, "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode()}))
        print("Private signing key protected with Windows DPAPI at:", args.key)
    elif args.command == "export-key":
        import getpass
        from cryptography.hazmat.primitives import serialization
        if not args.output or ROOT == args.output.resolve() or ROOT in args.output.resolve().parents:
            parser.error("--output must be outside the project directory")
        password = getpass.getpass("Backup encryption passphrase (at least 12 characters): ")
        if len(password) < 12 or password != getpass.getpass("Repeat passphrase: "):
            parser.error("Passphrase is too short or does not match")
        encoded = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.BestAvailableEncryption(password.encode()))
        with args.output.open("xb") as output:
            output.write(encoded)
        print("Encrypted key backup created. Store it offline, separately from the passphrase.")
    else:
        if not args.version or not args.output:
            parser.error("build requires --version and --output")
        if (ROOT / ".git").exists() and not args.allow_dirty:
            if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip():
                parser.error("Commit and test release source first; working tree is not clean")
        manifest = build(ROOT, args.output, args.version, key, args.notes, args.wheels, args.delete)
        print(f"Signed {manifest['version']} assets prepared at {args.output}. Nothing uploaded or published.")


if __name__ == "__main__":
    main()
