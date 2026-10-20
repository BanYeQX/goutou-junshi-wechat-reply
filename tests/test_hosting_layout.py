"""Own Tk widgets + synthetic contact data only; no capture or live launch."""
import gc
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from unittest.mock import Mock, patch
import unittest

from app import C
from hosting_ledger import Ledger
from hosting_window import HostingController, HostingWindow
import hosting_window


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.cfg = {"profiles": [{"id": "a", "name": "合成甲"}, {"id": "b", "name": "合成乙"}], "selected": "a"}
        self.ready = threading.Event()
        def loop(*_, stop_event, **__):
            self.ready.set()
            stop_event.wait(3)
            return 0
        self.runner = Mock(side_effect=loop)
        self.c = HostingController(config=lambda: self.cfg, select=lambda i: self.cfg.update(selected=i),
                                   calibration=lambda: {"title": "合成甲"},
                                   ledger=Ledger(str(Path(self.temp.name) / "ledger.jsonl")),
                                   runner=self.runner, dpi=lambda: None)
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = HostingWindow(self.root, self.c, confirm=lambda: False)

    def tearDown(self):
        self.c.stop()
        self.assertTrue(self.c.reap(4))
        if self.window.after_id:
            self.root.after_cancel(self.window.after_id)
        self.root.destroy()
        self.window = None
        self.root = None
        gc.collect()
        self.temp.cleanup()

    def test_sidebar_and_chat_panel_reuse_app_tokens_not_a_practice_editor(self):
        w = self.window
        self.assertIsInstance(w.contact_list, tk.Listbox)
        self.assertEqual(w.contact_list.get(0, "end"), ("合成甲", "合成乙"))
        self.assertEqual(w.sidebar.cget("bg"), C["panel"])
        self.assertEqual(w.main_panel.cget("bg"), C["bg"])
        self.assertEqual(w.contact_title.cget("text"), "合成甲")
        self.assertIn("不是练习", w.scope_label.cget("text"))
        self.assertEqual(w.sidebar.pack_info()["side"], "left")
        self.assertEqual(w.main_panel.pack_info()["side"], "left")
        self.runner.assert_not_called()

    def test_browsing_contact_never_starts_or_relabels_another_run(self):
        self.c.active_contact = "a"
        self.window.contact_list.selection_clear(0, "end")
        self.window.contact_list.selection_set(1)
        self.window.select_contact()
        self.assertEqual(self.window.contact_title.cget("text"), "合成甲 · 上一轮记录")
        self.assertEqual(self.window.selected_profile()["id"], "b")
        self.assertEqual(self.cfg["selected"], "a")
        self.runner.assert_not_called()
        self.window.toggle()
        self.runner.assert_not_called()  # wrong calibrated target still refuses

    def test_only_start_button_runs_and_contact_is_locked_while_running(self):
        self.window.button.invoke()
        self.assertTrue(self.ready.wait(2))
        self.window._pump()
        self.assertEqual(str(self.window.contact_list.cget("state")), "disabled")
        self.assertEqual(self.window.button.cget("text"), "停止托管")
        self.window.button.invoke()
        self.assertTrue(self.c.stop_event.is_set())
        self.assertTrue(self.c.reap(2))
        self.window._pump()
        self.assertEqual(str(self.window.contact_list.cget("state")), "normal")

    def test_no_selection_refuses_and_banner_keeps_the_reason(self):
        self.window.contact_list.selection_clear(0, "end")
        self.window.select_contact()
        self.window.button.invoke()
        self.window._pump()
        self.assertIn("未选择联系人", self.window.banner.cget("text"))
        self.runner.assert_not_called()

    def test_start_stop_control_reserved_before_expandable_chat(self):
        # Layout geometry is queried for our synthetic Tk window, not WeChat.
        self.root.geometry("980x690")
        self.root.update_idletasks()
        foot = self.window.button.master
        # Footer and chat now live in the 托管 tab's frame (军师 / 聊天节奏 are
        # sibling tabs); the ordering rule is the same inside it.
        self.assertIs(foot.master, self.window.hosting_view)
        order = self.window.hosting_view.pack_slaves()
        self.assertLess(order.index(foot), order.index(self.window.chat_panel))
        self.assertEqual(foot.pack_info()["side"], "bottom")
        self.assertEqual(self.window.history.cget("height"), 1)

    def test_tabs_switch_views_without_touching_the_runner(self):
        w = self.window
        self.assertEqual(w.view, "hosting")
        w.show_view("advisor")
        self.root.update_idletasks()
        self.assertTrue(w.advisor.frame.winfo_manager())
        self.assertFalse(w.hosting_view.winfo_manager())
        self.assertIn("合成甲", w.advisor.background.cget("text"))
        w.show_view("rhythm")
        self.assertTrue(w.rhythm.frame.winfo_manager())
        w.show_view("hosting")
        self.assertTrue(w.hosting_view.winfo_manager())
        self.runner.assert_not_called()

    def test_sidebar_text_wraps_to_the_fixed_width(self):
        # User report 16:3x: the two muted lines were clipped on both sides.
        wrapped = [w for w in self.window.sidebar.winfo_children()
                   if isinstance(w, tk.Label) and w.cget("text") in ("托管收发，随时交还给你。", "一次只托管一位，资料各自独立")]
        self.assertEqual(len(wrapped), 2)
        for label in wrapped:
            self.assertEqual(int(str(label.cget("wraplength"))), 205)

    def test_add_and_edit_contact_go_through_save(self):
        saved = []

        def save(cfg):
            saved.append(cfg)
            self.cfg.update(cfg)

        self.window.save_config = save
        dialog = self.window.profile_dialog()
        entries = [w for w in dialog.winfo_children()[0].winfo_children() if isinstance(w, tk.Entry)]
        entries[0].insert(0, "阿南")
        entries[1].delete(0, "end")          # a new contact comes with defaults
        entries[1].insert(0, "刚认识")
        save_button = [w for w in dialog.winfo_children()[0].winfo_children() if isinstance(w, tk.Button)][-1]
        save_button.invoke()
        self.assertEqual([p["name"] for p in saved[-1]["profiles"]], ["合成甲", "合成乙", "阿南"])
        self.assertEqual(self.window.contact_list.get(0, "end"), ("合成甲", "合成乙", "阿南"))
        new = self.window.selected_profile()
        self.assertEqual((new["name"], new["stage"]), ("阿南", "刚认识"))
        self.runner.assert_not_called()

        edit = self.window.profile_dialog(new)
        name_entry = [w for w in edit.winfo_children()[0].winfo_children() if isinstance(w, tk.Entry)][0]
        self.assertEqual(str(name_entry.cget("state")), "readonly")
        edit.destroy()

    def test_edit_dialog_shows_and_deletes_portrait_items(self):
        saved = []
        self.cfg["profiles"][0]["traits"] = [
            {"text": "爱用哈哈", "evidence": "哈哈哈哈", "kind": "说话方式", "added": "2026-10-20"},
            {"text": "喜欢吃火锅", "evidence": "我超爱吃火锅", "kind": "兴趣话题", "added": "2026-10-20"}]
        self.window.save_config = lambda cfg: (saved.append(cfg), self.cfg.update(cfg))
        dialog = self.window.profile_dialog(self.cfg["profiles"][0])
        form = dialog.winfo_children()[0]
        box = [w for w in form.winfo_children() if isinstance(w, tk.Frame)][-1]
        listbox = next(w for w in box.winfo_children() if isinstance(w, tk.Listbox))
        self.assertEqual(listbox.get(0, "end"), ("[说话方式] 爱用哈哈 —— “哈哈哈哈”",
                                                  "[兴趣话题] 喜欢吃火锅 —— “我超爱吃火锅”"))
        listbox.selection_set(0)
        next(w for w in box.winfo_children() if isinstance(w, tk.Button)).invoke()
        [w for w in form.winfo_children() if isinstance(w, tk.Button)][-1].invoke()   # 保存并选中
        kept = next(p for p in saved[-1]["profiles"] if p["id"] == "a")["traits"]
        self.assertEqual([t["text"] for t in kept], ["喜欢吃火锅"])

    def test_contacts_cannot_be_edited_while_hosting_runs(self):
        self.window.save_config = lambda cfg: self.fail("must not save while running")
        self.window.button.invoke()
        self.assertTrue(self.ready.wait(2))
        self.window._pump()
        self.assertEqual(str(self.window.add_button.cget("state")), "disabled")
        with patch.object(hosting_window.messagebox, "showinfo") as info:
            self.assertIsNone(self.window.profile_dialog())
        info.assert_called_once()

    def test_already_open_guard_does_not_load_real_store_or_runner(self):
        with patch.object(hosting_window, "socket") as sock, \
                patch("hosting_run.enable_pmv2", return_value="per_monitor_v2"), \
                patch.object(hosting_window.tk, "Tk") as root, \
                patch.object(hosting_window.messagebox, "showinfo"), \
                patch("chat_core.Store") as store:
            sock.socket.return_value.bind.side_effect = OSError("synthetic occupied")
            self.assertEqual(hosting_window.main([]), 1)
            store.assert_not_called()
            root.return_value.destroy.assert_called_once()
            sock.socket.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
