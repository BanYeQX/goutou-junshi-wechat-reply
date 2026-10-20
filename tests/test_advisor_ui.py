"""军师 and 聊天节奏 panels on a hidden Tk root. Fake model, no WeChat, no network."""
import gc
import json
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from advisor_ui import AdvisorPanel
from hosting_window import ChatEntry
from rhythm_ui import RhythmPanel

PROFILE = {"id": "p1", "name": "小雨", "stage": "刚认识", "goal": "自然聊天"}
CFG = {"profiles": [PROFILE], "selected": "p1", "my_style": "短句", "my_facts": ""}
ANSWER = json.dumps({"strategy": "承接", "intent": "可能想听你夸她", "confidence": 0.62, "basis": "对方：我搞了",
                     "facts": ["她做完了"], "unknowns": ["做的什么"], "advice": "顺着接",
                     "stop_when": "她转开就别追",
                     "candidates": [{"text": "可以啊", "why": "接住", "cost": "平淡"},
                                    {"text": "牛", "why": "短", "cost": "敷衍"},
                                    {"text": "给我看看", "why": "好奇", "cost": "压力"}]},
                    ensure_ascii=False)


class Ask:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, instructions, content, *, schema, name):
        self.calls += 1
        return self.answers.pop(0)


class PanelBase(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.running = False
        self.entries = [ChatEntry("incoming", "我搞了"), ChatEntry("outgoing", "可以")]

    def tearDown(self):
        # Let worker threads finish and drop Tk objects on THIS thread.
        for thread in threading.enumerate():
            if thread is not threading.current_thread() and thread.daemon:
                thread.join(2)
        self.root.update()
        self.root.destroy()
        self.root = None
        gc.collect()

    def panel(self, ask):
        panel = AdvisorPanel(self.root, self.root, config=lambda: CFG, entries=lambda: self.entries,
                             running=lambda: self.running, ask=ask)
        panel.frame.pack()
        return panel

    def pump(self, panel, timeout=3):
        deadline = time.monotonic() + timeout
        while panel.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.root.update()

    def output(self, panel):
        return panel.output.get("1.0", "end")


class PanelTests(PanelBase):
    def test_worker_thread_holds_no_tk_object(self):
        # Closing the window mid-analysis must not let a worker drop the last
        # reference to a widget (Tcl aborts: "async handler deleted by the
        # wrong thread").
        import inspect
        from advisor_ui import AdvisorPanel as Panel
        self.assertIsInstance(inspect.getattr_static(Panel, "_worker"), staticmethod)

    def test_analysis_shows_intent_confidence_and_three_cards(self):
        panel = self.panel(Ask(ANSWER))
        self.assertIn("刚认识", panel.background.cget("text"))
        panel.set_text("我搞了")
        panel.start_analysis()
        self.pump(panel)
        text = self.output(panel)
        self.assertIn("可能想听你夸她", text)
        self.assertIn("判断把握 · 62%（模型估计）", text)
        self.assertIn("军师建议 · 承接", text)
        self.assertEqual(len(panel.card_widgets), 3)

    def test_no_usable_candidate_is_said_out_loud(self):
        empty = json.loads(ANSWER)
        empty["candidates"] = []
        panel = self.panel(Ask(json.dumps(empty, ensure_ascii=False)))
        panel.set_text("好呀好呀")
        panel.start_analysis()
        self.pump(panel)
        self.assertEqual(panel.card_widgets, [])
        self.assertIn("模型这次没有给出候选", self.output(panel))

    def test_fill_from_hosting_labels_both_sides(self):
        panel = self.panel(Ask())
        panel.fill_from_hosting()
        self.assertEqual(panel.text(), "对方：我搞了\n我：可以")

    def test_rewrite_replaces_only_that_candidate(self):
        ask = Ask(ANSWER, json.dumps({"text": "可以 挺快"}, ensure_ascii=False))
        panel = self.panel(ask)
        panel.set_text("我搞了")
        panel.start_analysis()
        self.pump(panel)
        panel.start_rewrite(0)
        self.pump(panel)
        self.assertEqual([c.text for c in panel.analysis.candidates], ["可以 挺快", "牛", "给我看看"])
        self.assertEqual(ask.calls, 2)

    def test_a_result_after_clear_is_dropped(self):
        panel = self.panel(Ask(ANSWER))
        panel.set_text("我搞了")
        panel.start_analysis()
        panel.clear()
        time.sleep(0.2)
        self.pump(panel)
        self.assertIsNone(panel.analysis)
        self.assertEqual(panel.card_widgets, [])

    def test_guarded_message_never_calls_the_model(self):
        ask = Ask()
        panel = self.panel(ask)
        panel.set_text("别再给我发消息了")
        panel.start_analysis()
        self.pump(panel)
        self.assertEqual(ask.calls, 0)
        self.assertEqual(panel.card_widgets, [])
        self.assertIn("尊重对方", self.output(panel))

    def test_without_a_model_it_says_so(self):
        panel = self.panel(None)
        panel.set_text("我搞了")
        panel.start_analysis()
        self.assertIn("模型未配置", panel.status.cget("text"))
        self.assertFalse(panel.busy)

    def test_copy_is_refused_while_hosting_runs(self):
        panel = self.panel(Ask())
        self.running = True
        with patch("advisor_ui.messagebox.showinfo") as info:
            panel.copy("可以啊")
        info.assert_called_once()
        self.running = False
        panel.copy("可以啊")
        self.assertEqual(self.root.clipboard_get(), "可以啊")


class ReadChatTests(PanelBase):
    """从微信读取: one read-only capture fills the input; analysis is a separate click."""

    def reader_panel(self, read_chat, ask=None):
        panel = AdvisorPanel(self.root, self.root, config=lambda: CFG, entries=lambda: self.entries,
                             running=lambda: self.running, ask=ask or Ask(), read_chat=read_chat)
        panel.frame.pack()
        return panel

    def test_read_fills_the_input_and_does_not_analyse(self):
        from advisor import Turn
        asked = []
        ask = Ask()
        panel = self.reader_panel(lambda name: asked.append(name) or [Turn("me", "周末吃火锅？"),
                                                                      Turn("other", "好呀好呀")], ask)
        panel.start_read()
        self.pump(panel)
        self.assertEqual(asked, ["小雨"])                   # the selected contact's name
        self.assertEqual(panel.text(), "我：周末吃火锅？\n对方：好呀好呀")
        self.assertIn("请核对", panel.status.cget("text"))
        self.assertEqual(ask.calls, 0)

    def test_read_errors_are_shown(self):
        from chat_reader import ReadError

        def refuse(name):
            raise ReadError("微信里打开的不是「小雨」的聊天")
        panel = self.reader_panel(refuse)
        panel.start_read()
        self.pump(panel)
        self.assertIn("不是「小雨」", panel.status.cget("text"))
        self.assertEqual(str(panel.read_button.cget("state")), "normal")

    def test_no_read_while_hosting_runs(self):
        calls = []
        panel = self.reader_panel(lambda name: calls.append(name) or [])
        self.running = True
        panel.start_read()
        self.assertEqual(calls, [])
        self.assertIn("托管运行中", panel.status.cget("text"))

    def test_without_a_reader_it_says_paste(self):
        panel = self.reader_panel(None)
        panel.start_read()
        self.assertIn("直接粘贴", panel.status.cget("text"))


class PortraitPanelTests(PanelBase):
    """总结对象画像: suggestions start unticked; only ticked ones are saved."""

    PORTRAIT = json.dumps({"traits": [
        {"text": "爱用哈哈", "evidence": "哈哈哈哈", "kind": "说话方式"},
        {"text": "喜欢吃火锅", "evidence": "我超爱吃火锅", "kind": "兴趣话题"}]}, ensure_ascii=False)

    def portrait_panel(self, save):
        cfg = {"profiles": [dict(PROFILE)], "selected": "p1", "my_style": "", "my_facts": ""}
        self.cfg = cfg
        panel = AdvisorPanel(self.root, self.root, config=lambda: json.loads(json.dumps(self.cfg)),
                             entries=lambda: [], running=lambda: self.running,
                             ask=Ask(self.PORTRAIT), save_config=save)
        panel.frame.pack()
        panel.set_text("哈哈哈哈\n我：周末吃火锅？\n对方：好呀 我超爱吃火锅")
        return panel

    def test_only_ticked_traits_are_saved(self):
        saved = []
        panel = self.portrait_panel(lambda cfg: saved.append(cfg))
        panel.start_portrait()
        self.pump(panel)
        self.assertEqual([v.get() for v, _ in panel.portrait_choices], [False, False])   # nothing pre-ticked
        panel.portrait_choices[1][0].set(True)
        save = [w for w in panel.portrait_window.winfo_children()[0].winfo_children()
                if isinstance(w, tk.Frame)][-1].winfo_children()[0]
        save.invoke()
        traits = saved[0]["profiles"][0]["traits"]
        self.assertEqual([(t["text"], t["evidence"], t["kind"]) for t in traits],
                         [("喜欢吃火锅", "我超爱吃火锅", "兴趣话题")])
        self.assertTrue(traits[0]["added"])

    def test_nothing_ticked_saves_nothing(self):
        saved = []
        panel = self.portrait_panel(lambda cfg: saved.append(cfg))
        panel.start_portrait()
        self.pump(panel)
        save = [w for w in panel.portrait_window.winfo_children()[0].winfo_children()
                if isinstance(w, tk.Frame)][-1].winfo_children()[0]
        save.invoke()
        self.assertEqual(saved, [])

    def test_not_saved_while_hosting_runs(self):
        saved = []
        panel = self.portrait_panel(lambda cfg: saved.append(cfg))
        panel.start_portrait()
        self.pump(panel)
        panel.portrait_choices[0][0].set(True)
        self.running = True
        save = [w for w in panel.portrait_window.winfo_children()[0].winfo_children()
                if isinstance(w, tk.Frame)][-1].winfo_children()[0]
        with patch("advisor_ui.messagebox.showinfo") as info:
            save.invoke()
        info.assert_called_once()
        self.assertEqual(saved, [])


class ContextMenuTests(PanelBase):
    """Right-click menus: Tk text has none of its own (user report 16:2x)."""

    @staticmethod
    def labels(menu):
        return [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)]

    def test_input_and_output_have_menus(self):
        panel = self.panel(Ask())
        self.assertEqual(self.labels(panel.input_menu), ["剪切", "复制", "粘贴", "全选"])
        self.assertEqual(self.labels(panel.output_menu), ["复制", "全选"])
        self.assertTrue(panel.input.bind("<Button-3>"))
        self.assertTrue(panel.output.bind("<Button-3>"))

    def test_copy_selection_from_input_and_cut(self):
        panel = self.panel(Ask())
        panel.set_text("对方：在吗")
        panel.input.tag_add("sel", "1.3", "1.5")
        panel.copy_input()
        self.assertEqual(self.root.clipboard_get(), "在吗")
        panel.copy_input(cut=True)
        self.assertEqual(panel.text(), "对方：")

    def test_copy_analysis_selection_or_everything(self):
        panel = self.panel(Ask(ANSWER))
        panel.set_text("我搞了")
        panel.start_analysis()
        self.pump(panel)
        panel.copy_output(all_if_empty=True)
        copied = self.root.clipboard_get()
        self.assertIn("可能想听你夸她", copied)
        # The cards are embedded widgets; "copy all" must still carry the replies.
        self.assertIn("1. 可以啊（为什么：接住；代价：平淡）", copied)
        self.assertIn("3. 给我看看", copied)
        start = panel.output.search("顺着接", "1.0")
        panel.output.tag_add("sel", start, f"{start}+3c")
        panel.copy_output()
        self.assertEqual(self.root.clipboard_get(), "顺着接")

    def test_each_card_copies_its_own_text_on_right_click(self):
        panel = self.panel(Ask(ANSWER))
        panel.set_text("我搞了")
        panel.start_analysis()
        self.pump(panel)
        card = panel.card_widgets[1]
        menu = next(w for w in card.winfo_children() if isinstance(w, tk.Menu))
        self.assertEqual(self.labels(menu), ["复制这条"])
        menu.invoke(0)
        self.assertEqual(self.root.clipboard_get(), "牛")

    def test_menu_copy_is_refused_while_hosting_runs(self):
        panel = self.panel(Ask())
        panel.set_text("对方：在吗")
        self.root.clipboard_clear()
        self.root.clipboard_append("原来的")
        self.running = True
        panel.input.tag_add("sel", "1.0", "end-1c")
        with patch("advisor_ui.messagebox.showinfo"):
            panel.copy_input(cut=True)
        self.assertEqual(self.root.clipboard_get(), "原来的")
        self.assertEqual(panel.text(), "对方：在吗")       # cut refused too: nothing deleted


class RhythmPanelTests(unittest.TestCase):
    def test_demo_draws_one_candle_per_day(self):
        root = tk.Tk()
        root.withdraw()
        try:
            panel = RhythmPanel(root, root)
            panel.frame.pack(fill="both", expand=True)
            root.geometry("800x500")
            root.update()
            panel.show_demo("双向升温")
            root.update()
            rectangles = [i for i in panel.canvas.find_all() if panel.canvas.type(i) == "rectangle"]
            self.assertEqual(len(rectangles), len(panel.series))
            self.assertIn("8 天", panel.info.cget("text"))
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
