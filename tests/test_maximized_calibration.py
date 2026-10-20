"""Only synthetic pixels, temporary JSON, fake native/Tk APIs. No real windows."""
import ast
from contextlib import contextmanager
from copy import deepcopy
import ctypes as C
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from maximized_calibration import (KEY, MODE, CANVAS, REGIONS, CLEANUP_TEXT, CalibrationError,
    CalibrationStore, assert_geometry, build_calibration, crop_frame, result_report,
    screen_to_frame, validate_calibration, validate_snapshot)
from calibration_tool import CalibrationTool, main
from calibration_native import CalibrationNative, binding_from_document
from wechat_capture import PixelImage, CaptureError, WindowScope
import test_wechat_capture as capture_fixture

ROOT = Path(__file__).resolve().parents[1]
# Temp dirs live under the project (sandboxed %TEMP% failed here once); the
# folder is not part of a fresh checkout, so make it.
TMP_ROOT = ROOT / "artifacts"
TMP_ROOT.mkdir(exist_ok=True)


def snapshot(origin=(20, 30)):
    x, y = origin
    return {"window_rect": [x,y,x+170,y+120], "window_origin": [x,y], "frame_size": [170,120],
        "dpi":144, "dpi_awareness": MODE, "pmv2_verified":True,
        "dpi_verification":"AreDpiAwarenessContextsEqual(-4)", "canvas": CANVAS, "maximized":True,
        "window_monitor":"DISPLAY1", "monitors":[{"device":"DISPLAY1", "monitor_rect":[-180,-100,300,220],
            "work_rect":[-180,-100,300,200], "primary":True}],
        "binding":{"hwnd":123,"pid":456,"created":999,"class_name":"Qt51514QWindowIcon","version":"4.1.15.13"}}


def screen_boxes(s):
    x,y = s["window_origin"]
    return {name:[l+x,t+y,r+x,b+y] for name,(l,t,r,b) in {
        "messages":(8,5,165,65), "input":(8,75,115,112), "send":(122,85,165,112)}.items()}


def value(s=None):
    s = s or snapshot()
    return build_calibration(s, screen_boxes(s), empty_confirmed=True, typed_confirmed=True,
        cleanup_confirmation=CLEANUP_TEXT, borders_confirmed=True, target="小雨", clock=lambda:"offline")


def old_document():
    return {"binding":{"hwnd":123,"pid":456},"expected_title":"小雨","target_kind":"contact",
            "unrelated":{"nested":[1,"保留",False]}, "messages":{"left":11}}


class CoordinateTests(unittest.TestCase):
    def test_parameterized_known_desktop_pixels_equal_window_frame_crops(self):
        desktop_origin = (-180,-100)
        desktop = PixelImage(480,320, bytes(v for y in range(320) for x in range(480)
            for v in (x%256,y%256,(x*3+y*7)%256,255)))
        for origin in ((20,30),(-120,-80),(85,75)):
            s = snapshot(origin)
            v = value(s)
            window = desktop.crop(tuple(screen_to_frame(s["window_rect"], desktop_origin)))
            for name in REGIONS:
                with self.subTest(origin=origin, region=name):
                    direct = desktop.crop(tuple(screen_to_frame(v["regions"][name]["screen_rect"],desktop_origin)))
                    local = crop_frame(window,v,name,s)
                    self.assertEqual(local.size,direct.size)
                    self.assertEqual(local.data,direct.data)

    def test_fixed_screen_rectangle_changes_inverse_to_origin(self):
        for a,b in (((0,0),(10,20)),((-50,20),(12,-30)),((300,-100),(-20,15))):
            with self.subTest(a=a,b=b):
                first = screen_to_frame([30,40,80,90],a)
                second = screen_to_frame([30,40,80,90],b)
                self.assertEqual(second,[first[0]+a[0]-b[0],first[1]+a[1]-b[1],
                                         first[2]+a[0]-b[0],first[3]+a[1]-b[1]])

    def test_equal_translation_preserves_frame_rectangle_pure_math_only(self):
        for dx,dy in ((-999,101),(0,0),(1800,-1500)):
            self.assertEqual(screen_to_frame([20+dx,30+dy,50+dx,90+dy],[5+dx,7+dy]),[15,23,45,83])

    def test_bad_coordinates_not_clipped_or_scaled(self):
        for bad in ([1,2,1,8],[3,4,2,9],[0,0,True,9],[0,0,1.5,5],[],[1,2,3]):
            with self.subTest(bad=bad), self.assertRaises(CalibrationError):
                screen_to_frame(bad,[0,0])
        self.assertEqual(screen_to_frame([150,150,300,300],[100,100]),[50,50,200,200])

    def test_frame_cropping_uses_frame_rect_and_refuses_wrong_frame_size(self):
        v = value()
        image = Mock(size=(170,120))
        crop_frame(image,v,"input",v["snapshot"])
        image.crop.assert_called_once_with(tuple(v["regions"]["input"]["frame_rect"]))
        image.size = (171,120)
        with self.assertRaises(CalibrationError):
            crop_frame(image,v,"input",v["snapshot"])


class ValidationTests(unittest.TestCase):
    def test_geometry_dpi_monitor_maximized_and_binding_changes_invalidate(self):
        s = snapshot()
        variants=[]
        moved=snapshot((21,30)); variants.append(moved)
        for key,val in (("dpi",96),("dpi_awareness","per_monitor_v1"),("pmv2_verified",False),
                        ("maximized",False),("frame_size",[171,120])):
            changed=deepcopy(s); changed[key]=val; variants.append(changed)
        changed=deepcopy(s); changed["monitors"][0]["monitor_rect"][2]+=1; variants.append(changed)
        changed=deepcopy(s); changed["binding"]["created"]+=1; variants.append(changed)
        for current in variants:
            with self.subTest(current=current), self.assertRaises(CalibrationError):
                validate_calibration(value(s),current)

    def test_saved_screen_frame_center_and_overlap_tampering_rejected(self):
        v=value()
        for kind in ("outside","frame","center","overlap","condition","mode","origin","cleanup"):
            candidate=deepcopy(v)
            if kind=="outside": candidate["regions"]["messages"]["screen_rect"][0]=-1000
            if kind=="frame": candidate["regions"]["input"]["frame_rect"][0]+=1
            if kind=="center": candidate["regions"]["send"]["screen_center"][0]+=1
            if kind=="overlap": candidate["regions"]["send"]=deepcopy(candidate["regions"]["input"])
            if kind=="condition": candidate["regions"]["send"]["measurement_condition"]="guessed"
            if kind=="mode": candidate["snapshot"]["dpi_awareness"]="per_monitor"
            if kind=="origin": candidate["snapshot"]["window_origin"][0]+=1
            if kind=="cleanup": candidate["cleanup_confirmation"]=""
            with self.subTest(kind=kind), self.assertRaises(CalibrationError): validate_calibration(candidate)

    def test_all_human_confirmations_required_no_programmatic_cleanup_claim(self):
        kwargs=dict(empty_confirmed=True,typed_confirmed=True,borders_confirmed=True,cleanup_confirmation=CLEANUP_TEXT,target="小雨")
        for key,bad in (("empty_confirmed",False),("typed_confirmed",False),("borders_confirmed",False),
                        ("cleanup_confirmation","测试文字已清除")):
            with self.subTest(key=key), self.assertRaises(CalibrationError):
                build_calibration(snapshot(),screen_boxes(snapshot()),**dict(kwargs,**{key:bad}))
        self.assertNotIn("[20,",result_report())
        self.assertTrue(result_report().startswith("本轮是否改变了某个结论：具体变化。"))

    def test_target_binding_only_reads_named_saved_window_no_window_search(self):
        self.assertEqual(binding_from_document(old_document()),(123,456))
        # Any calibrated contact (it used to accept one hard-coded name only).
        self.assertEqual(binding_from_document(dict(old_document(),expected_title="其他人")),(123,456))
        for doc in (dict(old_document(),expected_title=""),dict(old_document(),expected_title=None),
                    dict(old_document(),target_kind="filehelper"),dict(old_document(),binding={"hwnd":True,"pid":456})):
            with self.assertRaises(CalibrationError): binding_from_document(doc)

    def test_calibration_records_the_calibrated_contact(self):
        other=build_calibration(snapshot(),screen_boxes(snapshot()),empty_confirmed=True,typed_confirmed=True,
            cleanup_confirmation=CLEANUP_TEXT,borders_confirmed=True,target="小雨",clock=lambda:"offline")
        self.assertEqual(validate_calibration(other)["confirmed_target"],"小雨")
        for bad in ("","   ","长"*81):
            with self.subTest(bad=bad), self.assertRaises(CalibrationError):
                build_calibration(snapshot(),screen_boxes(snapshot()),empty_confirmed=True,typed_confirmed=True,
                    cleanup_confirmation=CLEANUP_TEXT,borders_confirmed=True,target=bad)


class PersistenceTests(unittest.TestCase):
    def fixture(self, directory):
        path=Path(directory)/"calibration.json"
        path.write_text(json.dumps(old_document(),ensure_ascii=False,indent=3),encoding="utf-8")
        return path,CalibrationStore(path)

    def test_roundtrip_three_regions_modes_and_unrelated_fields(self):
        with tempfile.TemporaryDirectory(prefix="calibration-offline-", dir=TMP_ROOT) as directory:
            path,store=self.fixture(directory)
            v=value(); v["reloaded_borders_confirmed"]=True
            loaded=store.save(v,snapshot())
            self.assertEqual(loaded,v)
            self.assertEqual(store.load(snapshot()),v)
            doc=json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({k:x for k,x in doc.items() if k!=KEY},old_document())
            for name in REGIONS:
                self.assertEqual(loaded["regions"][name],v["regions"][name])
            self.assertEqual(loaded["snapshot"]["dpi_awareness"],MODE)

    def test_cancel_before_and_after_saved_preview_preserves_original_bytes(self):
        with tempfile.TemporaryDirectory(prefix="calibration-offline-", dir=TMP_ROOT) as directory:
            path,store=self.fixture(directory)
            raw=path.read_bytes()
            store.cancel(); self.assertEqual(path.read_bytes(),raw)
            preview=store.stage(value(),snapshot())
            self.assertEqual(preview,value())
            pending=store.pending
            self.assertTrue(pending.is_file())
            self.assertEqual(path.read_bytes(),raw)
            store.cancel()
            self.assertFalse(pending.exists())
            self.assertEqual(path.read_bytes(),raw)

    def test_concurrent_user_config_change_not_overwritten_or_restored(self):
        with tempfile.TemporaryDirectory(prefix="calibration-offline-", dir=TMP_ROOT) as directory:
            path,store=self.fixture(directory)
            store.stage(value(),snapshot())
            path.write_text('{"user_new_content":true}',encoding="utf-8")
            with self.assertRaisesRegex(CalibrationError,"configuration_changed_no_overwrite"):
                store.save(value(),snapshot())
            store.cancel()
            self.assertEqual(path.read_text(encoding="utf-8"),'{"user_new_content":true}')

    def test_invalid_save_and_geometry_mismatch_leave_old_file_untouched(self):
        with tempfile.TemporaryDirectory(prefix="calibration-offline-", dir=TMP_ROOT) as directory:
            path,store=self.fixture(directory); raw=path.read_bytes()
            for current in (snapshot((21,30)),dict(snapshot(),dpi_awareness="per_monitor_v1")):
                with self.assertRaises(CalibrationError): store.save(value(),current)
                self.assertEqual(path.read_bytes(),raw)
                self.assertEqual(list(Path(directory).glob(".calibration-*")),[])

    def test_legacy_parser_ignores_only_namespaced_new_calibration(self):
        from visual_types import Calibration, WindowBinding, Box
        old=Calibration(WindowBinding(123,456,"fixture","class",400,300,96),"profile","小雨",Box(10,10,150,40),Box(10,60,350,250),True)
        doc=old.to_dict(); doc[KEY]=value()
        self.assertEqual(Calibration.from_dict(doc),old)
        doc["unknown_typo"]=True
        with self.assertRaises(TypeError): Calibration.from_dict(doc)


class DpiAndNativeTests(unittest.TestCase):
    def test_native_snapshot_uses_capture_window_rect_and_monitor_metadata_only(self):
        backend=Mock(); native=CalibrationNative(123,456,backend=backend)
        s=snapshot()
        backend.describe.return_value=WindowScope(123,456,999,tuple(s["window_rect"]),144)
        backend.is_pmv2.return_value=True
        backend.user.IsZoomed.return_value=1
        backend.user.MonitorFromWindow.return_value=7
        def monitor(handle,pointer):
            info=pointer._obj; info.device="DISPLAY1"; info.flags=1
            info.monitor.left,info.monitor.top,info.monitor.right,info.monitor.bottom=-180,-100,300,220
            info.work.left,info.work.top,info.work.right,info.work.bottom=-180,-100,300,200
            return 1
        backend.user.GetMonitorInfoW.side_effect=monitor
        backend.user.EnumDisplayMonitors.side_effect=lambda dc,area,callback,data: callback(7,None,None,0)
        self.assertEqual(native.snapshot(),s)
        backend.describe.assert_called_once_with(123,expected_pid=456)
        backend.user.GetClientRect.assert_not_called()
        backend.user.ClientToScreen.assert_not_called()
        backend.user.PrintWindow.assert_not_called()
        backend.grab.assert_not_called()
        backend.is_pmv2.return_value=False
        with self.assertRaisesRegex(CalibrationError,"exact_pmv2_required"): native.snapshot()

    def test_per_monitor_v1_does_not_pass_as_v2(self):
        native,user,*_=capture_fixture.native_fixture()
        user.GetAwarenessFromDpiAwarenessContext.return_value=2  # Same enum in V1.
        user.AreDpiAwarenessContextsEqual.return_value=0
        user.SetThreadDpiAwarenessContext.return_value=101
        with self.assertRaisesRegex(CaptureError,"physical_dpi_context_required"):
            with native.physical_pixels(): self.fail("V1 cannot reach coordinates")
        self.assertEqual(user.SetThreadDpiAwarenessContext.call_count,2)
        user.GetWindowRect.assert_not_called()

    def test_v1_switches_to_exact_v2_and_restores_own_thread(self):
        native,user,*_=capture_fixture.native_fixture()
        user.AreDpiAwarenessContextsEqual.side_effect=[0,1]
        user.SetThreadDpiAwarenessContext.side_effect=[101,202]
        with native.physical_pixels(): pass
        self.assertEqual(user.SetThreadDpiAwarenessContext.call_args_list[0].args[0].value,C.c_void_p(-4).value)
        self.assertEqual(user.SetThreadDpiAwarenessContext.call_args.args,(101,))
        user.SetProcessDpiAwarenessContext.assert_not_called()

    def test_exact_v2_fast_path_checked_and_no_context_change(self):
        native,user,*_=capture_fixture.native_fixture()
        with native.physical_pixels(): pass
        user.AreDpiAwarenessContextsEqual.assert_called_once()
        user.SetThreadDpiAwarenessContext.assert_not_called()

    def test_overlay_api_refuses_other_process_and_color_key_or_clickthrough(self):
        backend=Mock(); native=CalibrationNative(123,456,backend=backend)
        user=backend.user
        user.GetAncestor.return_value=999
        def pid(hwnd,pointer): pointer._obj.value=os.getpid(); return 1
        user.GetWindowThreadProcessId.side_effect=pid
        user.GetWindowDpiAwarenessContext.return_value=20
        backend.is_pmv2.return_value=True
        user.SetWindowPos.return_value=1
        def area(hwnd,pointer):
            pointer._obj.left,pointer._obj.top,pointer._obj.right,pointer._obj.bottom=-180,-100,300,220
            return 1
        user.GetWindowRect.side_effect=area
        user.GetWindowLongW.return_value=0x80000
        def alpha(hwnd,key,opacity,flags): opacity._obj.value=76; flags._obj.value=2; return 1
        user.GetLayeredWindowAttributes.side_effect=alpha
        native.place_overlay(888,[-180,-100,300,220])
        self.assertEqual(user.SetWindowPos.call_args.args[0],999)
        self.assertNotEqual(user.SetWindowPos.call_args.args[0],native.hwnd)
        user.GetWindowLongW.return_value=0x80020
        with self.assertRaisesRegex(CalibrationError,"click_through"): native.place_overlay(888,[-180,-100,300,220])
        user.GetWindowThreadProcessId.side_effect=lambda hwnd,pointer:1  # Leaves PID zero.
        count=user.SetWindowPos.call_count
        with self.assertRaisesRegex(CalibrationError,"not_an_owned"): native.place_overlay(888,[-180,-100,300,220])
        self.assertEqual(user.SetWindowPos.call_count,count)


class EntryAndFlowTests(unittest.TestCase):
    def test_whole_user_workflow_with_fake_widgets_only_and_temporary_files(self):
        with tempfile.TemporaryDirectory(prefix="calibration-flow-",dir=TMP_ROOT) as directory:
            path=Path(directory)/"calibration.json"
            path.write_text(json.dumps(old_document()),encoding="utf-8")
            store=CalibrationStore(path); original=path.read_bytes()
            native=Mock(); native.snapshot.return_value=snapshot()
            root=Mock()
            with patch("tkinter.StringVar"), patch("tkinter.Label"), patch("tkinter.Button"), \
                 patch("tkinter.Entry") as entry, patch("tkinter.Frame"), patch("tkinter.Canvas"), \
                 patch("tkinter.Toplevel"), patch("tkinter.messagebox.askyesno",return_value=True), \
                 patch("tkinter.messagebox.showinfo"), patch("tkinter.messagebox.showerror") as error:
                tool=CalibrationTool(root,native,store,clock=lambda:100,report_path=Path(directory)/"result.md")
                tool.begin(); self.assertEqual(tool.phase,"await_manual_typing")
                tool.typed_ready(); self.assertEqual(tool.phase,"select")
                for box in screen_boxes(snapshot()).values():
                    native.cursor.side_effect=[tuple(box[:2]),tuple(box[2:])]
                    tool.press(None); tool.release(None)
                tool.accept_overlay(); self.assertEqual(tool.phase,"await_cleanup")
                entry.return_value.get.return_value=CLEANUP_TEXT
                tool.save_and_preview(); self.assertEqual(tool.phase,"reload")
                self.assertEqual(path.read_bytes(),original)  # Even reloaded preview isn't a commit.
                tool.accept_overlay()
                error.assert_not_called()
                self.assertTrue(tool.accepted and tool.closed)
                self.assertTrue(store.load(snapshot())["reloaded_borders_confirmed"])
                self.assertIsNone(store.pending)
                report=(Path(directory)/"result.md").read_text(encoding="utf-8")
                self.assertIn(CLEANUP_TEXT,report)
                self.assertIn("frame_rect",report)
                native.backend.grab.assert_not_called()

    def test_user_entry_establishes_pmv2_before_creating_any_tk_window(self):
        events=[]
        native,root,tool=Mock(),Mock(),Mock()
        tool.closed=True
        @contextmanager
        def context():
            events.append("pmv2_enter")
            yield
            events.append("pmv2_exit")
        native.backend.physical_pixels.side_effect=context
        native.own_window.side_effect=lambda hwnd:events.append("owned_window_v2_check")
        root.mainloop.side_effect=lambda:events.append("loop")
        def tk_root(): events.append("Tk"); return root
        with patch("calibration_tool.CalibrationStore") as store, \
             patch("calibration_native.binding_from_document",return_value=(123,456)), \
             patch("calibration_native.CalibrationNative",return_value=native), \
             patch("calibration_tool.CalibrationTool",return_value=tool), \
             patch("tkinter.Tk",side_effect=tk_root), patch("threading.Timer") as timer:
            self.assertEqual(main(["--run"]),0)
        self.assertEqual(events,["pmv2_enter","Tk","owned_window_v2_check","loop","pmv2_exit"])
        self.assertEqual(timer.call_args.args[0],600)
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()

    def test_pmv2_entry_failure_creates_no_gui_and_no_retry(self):
        native=Mock()
        native.backend.physical_pixels.return_value.__enter__=Mock(side_effect=CaptureError("physical_dpi_context_required"))
        native.backend.physical_pixels.return_value.__exit__=Mock(return_value=False)
        with patch("calibration_tool.CalibrationStore"), patch("calibration_native.binding_from_document",return_value=(123,456)), \
             patch("calibration_native.CalibrationNative",return_value=native), patch("tkinter.Tk") as root, \
             patch("threading.Timer"), patch("sys.stdout",new_callable=io.StringIO):
            self.assertEqual(main(["--run"]),2)
        root.assert_not_called()
        self.assertEqual(native.backend.physical_pixels.call_count,1)

    def test_default_cli_no_config_native_or_ui(self):
        with patch("pathlib.Path.read_bytes",side_effect=AssertionError("no read")), patch("sys.stdout",new_callable=io.StringIO) as out:
            self.assertEqual(main([]),0)
        self.assertEqual(json.loads(out.getvalue())["status"],"not_started")
        code="import sys,calibration_tool;calibration_tool.main([]);print([n for n in ('calibration_native','capture_gdi','tkinter','socket','app','chat_core') if n in sys.modules])"
        result=subprocess.run([sys.executable,"-B","-c",code],cwd=ROOT,text=True,capture_output=True,timeout=5,check=True)
        self.assertEqual(result.stdout.splitlines()[1],"[]")

    def test_new_tool_source_has_no_capture_clipboard_model_or_input_calls(self):
        banned={"PrintWindow","BitBlt","GetDC","GetWindowDC","grab","capture","SendInput","SendMessage",
                "PostMessage","SetForegroundWindow","SetFocus","SetClipboardData","OpenClipboard","keybd_event","click",
                "ReadProcessMemory","WriteProcessMemory","SetProcessDpiAwarenessContext"}
        for name in ("maximized_calibration.py","calibration_tool.py","calibration_native.py"):
            tree=ast.parse((ROOT/name).read_text(encoding="utf-8"))
            calls={node.func.attr if isinstance(node.func,ast.Attribute) else node.func.id for node in ast.walk(tree)
                   if isinstance(node,ast.Call) and isinstance(node.func,(ast.Name,ast.Attribute))}
            self.assertFalse(calls & banned,name)

    def bare_tool(self):
        tool=CalibrationTool.__new__(CalibrationTool)
        tool.root=Mock(); tool.native=Mock(); tool.snapshot=snapshot()
        tool.clock=lambda:100; tool.deadline=600; tool.closed=False
        tool.accepted=False; tool.store=Mock(); tool.messagebox=Mock()
        tool.target="小雨"
        tool.overlay=None; tool.status=Mock(); tool.regions=screen_boxes(snapshot())
        tool.empty_confirmed=tool.typed_confirmed=True
        tool.current=Mock(return_value=snapshot()); tool.show_overlay=Mock()
        return tool

    def test_missing_cleanup_cannot_stage_or_commit(self):
        tool=self.bare_tool(); tool.phase="await_cleanup"; tool.cleanup_entry=Mock()
        tool.cleanup_entry.get.return_value=""
        tool.fail=Mock()
        tool.save_and_preview()
        tool.store.stage.assert_not_called(); tool.store.save.assert_not_called()
        tool.fail.assert_called_once_with("cleanup_not_confirmed")

    def test_preview_loaded_from_saved_candidate_commit_requires_accept(self):
        tool=self.bare_tool(); tool.phase="await_cleanup"; tool.cleanup_entry=Mock()
        tool.cleanup_entry.get.return_value=CLEANUP_TEXT
        reloaded=value(); reloaded["regions"]["messages"]["screen_rect"][0]+=1
        tool.store.stage.return_value=reloaded
        tool.save_and_preview()
        tool.store.save.assert_not_called()
        self.assertEqual(tool.regions["messages"][0],reloaded["regions"]["messages"]["screen_rect"][0])
        self.assertEqual(tool.phase,"reload")
        tool.store.save.return_value=value(); tool.draw=Mock(); tool.finish_report=Mock()
        tool.accept_overlay()
        self.assertTrue(tool.store.save.call_args.args[0]["reloaded_borders_confirmed"])
        tool.finish_report.assert_called_once()

    def test_timeout_stops_without_native_check_or_save(self):
        tool=self.bare_tool(); tool.clock=lambda:601; tool.fail=Mock()
        tool.tick()
        tool.current.assert_not_called(); tool.store.save.assert_not_called()
        tool.fail.assert_called_once_with("timeout_or_geometry_dpi_monitor_changed")

    def test_owned_overlay_uses_uniform_alpha_not_clickthrough_color_key(self):
        tool=self.bare_tool(); tool.tk=Mock(); tool.phase="select"
        tool.regions={}
        tool.show_overlay=CalibrationTool.show_overlay.__get__(tool,CalibrationTool)
        tool.show_overlay(select=True)
        top=tool.tk.Toplevel.return_value
        self.assertIn((("-alpha",0.30),{}),[(c.args,c.kwargs) for c in top.attributes.call_args_list])
        self.assertFalse(any(c.args[0]=="-transparentcolor" for c in top.attributes.call_args_list))
        tool.native.place_overlay.assert_called_once()
        self.assertIn("<ButtonPress-1>",[c.args[0] for c in tool.canvas.bind.call_args_list])

    def test_cancel_only_discards_preview_and_never_claims_cleanup(self):
        tool=self.bare_tool()
        tool.cancel(show=False)
        tool.store.cancel.assert_called_once(); tool.store.save.assert_not_called()
        self.assertTrue(tool.closed)


if __name__=="__main__": unittest.main()
