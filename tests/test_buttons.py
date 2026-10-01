"""
Распознавание нажатий без контроллера: подставное время, вход и счётчик
подаются так, как их публикует wb-mqtt-serial.

    python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import wbloxone as wl  # noqa: E402

wl.log = lambda *a: None  # журнал событий в тестах не нужен

DEV, IN = "wb-mr6c_1", "Input 1"
EVENTS = ("click", "double", "triple", "long")


class Rig:
    """Модель с одним входом-кнопкой и записью событий, ушедших в Loxone."""

    def __init__(self, selected=EVENTS, click_ms=250, long_ms=600, start_down=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = wl.Config(os.path.join(self.tmp.name, "cfg.json"))
        self.cfg.update({"click_ms": click_ms, "long_ms": long_ms, "pulse_ms": 100,
                         "selected": {"%s/%s#%s" % (DEV, IN, e): True for e in selected}})
        self.model = wl.Model()
        self.bridge = wl.Bridge(self.cfg, self.model)
        self.sent = []
        self.bridge.send = lambda d, c, v: self.sent.append((round(self.t, 3), c.split("#")[-1], v))
        self.buttons = wl.Buttons(self.cfg, self.model, self.bridge)
        self.t = 0.0
        self.model.handle("/devices/%s/controls/%s/meta" % (DEV, IN), '{"type":"switch","readonly":true}')
        self.model.handle("/devices/%s/controls/%s" % (DEV, IN), "1" if start_down else "0")
        self.model.handle("/devices/%s/controls/%s counter" % (DEV, IN), "100")
        self.counter = 100

    def run(self, script, until):
        """script: [(время, "press"|"release"|("count", n))] — события шины по времени."""
        script = sorted(script, key=lambda e: e[0])
        while self.t <= until + 1e-9:
            while script and script[0][0] <= self.t + 1e-9:
                _, ev = script.pop(0)
                if ev == "press":
                    self.buttons.on_change(DEV, IN, "1", now=self.t)
                elif ev == "release":
                    self.buttons.on_change(DEV, IN, "0", now=self.t)
                else:
                    self.counter += ev[1]
                    self.buttons.on_change(DEV, IN + " counter", str(self.counter), now=self.t)
            self.buttons.step(self.t)
            self.t = round(self.t + 0.01, 3)
        return [(t, e) for t, e, v in self.sent if v == "1"]


def press(t, dur, counter_at=None):
    """Нажатие длительностью dur; счётчик по умолчанию приходит вместе с нажатием."""
    return [(t, "press"), (counter_at if counter_at is not None else t, ("count", 1)), (t + dur, "release")]


class Buttons(unittest.TestCase):
    def events(self, rig, script, until=3.0):
        return [e for _, e in rig.run(script, until)]

    def test_single_missed_by_bus(self):
        # шина не увидела импульс на входе — только прирост счётчика
        r = Rig()
        out = r.run([(0.0, ("count", 1))], 1.0)
        self.assertEqual([e for _, e in out], ["click"])
        self.assertAlmostEqual(out[0][0], 0.25, delta=0.02)

    def test_double_in_one_poll(self):
        self.assertEqual(self.events(Rig(), [(0.0, ("count", 2))]), ["double"])

    def test_double_across_polls(self):
        self.assertEqual(self.events(Rig(), press(0.0, 0.2) + press(0.35, 0.1)), ["double"])

    def test_triple(self):
        self.assertEqual(self.events(Rig(), press(0.0, 0.1) + press(0.2, 0.1) + press(0.4, 0.1)), ["triple"])

    def test_long(self):
        out = Rig().run(press(0.0, 1.2), 3.0)
        self.assertEqual([e for _, e in out], ["long"])
        self.assertAlmostEqual(out[0][0], 0.6, delta=0.02)  # пока кнопку держат, не после отпускания

    def test_long_counter_after_release(self):
        # счётчик опрошен уже после отпускания — это то же долгое нажатие, не одинарное
        self.assertEqual(self.events(Rig(), press(0.0, 1.0, counter_at=1.05)), ["long"])

    def test_short_hold_is_click(self):
        self.assertEqual(self.events(Rig(), press(0.0, 0.3)), ["click"])

    def test_closed_on_start_is_not_long(self):
        # вход замкнут на старте службы (выключатель, датчик) — это не удержание кнопки
        self.assertEqual(self.events(Rig(start_down=True), [], 2.0), [])

    def test_counter_reset_ignored(self):
        r = Rig()
        r.counter = 0  # модуль перезагрузился, счётчик начался заново
        self.assertEqual(self.events(r, [(0.0, ("count", 0))]), [])

    # «Не ждать без нужды»: пауза click_ms нужна, только если выбрана более длинная серия

    def test_double_immediate_without_triple(self):
        r = Rig(selected=("click", "double", "long"))
        out = r.run(press(0.0, 0.15) + press(0.3, 0.15), 2.0)
        self.assertEqual([e for _, e in out], ["double"])
        self.assertAlmostEqual(out[0][0], 0.3, delta=0.02)  # сразу на втором нажатии

    def test_double_waits_when_triple_selected(self):
        out = Rig().run(press(0.0, 0.15) + press(0.3, 0.15), 2.0)
        self.assertAlmostEqual(out[0][0], 0.45 + 0.25, delta=0.02)  # отпускание + click_ms

    def test_click_immediate_without_double(self):
        r = Rig(selected=("click", "long"))
        out = r.run(press(0.0, 0.2), 1.0)
        self.assertEqual([e for _, e in out], ["click"])
        self.assertAlmostEqual(out[0][0], 0.2, delta=0.02)  # при отпускании

    def test_click_waits_with_double(self):
        out = Rig(selected=("click", "double")).run(press(0.0, 0.2), 1.0)
        self.assertAlmostEqual(out[0][0], 0.2 + 0.25, delta=0.02)

    def test_pulse_returns_to_zero(self):
        r = Rig()
        r.run([(0.0, ("count", 1))], 1.0)
        self.assertEqual([(e, v) for _, e, v in r.sent], [("click", "1"), ("click", "0")])


class Values(unittest.TestCase):
    def test_to_loxone(self):
        self.assertEqual(wl.to_loxone("switch", "1"), "1")
        self.assertEqual(wl.to_loxone("text", "true"), "1")
        self.assertEqual(wl.to_loxone("text", "single"), None)
        self.assertEqual(wl.to_loxone("rgb", "255;128;0"), "50100")

    def test_from_loxone(self):
        self.assertEqual(wl.from_loxone({"type": "rgb"}, "0050100"), "255;128;0")
        self.assertEqual(wl.from_loxone({"type": "range", "max": 100}, "150.5"), "100")
        self.assertEqual(wl.from_loxone({"type": "switch"}, "0.0"), "0")

    def test_safe_id(self):
        self.assertEqual(wl.safe_id("Температура в помещении"), "Temperatura_v_pomeschenii")
        self.assertEqual(wl.safe_id("Input 1#double"), "Input_1_double")


if __name__ == "__main__":
    unittest.main()
