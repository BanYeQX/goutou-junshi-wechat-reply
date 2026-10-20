"""Opt-in native OCR integration probe. Generated pixels only, no UI/capture.

Stage-only reports survive native DLL crashes without dumping process memory,
Python locals, screenshots, user text or credentials.
"""
from __future__ import annotations

import json
import ipaddress
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch


def run_runtime_probe(path, repeats=3, *, legacy_windows=False):
    path = Path(path)
    started = time.monotonic()
    report = {"success": False, "stage": "starting", "frozen": bool(getattr(sys, "frozen", False)),
              "external_network_blocked": True, "user_images_read": 0, "wechat_captures": 0,
              "model_calls": 0, "wechat_inputs": 0, "wechat_sends": 0,
              "worker_runs": 0, "title_passes": 0, "body_passes": 0, "layout_passes": 0,
              "strict_message_passes": 0, "strict_message_layout_passes": 0,
              "light_message_passes": 0, "dark_message_passes": 0,
              "system_time_passes": 0,
              "worker_elapsed_ms": [],
              "legacy_windows_ocr_exercised": legacy_windows,
              "body_ocr_backend": "windows-legacy" if legacy_windows else "rapidocr-3.10.0-strict-message"}

    def stage(name):
        report["last_operation"] = name
        report["stage"] = name
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temp.replace(path)

    def worker():
        initialized = False
        worker_started = time.monotonic()
        # Alternate themes between independent worker lifetimes instead of
        # duplicating three complete fixtures in every 50-second budget.
        dark = report["worker_runs"] % 2 == 1
        try:
            stage("runtime_prepare")
            from ocr_runtime import ensure_ocr_runtime
            ensure_ocr_runtime()
            stage("winrt_import")
            from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
            init_apartment(ApartmentType.MULTI_THREADED)
            initialized = True
            body = None
            if legacy_windows:
                stage("body_init")
                from local_ocr import WindowsOCR
                body = WindowsOCR()
            stage("title_init")
            from title_ocr import LocalTitleOCR
            title = LocalTitleOCR()
            from message_ocr import LocalMessageOCR
            message = LocalMessageOCR(engine=title.engine)
            stage("synthetic_recognition")
            from PIL import Image, ImageDraw, ImageFont
            font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
            image = Image.new("RGB", (300, 60), "white")
            try:
                ImageDraw.Draw(image).text((12, 8), "文件传输助手", font=font, fill="black")
                if legacy_windows:
                    stage("synthetic_legacy_body")
                    if body.read_consistent(image)[0] != "文件传输助手":
                        raise ValueError("synthetic legacy body mismatch")
                    report["body_passes"] += 1
                    stage("synthetic_legacy_layout")
                    lines = body.scan_layout(image)
                    if not lines or any(line.uncertainty for line in lines) or "".join(line.text for line in lines) != "文件传输助手":
                        raise ValueError("synthetic legacy layout mismatch")
                    report["layout_passes"] += 1
                stage("strict_message_multiline")
                # Generated, unrelated text; no supplied screenshot or current
                # chat is read. Exercise repeated Han text and multiple lines
                # in both common themes using the production message adapter.
                for dark in (dark,):
                    sample = Image.new("RGB", (300, 130), (50, 50, 50) if dark else "white")
                    try:
                        ink = (225, 225, 225) if dark else "black"
                        draw = ImageDraw.Draw(sample)
                        draw.text((20, 12), "哈哈哈", font=font, fill=ink)
                        draw.text((20, 70), "第二行", font=font, fill=ink)
                        text, lines = message.read_consistent(sample)
                        if text != "哈哈哈\n第二行" or len(lines) != 2:
                            raise ValueError("synthetic strict message mismatch")
                        report["strict_message_passes"] += 1
                        if not legacy_windows:
                            report["body_passes"] += 1
                        stage("strict_message_layout")
                        located = message.scan_layout(sample)
                        if tuple(row.text for row in located) != ("哈哈哈", "第二行") or any(row.uncertainty for row in located):
                            raise ValueError("synthetic strict message layout mismatch")
                        report["strict_message_layout_passes"] += 1
                        if not legacy_windows:
                            report["layout_passes"] += 1
                        report["dark_message_passes" if dark else "light_message_passes"] += 1
                    finally:
                        sample.close()
                stage("synthetic_system_time")
                from message_ocr import LayoutLine
                from visual_timestamps import refine_time_lines
                from visual_recognition import timestamp
                from visual_types import Box
                time_image = Image.new("RGB", (550, 200), (30,30,30) if dark else (245,245,245))
                try:
                    time_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 20)
                    draw = ImageDraw.Draw(time_image)
                    bounds = draw.textbbox((0,0), "昨天12:34", font=time_font)
                    width, height = bounds[2]-bounds[0], bounds[3]-bounds[1]
                    left = (550-width)//2
                    draw.text((left,80-bounds[1]), "昨天12:34", font=time_font,
                              fill=(128,128,128) if dark else (140,140,140))
                    proposal = LayoutLine("", Box(left,80,left+width,80+height), "合成待局部核对",
                                          geometry_verified=True)
                    verified = refine_time_lines(time_image, (proposal,), (), message, timestamp)
                    if not verified[0].timestamp_verified or verified[0].text != "昨天12:34":
                        raise ValueError("synthetic system time mismatch")
                    report["system_time_passes"] += 1
                finally:
                    time_image.close()
                # The same engine must still perform the strict title path
                # after direct single-line recognition restored its flags.
                stage("synthetic_title_after_local_line")
                if title.read_consistent(image)[0] != "文件传输助手":
                    raise ValueError("synthetic title mismatch")
                report["title_passes"] += 1
            finally:
                image.close()
            # Drop native objects while the worker apartment is still alive.
            del message, title, body
            report["worker_runs"] += 1
        except Exception as error:
            report["error_type"] = type(error).__name__
            stage("python_error")
        finally:
            report["worker_elapsed_ms"].append(round((time.monotonic() - worker_started) * 1000))
            if initialized:
                stage("winrt_cleanup")
                uninit_apartment()

    stage("starting")
    original_connect = socket.socket.connect

    def local_asyncio_only(sock, address):
        # asyncio's Windows proactor creates an internal loopback socket pair.
        # No hostnames, DNS, external connections or HTTP are permitted.
        try:
            allowed = isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise RuntimeError("offline probe")
        return original_connect(sock, address)

    # Process-local blocks only in this diagnostic CLI branch, never in the UI.
    with patch("requests.sessions.Session.request", side_effect=RuntimeError("offline probe")), \
         patch.object(socket.socket, "connect", local_asyncio_only), \
         patch.object(socket.socket, "connect_ex", side_effect=RuntimeError("offline probe")), \
         patch("socket.getaddrinfo", side_effect=RuntimeError("offline probe")):
        for _ in range(repeats):
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            thread.join(50)
            if thread.is_alive():
                report["error_type"] = "TimeoutError"
                break
            if "error_type" in report:
                break
    report["success"] = (report["worker_runs"] == repeats and "error_type" not in report
                         and all(report[key] == repeats for key in ("title_passes", "body_passes", "layout_passes"))
                         and report["strict_message_passes"] == repeats
                         and report["strict_message_layout_passes"] == repeats
                         and report["system_time_passes"] == repeats
                         and (repeats < 2 or (report["light_message_passes"] > 0 and report["dark_message_passes"] > 0)))
    operation = report.get("last_operation", "starting")
    stage("complete" if report["success"] else "failed")
    if not report["success"]:
        report["failed_operation"] = operation
        stage("failed")
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Offline synthetic native worker check")
    parser.add_argument("--legacy-windows", action="store_true",
                        help="Separately reproduce the retired Windows OCR mixed path; not production message OCR")
    args = parser.parse_args()
    report_name = "visual_runtime_legacy_selftest.json" if args.legacy_windows else "visual_runtime_selftest.json"
    report = run_runtime_probe(Path(__file__).resolve().parent / "artifacts" / report_name,
                               legacy_windows=args.legacy_windows)
    print(json.dumps(report))
    raise SystemExit(0 if report["success"] else 1)
