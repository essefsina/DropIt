"""Generate installer build inputs from dropit.py's VERSION.

Run on the Windows PC from the project root (build.bat does this):
    python make_version_files.py

Outputs:
    version.iss            -> #define MyAppVersion "x.y.z"  (used by dropit.iss)
    file_version_info.txt      -> VS_VERSION_INFO for PyInstaller --version-file,
                                   so the exe shows its version in
                                   right-click -> Properties -> Details
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent


def get_version() -> str:
    text = (ROOT / "dropit.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("VERSION not found in dropit.py")
    return match.group(1)


def version_tuple(version: str) -> str:
    parts = [int(p) for p in version.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return "(" + ", ".join(str(p) for p in parts[:4]) + ")"


def main() -> None:
    version = get_version()
    vt = version_tuple(version)

    (ROOT / "version.iss").write_text(
        f'#define MyAppVersion "{version}"\n', encoding="utf-8"
    )

    info = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={vt},
    prodvers={vt},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          u'040904B0',
          [
            StringStruct(u'CompanyName', u'Essef'),
            StringStruct(u'FileDescription', u'DropIt - WiFi file transfer'),
            StringStruct(u'FileVersion', u'{version}'),
            StringStruct(u'InternalName', u'DropIt'),
            StringStruct(u'OriginalFilename', u'DropIt.exe'),
            StringStruct(u'ProductName', u'DropIt'),
            StringStruct(u'ProductVersion', u'{version}'),
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""
    (ROOT / "file_version_info.txt").write_text(info, encoding="utf-8")
    print(f"Version files generated for {version}")


if __name__ == "__main__":
    main()
