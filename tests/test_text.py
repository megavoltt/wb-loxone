"""
Текстовые каналы: значения уходят в Loxone как есть, шаблон делает
по цифровому входу на каждое известное значение.

    python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import wbloxone as wl  # noqa: E402

wl.log = lambda *a: None  # журнал событий в тестах не нужен

DEV, CH = "wb-msw_1", "action"


class Rig:
    """Модель с одним текстовым каналом и записью пакетов, ушедших в Loxone."""

    def __init__(self, ch=CH, selected=True, meta=None):
        self.ch = ch
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = wl.Config(os.path.join(self.tmp.name, "cfg.json"))
        self.cfg.update({"throttle_ms": 0,
                         "selected": {("%s/%s" % (DEV, ch)): True} if selected else {}})
        self.model = wl.Model()
        self.bridge = wl.Bridge(self.cfg, self.model)
        self.sent = []
        self.bridge.send = lambda d, c, v: self.sent.append("%s/%s=%s" % (d, c, v))
        self.model.handle("/devices/%s/controls/%s/meta" % (DEV, ch),
                          meta or '{"type":"text","readonly":true}')

    def put(self, value):
        self.model.handle("/devices/%s/controls/%s" % (DEV, self.ch), value)


class TextValues(unittest.TestCase):
    def test_numeric_text_stays_number(self):
        self.assertEqual(wl.to_loxone("text", "42"), "42")
        self.assertEqual(wl.to_loxone("text", "on"), "1")

    def test_enum_text_becomes_string(self):
        self.assertEqual(wl.text_value("single"), "single")
        self.assertEqual(wl.send_value("text", "single"), "single")
        self.assertEqual(wl.text_value("Режим авто"), "Rezhim_avto")  # кириллица → транслит

    def test_spaces_become_underscores(self):
        # пробелы превращаются в "_": строка шлётся, шаблон матчит ту же строку
        self.assertEqual(wl.text_value("a b c"), "a_b_c")

    def test_garbage_rejected(self):
        for bad in (None, "", "x" * 40):
            self.assertIsNone(wl.text_value(bad), bad)
        self.assertIsNone(wl.send_value("value", "single"))  # не текстовый тип — только числа


class EventChannel(unittest.TestCase):
    def test_action_is_event(self):
        self.assertTrue(wl.text_is_event("action"))
        self.assertTrue(wl.text_is_event("Button Action"))

    def test_state_channels_are_not_events(self):
        for ch in ("mode", "preset", "state", "status", "current_temp"):
            self.assertFalse(wl.text_is_event(ch), ch)


class Learning(unittest.TestCase):
    def test_new_value_learned_and_sent(self):
        r = Rig()
        r.put("single")
        self.assertIn("wb-msw_1/action=single", [s.replace("/", "/", 1) for s in r.sent])
        self.assertEqual(r.cfg["textvals"]["%s/%s" % (DEV, CH)], ["single"])

    def test_known_value_not_duplicated(self):
        r = Rig()
        r.put("single")
        r.put("single")
        self.assertEqual(r.cfg["textvals"]["%s/%s" % (DEV, CH)], ["single"])

    def test_unselected_not_learned(self):
        r = Rig(selected=False)
        r.put("single")
        self.assertEqual(r.cfg["textvals"], {})

    def test_config_persists(self):
        r = Rig()
        r.put("double")
        cfg2 = wl.Config(os.path.join(r.tmp.name, "cfg.json"))
        self.assertEqual(cfg2["textvals"]["%s/%s" % (DEV, CH)], ["double"])

    def test_repeat_same_value_is_new_event(self):
        # два одинаковых нажатия подряд (action zigbee2mqtt не меняется) —
        # оба должны уйти в Loxone
        r = Rig()
        r.put("single")
        r.put("single")
        sent = [s for s in r.sent if s.endswith("=single")]
        self.assertEqual(len(sent), 2)

    def test_numeric_repeat_not_duplicated(self):
        # числовые каналы по-прежнему шлются только при изменении
        r = Rig(meta='{"type":"value","readonly":true}')
        r.put("84")
        r.put("84")
        self.assertEqual(len(r.sent), 1)

    def test_text_events_bypass_throttle(self):
        # быстрые события на одном канале не ждут окна троттлинга
        # и не затирают друг друга
        r = Rig()
        r.cfg.update({"throttle_ms": 500})
        r.put("single")
        r.put("double")
        r.put("triple")
        self.assertEqual(r.sent, ["%s/%s=%s" % (DEV, CH, v)
                                  for v in ("single", "double", "triple")])

    def test_seeds_by_channel_name(self):
        r = Rig(ch="mode")
        vals = wl.text_values_for(r.cfg, DEV, "mode")
        for v in ("off", "heat", "auto"):
            self.assertIn(v, vals)


class Template(unittest.TestCase):
    def test_digital_entry_per_value(self):
        r = Rig()
        r.put("hold")
        r.cfg["textvals"]["%s/%s" % (DEV, CH)] = ["single", "hold"]
        xml = wl.tpl_inputs(r.cfg, r.model, r.bridge)
        self.assertIn('Check="wb.wb-msw_1.action=single"', xml)
        self.assertIn('Check="wb.wb-msw_1.action=hold"', xml)
        cmds = [l for l in xml.splitlines() if "VirtualInUdpCmd" in l]
        self.assertEqual(len(cmds), 6)  # 6 типовых значений action
        self.assertTrue(all('Analog="false"' in l and "=\\v" not in l for l in cmds))

    def test_template_has_no_analog_for_text(self):
        r = Rig()
        r.put("single")
        xml = wl.tpl_inputs(r.cfg, r.model, r.bridge)
        cmds = [l for l in xml.splitlines() if "VirtualInUdpCmd" in l]
        self.assertTrue(cmds)
        self.assertTrue(all('Analog="false"' in l for l in cmds))

    def test_numeric_text_keeps_analog(self):
        r = Rig()
        r.put("84")
        xml = wl.tpl_inputs(r.cfg, r.model, r.bridge)
        self.assertIn('Check="wb.wb-msw_1.action=\\v"', xml)
        self.assertIn('Analog="true"', xml)

    def test_input_ok_counts(self):
        r = Rig()
        r.put("single")
        n_in, _ = wl.tpl_counts(r.cfg, r.model, DEV)
        self.assertEqual(n_in, 1)  # канал теперь попадает во вход
        r2 = Rig(selected=False)
        r2.model.handle("/devices/%s/controls/%s" % (DEV, CH), None)
        n_in2, _ = wl.tpl_counts(r2.cfg, r2.model, DEV)
        self.assertEqual(n_in2, 0)


class Resync(unittest.TestCase):
    def test_event_text_not_resent_on_resync(self):
        # action — событие: повтор последнего значения при resync (раз в resync_s
        # и после сохранения настроек) выглядит в Loxone как новое нажатие
        r = Rig()
        r.put("single")
        r.sent.clear()
        n = r.bridge.resync()
        self.assertEqual(n, 0)
        self.assertEqual(r.sent, [])

    def test_state_text_resent_on_resync(self):
        # режим/статус — состояние: после перезагрузки Miniserver оно
        # должно восстановиться, поэтому повторяется
        r = Rig(ch="mode")
        r.put("heat")
        r.sent.clear()
        n = r.bridge.resync()
        self.assertEqual(n, 1)
        self.assertTrue(any("=heat" in s for s in r.sent))


if __name__ == "__main__":
    unittest.main()
