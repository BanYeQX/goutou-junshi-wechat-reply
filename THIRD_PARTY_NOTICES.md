# Visual connector dependency notices

## Optional, isolated capture lab: windows-capture 2.0.1

Used only by `capture-lab.cmd`, from the separate `staging/wgc-runtime` directory.
Not installed into/replacing the product bundle. The Python wrapper and native
BGRA/window-handle path were reviewed. Runtime encoding/save/monitor capture APIs
are not invoked. Upstream package license remains in
`windows_capture-2.0.1.dist-info/licenses/LICENCE`; the wheel's own dependency
inventory remains in its `sboms/` directory.

Source: https://github.com/NiiightmareXD/windows-capture

MIT License

Copyright (c) 2023 NiiightmareXD

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

No third-party WeChat connector implementation was copied. The original wxauto4
route and version guard remain. Screenshot transport uses Windows APIs and Pillow;
Title and message OCR use local RapidOCR/ONNX Runtime weights, not a cloud OCR
service. The installed Windows.Media.Ocr adapter remains for native regression
and earlier development utilities; it is not chosen as a per-result fallback.
No Microsoft system OCR binaries, language packs or extracted DLLs are distributed.

## PyWinRT / Python projection packages 3.2.1

Reviewed: installed namespace wrapper source and public type definitions, Windows
OCR SDK contract, upstream license, and a real in-memory synthetic Chinese OCR test.
The wrapper imports the WinRT projection and calls the system OCR API; it is not a
WeChat compatibility solution. No README claim of WeChat support is relied on.

Upstream: https://github.com/pywinrt/pywinrt
License: https://github.com/pywinrt/pywinrt/blob/main/LICENSE
Documentation: https://pywinrt.readthedocs.io/en/latest/

MIT License

Copyright (c) Microsoft Corporation. All rights reserved.
Copyright (c) 2021-2025 David Lechner <david@pybricks.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Pillow

Pillow is already used by the wxauto4 dependency. The connector uses its local
in-memory image processing and Tk image display; it does not add a network
upload path. Source review found ImageGrab(bbox=...) first grabs the whole
desktop on Windows. This connector instead uses direct, validated Windows ROI
BitBlt/GetDIBits capture and does not call ImageGrab. The wheel's MIT-CMU and
bundled dependency notices are retained with --copy-metadata Pillow.
Upstream license and wheel distribution notices apply:
https://github.com/python-pillow/Pillow/blob/main/LICENSE

## Offline title/message OCR: RapidOCR 3.10.0 / ONNX Runtime 1.31.0

Reviewed the wheel's LICENSE, MODEL_LICENSES.md, model loader, local/URL input
branches, recognizer dictionary handling, ONNX session implementation and
configuration. The unmodified wheel SHA-256 matches PyPI:
`2fc34e26cd0f48514804a94f8832283039c7334076bff60768b1fed9ad068050`.

RapidOCR engineering code is copyright RapidOCR Authors, Apache-2.0. The bundled
PaddleOCR-derived weights have separate Baidu/PaddleOCR attribution, Apache-2.0.
Their complete license and model notices are retained in the distribution's
`rapidocr-3.10.0.dist-info/licenses/`. No upstream code or model is modified.

The active detection/recognition model identities are pinned in `title_ocr.py`:
PP-OCRv6_det_small.onnx and PP-OCRv6_rec_small.onnx. The adapter checks SHA-256
before initialization, specifies both local paths, forbids upstream URL/path
image inputs, and disables ONNX telemetry. Missing/corrupt assets stop instead of
downloading. The exact weights embed the character dictionary. Tests hard-block
HTTP, socket connections and the downloader; no user images are sent to them.
Model scores are not calibrated identity/accuracy probabilities.

`message_ocr.py` reuses those same verified weights, uniformly for body and
layout recognition. It preserves multiline text and repeated occurrences,
requires two fixed scales, exact text agreement, line score >=0.95 and bounded
line-box agreement. Uncertain layout lines remain located but carry no text.
It has no expected-contact/message parameter, cloud fallback, capture, input
or send operation. Body/layout rejection does not select a matching result
from another engine. The title-specific unique-line and exact-name rules are
unchanged. No new models, packages or external resources were acquired.

System-time verification uses the same pinned recognizer directly only after
two full-area passes agree on a line's geometry. A centered, narrow, unclipped
line must avoid all bubbles, have neutral pixels and an outer ring exactly
matching the ROI background. Uniform linear grayscale contrast preserves
antialiasing. Two fixed-size local recognition passes must match exactly,
each score >=0.95, and yield a complete valid timestamp. No expected time is
provided and rejected global/local text is not a fallback. The upstream OCR
call's mutable detection/classification/recognition flags are checked and
restored in finally; tests cover exceptions and a subsequent title check.
Unverified lines remain uncertain, never ordinary chat text.

ONNX Runtime is MIT; OpenCV and its bundled notices, NumPy, pyclipper, Shapely,
OmegaConf, PyYAML, Requests and their dependencies retain their upstream
distribution metadata and license files via the packaging script. Unused CUDA,
Paddle, PyTorch, MNN and OpenVINO runtimes are not installed or enabled.

Sources:
- https://pypi.org/project/rapidocr/3.10.0/
- https://github.com/RapidAI/RapidOCR
- https://github.com/RapidAI/RapidOCR/blob/main/LICENSE
- https://github.com/RapidAI/RapidOCR/blob/main/MODEL_LICENSES.md
- https://github.com/microsoft/onnxruntime

## App-local Microsoft C++ runtime collection

The packaging specification excludes the WinRT wheel's nested `msvcp140.dll`
from the final TOC only when a single root copy is present and is at least as
new. DLL bytes, installed packages, system runtimes and WeChat are not changed.
The app preloads the explicit app-local copy before WinRT/ONNX; development
uses the system copy. A conflicting already-loaded origin pauses instead of
replacing it. NumPy/Shapely's differently named private dependencies remain.
This follows Microsoft's v14 runtime compatibility requirements:
https://learn.microsoft.com/en-us/cpp/porting/binary-compat-2015-2017

`--selftest-visual-runtime` covers the production native worker using generated
pixels only, with HTTP, DNS and external socket connections blocked. The
retired Windows OCR mixed path is available separately in
`runtime_probe.py --legacy-windows`; its results are not silently counted as
production-message passes. Windows asyncio's own loopback socket pair is
allowed for that legacy path; no model service is contacted.
It does not open Store/authentication, inspect user images or capture WeChat.

## jev-chat (design and data-format reference, MIT)

`advisor.py` (军师 analysis: facts / possible intent with a model-estimated
confidence / unknowns / advice / stop condition, candidate replies) and
`chat_rhythm.py` (聊天节奏 K 线) follow ideas and the example CSV format of
jev-chat: https://github.com/shengjidaguai-china/goutoujunshi-jev-chat
(MIT; Windows integration: Copyright (c) 2026 rezoch340, Finderchangchang and
the jev-chat contributors). No source code is copied: prompts, schema,
validation and the candle implementation here are this project's own; the
CSV header `timestamp,sender,message` with `me` / `other` is kept so their
example cases import unchanged. Demo patterns in `chat_rhythm.DEMOS` are
written for this project.
