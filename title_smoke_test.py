"""Real offline OCR on generated titles only. No user image or WeChat access."""
import json
import socket
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from title_ocr import LocalTitleOCR


def run_synthetic():
    from rapidocr.utils.download_file import DownloadFile
    passed = 0
    # This is an isolated diagnostic process, not a global application patch.
    with patch("requests.sessions.Session.request", side_effect=RuntimeError("offline test")), \
         patch.object(socket.socket, "connect", side_effect=RuntimeError("offline test")), \
         patch.object(DownloadFile, "run", side_effect=RuntimeError("no model download")):
        ocr = LocalTitleOCR()
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
        for text in ("干星", "文件传输助手"):
            for dark in (False, True):
                image = Image.new("RGB", (300, 60), (31, 31, 31) if dark else "white")
                ImageDraw.Draw(image).text((12, 8), text, font=font, fill=(230, 230, 230) if dark else "black")
                try:
                    actual, _ = ocr.read_consistent(image)
                    passed += int(actual == text)
                finally:
                    image.close()
    return {"success": passed == 4, "synthetic_cases": 4, "passed": passed,
            "network_blocked": True, "user_images_read": 0, "wechat_captures": 0,
            "model_calls": 0, "wechat_inputs": 0, "wechat_sends": 0}


if __name__ == "__main__":
    print(json.dumps(run_synthetic()))
