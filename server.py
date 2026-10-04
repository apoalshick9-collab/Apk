#!/usr/bin/env python3
"""
Build server for "صانع تطبيقات ميوز" (Muse App Builder).

Compiles real signed APKs from user HTML without Gradle,
using aapt2 + d8 + apksigner directly.

Env:
  DATA_DIR         - where builds + persistent keystore live (default ./data next to server.py)
  PORT             - listen port (default 8000)
  ANDROID_SDK_ROOT / ANDROID_HOME - Android SDK location (fallback ~/workspace/.tooling/android-sdk)
"""

import html
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "template"
SPLASH_DIR = TEMPLATE_DIR / "splash"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))

VERSION = "1.0"
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # 50MB zip-bomb guard

app = FastAPI(title="Muse App Builder Server", version=VERSION)


# ---------------------------------------------------------------- SDK lookup
def find_toolchain():
    sdk = (
        os.environ.get("ANDROID_SDK_ROOT")
        or os.environ.get("ANDROID_HOME")
        or ("/opt/android-sdk" if os.path.isdir("/opt/android-sdk") else None)
        or os.path.expanduser("~/workspace/.tooling/android-sdk")
    )
    bt = Path(sdk) / "build-tools" / "34.0.0"
    jar = Path(sdk) / "platforms" / "android-34" / "android.jar"
    tools = {
        "aapt2": bt / "aapt2",
        "d8": bt / "d8",
        "apksigner": bt / "apksigner",
        "zipalign": bt / "zipalign",
        "android.jar": jar,
    }
    missing = [name for name, p in tools.items() if not p.exists()]
    if missing:
        raise RuntimeError(
            "Android toolchain incomplete, missing: "
            + ", ".join(missing)
            + f" (SDK looked up at {sdk})"
        )
    tools["javac"] = Path(os.environ.get("JAVA_HOME", "")) / "bin" / "javac"
    if not tools["javac"].exists():
        for cand in [
            Path.home() / "workspace" / ".tooling" / "jdk-17" / "bin" / "javac",
            Path("/usr/lib/jvm/java-17-openjdk-amd64/bin/javac"),
        ]:
            if cand.exists():
                tools["javac"] = cand
                break
    tools["keytool"] = tools["javac"].parent / "keytool"
    if not tools["javac"].exists():
        raise RuntimeError("javac not found (checked JAVA_HOME/bin, workspace jdk-17, system jdk-17)")
    return tools


TOOLCHAIN = find_toolchain()

MANIFEST_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="__PACKAGE__">
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
    <application
        android:label="__APP_NAME__"
        android:icon="@mipmap/ic_launcher"
        android:allowBackup="false">
        <activity
            android:name="com.htmlapp.wrapper.MainActivity"
            android:exported="true"
            android:configChanges="keyboardHidden|orientation|screenSize"
            android:theme="@android:style/Theme.Material.Light.NoActionBar">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
"""


# ------------------------------------------------------------ housekeeping
def prune_old_builds():
    """Delete build dirs older than 24h."""
    builds = DATA_DIR / "builds"
    if not builds.is_dir():
        return
    now = time.time()
    for d in builds.iterdir():
        if d.is_dir() and now - d.stat().st_mtime > 24 * 3600:
            shutil.rmtree(d, ignore_errors=True)


@app.on_event("startup")
def on_startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "builds").mkdir(parents=True, exist_ok=True)
    prune_old_builds()


# ------------------------------------------------------------------ signing
def ensure_keystore():
    """Create the persistent build keystore on first use, then reuse it."""
    ks = DATA_DIR / "build.keystore"
    pw_file = DATA_DIR / ".ks-pass"
    if ks.exists() and pw_file.exists():
        return ks, pw_file.read_text().strip()
    password = secrets.token_urlsafe(18)  # ~24 chars
    pw_file.write_text(password)
    os.chmod(pw_file, 0o600)
    if ks.exists():
        ks.unlink()
    r = subprocess.run(
        [
            str(TOOLCHAIN["keytool"]),
            "-genkeypair",
            "-keystore", str(ks),
            "-alias", "buildkey",
            "-keyalg", "RSA",
            "-keysize", "2048",
            "-validity", "10950",
            "-storepass", password,
            "-keypass", password,
            "-dname", "CN=AppBuilder",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("keytool failed: " + r.stderr[-2000:])
    return ks, password


# ------------------------------------------------------------------- errors
def arabic_fail(exc: Exception) -> HTTPException:
    msg = str(exc)
    return HTTPException(
        status_code=500,
        detail=f"فشل بناء التطبيق: {msg[-1500:]}",
    )


# ------------------------------------------------------------------- routes
@app.get("/health")
def health():
    return {"ok": True, "version": VERSION}


@app.get("/api/splashes")
def splashes():
    names_path = SPLASH_DIR / "names.json"
    if not names_path.exists():
        raise HTTPException(status_code=500, detail="ملف شاشات البداية غير موجود")
    return JSONResponse(content=__import__("json").loads(names_path.read_text(encoding="utf-8")))


@app.post("/api/build")
def build(
    app_name: str = Form(...),
    splash_id: int = Form(...),
    html_zip: UploadFile = File(...),
    icon: UploadFile = File(...),
):
    app_name = (app_name or "").strip()
    if not (1 <= len(app_name) <= 60):
        raise HTTPException(status_code=400, detail="اسم التطبيق يجب أن يكون بين 1 و 60 حرفًا")
    if not (1 <= splash_id <= 20):
        raise HTTPException(status_code=400, detail="رقم شاشة البداية غير صالح (1-20)")

    if not html_zip.filename or not html_zip.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="ملف HTML يجب أن يكون بصيغة zip")
    icon_name = (icon.filename or "").lower()
    if not (icon_name.endswith(".png") or icon_name.endswith(".jpg") or icon_name.endswith(".jpeg")):
        raise HTTPException(status_code=400, detail="الأيقونة يجب أن تكون PNG أو JPG")

    build_id = uuid.uuid4().hex
    work = DATA_DIR / "builds" / build_id
    work.mkdir(parents=True, exist_ok=True)
    try:
        _run_build(work, app_name, splash_id, html_zip, icon)
    except HTTPException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise arabic_fail(exc)

    prune_old_builds()

    safe_name = re.sub(r"[^\w\u0600-\u06FF\-]+", "_", app_name).strip("_") or "app"
    return FileResponse(
        path=str(work / "final.apk"),
        media_type="application/vnd.android.package-archive",
        filename=f"{safe_name}_v1.apk",
    )


def _run_build(work: Path, app_name: str, splash_id: int, html_zip: UploadFile, icon: UploadFile):
    assets = work / "assets"
    res = work / "res"
    src = work / "src" / "com" / "htmlapp" / "wrapper"
    for d in (assets, res, src):
        d.mkdir(parents=True, exist_ok=True)
    www = assets / "www"
    www.mkdir(parents=True, exist_ok=True)

    # 1. Validate + extract zip
    zip_path = work / "upload.zip"
    zip_path.write_bytes(html_zip.file.read())
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                raise HTTPException(status_code=400, detail=f"اسم ملف غير مسموح داخل zip: {name}")
            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise HTTPException(status_code=400, detail="حجم الملفات داخل zip يتجاوز 50MB")
        zf.extractall(www)
    if not (www / "index.html").is_file():
        raise HTTPException(status_code=400, detail="ضع index.html في جذر الملف")
    zip_path.unlink()

    # 2. Icon: launcher densities + assets copy for splash
    try:
        img = Image.open(icon.file).convert("RGBA")
    except Exception:
        raise HTTPException(status_code=400, detail="تعذّر قراءة ملف الأيقونة")
    densities = {
        "mipmap-mdpi": 48,
        "mipmap-hdpi": 72,
        "mipmap-xhdpi": 96,
        "mipmap-xxhdpi": 144,
        "mipmap-xxxhdpi": 192,
    }
    for qual, size in densities.items():
        d = res / qual
        d.mkdir(parents=True, exist_ok=True)
        img.resize((size, size), Image.LANCZOS).save(d / "ic_launcher.png")
    img.resize((192, 192), Image.LANCZOS).save(assets / "icon.png")

    # 3. Splash
    splash_src = SPLASH_DIR / f"s{splash_id}.html"
    splash_html = splash_src.read_text(encoding="utf-8")
    splash_html = splash_html.replace("__APP_NAME__", html.escape(app_name))
    (assets / "splash.html").write_text(splash_html, encoding="utf-8")

    # 4. Manifest
    package = "com.htmlapp.a" + str(int(time.time()))
    manifest = MANIFEST_TEMPLATE.replace("__PACKAGE__", package).replace(
        "__APP_NAME__", xml_escape(app_name)
    )
    (work / "AndroidManifest.xml").write_text(manifest, encoding="utf-8")

    # 5. Java wrapper (versioned template copy)
    shutil.copy(TEMPLATE_DIR / "MainActivity.java", src / "MainActivity.java")

    def run(cmd, **kw):
        env = dict(os.environ)
        java_bin = str(TOOLCHAIN["javac"].parent)
        env["PATH"] = java_bin + os.pathsep + env.get("PATH", "")
        env.setdefault("JAVA_HOME", str(TOOLCHAIN["javac"].parent.parent))
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=work, env=env, **kw)
        if r.returncode != 0:
            raise RuntimeError(f"{Path(cmd[0]).name} failed:\n{r.stderr[-3000:] or r.stdout[-3000:]}")
        return r

    # 6. aapt2 compile
    run([str(TOOLCHAIN["aapt2"]), "compile", "--dir", "res", "-o", "compiled.zip"])

    # 7. aapt2 link
    run(
        [
            str(TOOLCHAIN["aapt2"]), "link",
            "-o", "base.apk",
            "-I", str(TOOLCHAIN["android.jar"]),
            "--manifest", "AndroidManifest.xml",
            "--version-code", "1",
            "--version-name", "1.0",
            "--min-sdk-version", "24",
            "--target-sdk-version", "34",
            "-A", "assets",
            "compiled.zip",
        ]
    )

    # 8. javac
    classes = work / "classes"
    classes.mkdir(exist_ok=True)
    run(
        [
            str(TOOLCHAIN["javac"]),
            "--release", "8",
            "-cp", str(TOOLCHAIN["android.jar"]),
            "-d", "classes",
            "src/com/htmlapp/wrapper/MainActivity.java",
        ]
    )

    # 9. d8
    class_files = [str(p) for p in classes.rglob("*.class")]
    if not class_files:
        raise RuntimeError("javac produced no .class files")
    dexout = work / "dexout"
    dexout.mkdir(exist_ok=True)
    run(
        [str(TOOLCHAIN["d8"]), "--lib", str(TOOLCHAIN["android.jar"]),
         "--min-api", "24", "--output", "dexout"] + class_files
    )
    dex = dexout / "classes.dex"
    if not dex.exists():
        raise RuntimeError("d8 produced no classes.dex")

    # 10. add classes.dex at APK root
    with zipfile.ZipFile(work / "base.apk", "a", zipfile.ZIP_DEFLATED) as zf:
        zf.write(dex, "classes.dex")

    # 11. zipalign
    run([str(TOOLCHAIN["zipalign"]), "-p", "4", "base.apk", "aligned.apk"])

    # 12. sign (persistent keystore)
    ks, password = ensure_keystore()
    pw_file = DATA_DIR / ".ks-pass"
    run(
        [
            str(TOOLCHAIN["apksigner"]), "sign",
            "--ks", str(ks),
            "--ks-pass", "file:" + str(pw_file),
            "--out", "final.apk",
            "aligned.apk",
        ]
    )

    # 13. verify
    badging = run([str(TOOLCHAIN["aapt2"]), "dump", "badging", "final.apk"]).stdout
    m_pkg = re.search(r"^package: name='([^']+)'", badging, re.M)
    m_label = re.search(r"^application-label:'(.*)'$", badging, re.M)
    if not m_pkg or not m_pkg.group(1).startswith("com.htmlapp.a"):
        raise RuntimeError("badging: package name mismatch:\n" + badging[:1000])
    if not m_label or m_label.group(1) != app_name:
        raise RuntimeError(
            f"badging: label mismatch (got {m_label.group(1) if m_label else None!r})"
        )
    run([str(TOOLCHAIN["apksigner"]), "verify", "final.apk"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
