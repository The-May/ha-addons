#!/usr/bin/env python3
"""
OpenHASP FTP Backup - Home Assistant Addon
Backs up multiple openHASP devices via FTP, uploads zips to a target FTP server.
"""

import ftplib
import io
import json
import logging
import socket
import sys
import zipfile

# ── Logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("openhasp_backup")

# ── Config ────────────────────────────────────────────────────────
with open("/data/options.json") as f:
    OPT = json.load(f)

ftp_display_host    = OPT["ftp_display_host"]
ftp_display_user    = OPT["ftp_display_user"]
ftp_display_pass    = OPT["ftp_display_pass"]
ftp_display_port    = OPT["ftp_display_port"]
ftp_display_passive = OPT["ftp_display_passive"]
ftp_target_host     = OPT["ftp_target_host"]
ftp_target_port     = OPT["ftp_target_port"]
ftp_target_user     = OPT["ftp_target_user"]
ftp_target_pass     = OPT["ftp_target_pass"]
ftp_target_dir      = OPT["ftp_target_dir"].lstrip("/\\").rstrip("/\\").replace("\\", "/")
# ─────────────────────────────────────────────────────────────────


def open_data_connection(host: str) -> socket.socket:
    return socket.create_connection((host, ftp_display_passive), timeout=15)


def list_files(ftp: ftplib.FTP, host: str) -> list[str]:
    data_sock = open_data_connection(host)
    ftp.sendcmd("PASV")
    ftp.sendcmd("TYPE A")
    ftp.sendcmd("LIST")

    raw = b""
    while True:
        chunk = data_sock.recv(4096)
        if not chunk:
            break
        raw += chunk
    data_sock.close()

    files = []
    for entry in raw.decode("utf-8", errors="replace").splitlines():
        parts = entry.split(None, 7)
        if len(parts) < 8:
            continue
        if not entry.startswith("d") and parts[7] not in (".", ".."):
            files.append(parts[7])
    return files


def download_file(ftp: ftplib.FTP, host: str, name: str) -> bytes:
    data_sock = open_data_connection(host)
    ftp.sendcmd("PASV")
    ftp.sendcmd("TYPE I")
    ftp.sendcmd(f"RETR {name}")

    buf = io.BytesIO()
    while True:
        chunk = data_sock.recv(4096)
        if not chunk:
            break
        buf.write(chunk)
    data_sock.close()

    # Drain all pending responses (150 start + 226 complete)
    # Some openHASP devices send both on the control socket after data is done
    for _ in range(3):
        try:
            resp = ftp.getresp()
            if resp.startswith("2"):
                break  # got final success, done
        except ftplib.all_errors:
            break

    return buf.getvalue()


def build_zip(host: str, index: int) -> tuple[str, bytes] | None:
    zip_name = f"openhasp_backup_{index:02d}.zip"
    log.info("── Device %02d: %s → %s", index, host, zip_name)

    ftp = ftplib.FTP()
    try:
        ftp.connect(host, ftp_display_port, timeout=15)
        ftp.login(ftp_display_user, ftp_display_pass)
        log.info("Connected to %s", host)
    except ftplib.all_errors as e:
        log.error("Failed to connect to %s: %s", host, e)
        return None

    files = list_files(ftp, host)
    log.info("Found %d file(s) on %s", len(files), host)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            try:
                data = download_file(ftp, host, name)
                zf.writestr(name, data)
                log.info("  + %-40s %6d bytes", name, len(data))
            except Exception as e:
                log.error("  ! Failed to download %s: %s", name, e)

    ftp.close()
    return zip_name, buf.getvalue()


def ensure_target_dir(ftp: ftplib.FTP) -> None:
    parts = [p for p in ftp_target_dir.split("/") if p]
    path = ""
    for part in parts:
        path = f"{path}/{part}" if path else part
        try:
            ftp.mkd(path)
            log.info("Created directory: %s", path)
        except ftplib.error_perm:
            pass  # already exists


def upload_zip(zip_name: str, zip_bytes: bytes) -> bool:
    log.info("Uploading %s → %s:%s/%s", zip_name, ftp_target_host, ftp_target_dir, zip_name)

    ftp = ftplib.FTP()
    try:
        ftp.connect(ftp_target_host, ftp_target_port, timeout=15)
        ftp.login(ftp_target_user, ftp_target_pass)
        ftp.set_pasv(False)  # HA FTP addon only supports active mode
    except ftplib.all_errors as e:
        log.error("Failed to connect to target FTP %s: %s", ftp_target_host, e)
        return False

    ensure_target_dir(ftp)

    try:
        ftp.cwd(ftp_target_dir)
    except ftplib.all_errors as e:
        log.error("Cannot CWD to %s: %s", ftp_target_dir, e)
        ftp.quit()
        return False

    try:
        response = ftp.storbinary(f"STOR {zip_name}", io.BytesIO(zip_bytes))
        log.info("Uploaded %s (%d bytes) — %s", zip_name, len(zip_bytes), response)
    except ftplib.all_errors as e:
        log.error("Upload failed for %s: %s", zip_name, e)
        ftp.quit()
        return False

    ftp.quit()
    return True


def main():
    log.info("OpenHASP Backup starting — %d device(s)", len(ftp_display_host))
    log.info("Target: %s:%d/%s", ftp_target_host, ftp_target_port, ftp_target_dir)

    ok, fail = 0, 0
    for i, host in enumerate(ftp_display_host, start=1):
        result = build_zip(host, i)
        if result is None:
            fail += 1
            continue
        if upload_zip(*result):
            ok += 1
        else:
            fail += 1

    log.info("── Finished: %d succeeded, %d failed ──", ok, fail)
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
