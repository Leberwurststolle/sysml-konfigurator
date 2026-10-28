"""Download the Open-MBEE sysml-toolkit CLI and the SysML v2 standard library.

Everything lands in ./toolkit next to this script:

    toolkit/bin/sysmlv2(.exe)            the `sysmlv2` command line tool
    toolkit/stdlib/sysml-library.json    the OMG standard library as one bundle file

Usage:
    python setup_toolkit.py            # detect the platform automatically
    python setup_toolkit.py --force    # download again even if already installed

Behind a corporate proxy, set HTTPS_PROXY before running. If downloads are
blocked entirely, fetch the two files listed by --print-urls manually and
pass them with --from-dir.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.10.10"
RELEASE_URL = f"https://github.com/Open-MBEE/sysml-toolkit/releases/download/v{VERSION}"

# SHA256 from the release's SHA256SUMS. The wasm package is not listed there;
# its hash was recorded when this script was written.
CLI_ASSETS = {
    "aarch64-apple-darwin": ("tar.gz", "f8b05574e829090bb321eacc85b98c44e98a0dbe3d7e8eb6c1ecffd407a32c05"),
    "x86_64-apple-darwin": ("tar.gz", "33c8d3dc7339dd142eadf69ffa3b38da927435cd77394a9f4c24fc58795d7572"),
    "x86_64-pc-windows-msvc": ("zip", "151e6f08ba1285e86974d522838ee3ccee2949e7ecf097138875f123f74d3c54"),
    "x86_64-unknown-linux-gnu": ("tar.gz", "f30cf22822de38a6df5d30a18cfa2efbbd9f179f9d84956705de5793b7575db9"),
    "aarch64-unknown-linux-gnu": ("tar.gz", "dc327361a44651cd483516468b63eb12b9041439231a26772e681994627e2289"),
}
STDLIB_ASSET = (f"sysml-wasm-{VERSION}.tgz", "93d6ef0a2140fceabc3115cd95d629146bc004c3959bfa23d715cce8b06cc13c")

TOOLKIT_DIR = Path(__file__).resolve().parent / "toolkit"


def detect_target() -> str:
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform == "win32":
        # Windows on ARM runs the x64 build through emulation.
        return "x86_64-pc-windows-msvc"
    if sys.platform == "darwin":
        return "aarch64-apple-darwin" if arm else "x86_64-apple-darwin"
    if sys.platform.startswith("linux"):
        return "aarch64-unknown-linux-gnu" if arm else "x86_64-unknown-linux-gnu"
    raise SystemExit(f"Unsupported platform: {sys.platform} / {machine}")


def cli_asset_name(target: str) -> str:
    ext, _ = CLI_ASSETS[target]
    return f"sysmlv2-{VERSION}-{target}.{ext}"


def fetch(name: str, expected_sha256: str, from_dir: Path | None) -> bytes:
    if from_dir is not None:
        data = (from_dir / name).read_bytes()
    else:
        url = f"{RELEASE_URL}/{name}"
        print(f"  downloading {url}")
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
        except urllib.error.URLError as err:
            # Python often lacks the OS certificate store (python.org builds on macOS,
            # company proxies on Windows); curl uses it. The checksum below still guards the bytes.
            curl = shutil.which("curl")
            if curl is None:
                raise SystemExit(f"Download failed ({err.reason}) and curl is not available.")
            print(f"  urllib failed ({err.reason}); retrying with curl")
            data = subprocess.run([curl, "-fsSL", url], check=True, capture_output=True).stdout
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise SystemExit(f"Checksum mismatch for {name}:\n  expected {expected_sha256}\n  got      {actual}")
    return data


def install_cli(target: str, from_dir: Path | None) -> Path:
    name = cli_asset_name(target)
    data = fetch(name, CLI_ASSETS[target][1], from_dir)
    exe_name = "sysmlv2.exe" if "windows" in target else "sysmlv2"
    bin_dir = TOOLKIT_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / exe_name

    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member = next(m for m in archive.namelist() if m.endswith("/" + exe_name))
            dest.write_bytes(archive.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            member = next(m for m in archive.getmembers() if m.name.endswith("/" + exe_name))
            extracted = archive.extractfile(member)
            assert extracted is not None
            dest.write_bytes(extracted.read())
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if sys.platform == "darwin":
            # Files written by Python are normally not quarantined; this is a harmless safeguard.
            subprocess.run(["xattr", "-d", "com.apple.quarantine", str(dest)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dest


def install_stdlib(from_dir: Path | None) -> Path:
    name, sha = STDLIB_ASSET
    data = fetch(name, sha, from_dir)
    lib_dir = TOOLKIT_DIR / "stdlib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    dest = lib_dir / "sysml-library.json"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        extracted = archive.extractfile("package/stdlib/sysml-library.json.gz")
        assert extracted is not None
        dest.write_bytes(gzip.decompress(extracted.read()))
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="reinstall even if already present")
    parser.add_argument("--target", choices=sorted(CLI_ASSETS), help="override platform detection")
    parser.add_argument("--from-dir", type=Path, help="use already downloaded release files from this folder")
    parser.add_argument("--print-urls", action="store_true", help="only print the download URLs")
    args = parser.parse_args()

    target = args.target or detect_target()
    if args.print_urls:
        print(f"{RELEASE_URL}/{cli_asset_name(target)}")
        print(f"{RELEASE_URL}/{STDLIB_ASSET[0]}")
        return

    version_file = TOOLKIT_DIR / "VERSION"
    if not args.force and version_file.exists() and version_file.read_text().strip() == f"{VERSION} {target}":
        print(f"sysml-toolkit {VERSION} ({target}) is already installed in {TOOLKIT_DIR}")
        return

    if TOOLKIT_DIR.exists():
        shutil.rmtree(TOOLKIT_DIR)
    print(f"Installing sysml-toolkit {VERSION} for {target} into {TOOLKIT_DIR}")
    exe = install_cli(target, args.from_dir)
    lib = install_stdlib(args.from_dir)
    version_file.write_text(f"{VERSION} {target}\n")

    result = subprocess.run([str(exe), "--version"], capture_output=True, text=True)
    print(f"  {exe}  ->  {result.stdout.strip() or result.stderr.strip()}")
    print(f"  {lib}")
    print("Done.")


if __name__ == "__main__":
    main()
