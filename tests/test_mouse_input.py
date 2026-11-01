from collections import deque
from io import StringIO
import curses
import unittest
from unittest.mock import patch


class Screen:
    def __init__(self, keys):self.keys=deque(keys);self.timeouts=[]
    def getch(self):return self.keys.popleft() if self.keys else -1
    def timeout(self,value):self.timeouts.append(value)


class MouseInputTests(unittest.TestCase):
    def reader(self, keys, prefix=b'\x1b[M'):
        from teamkit.mouse_input import MouseInput
        return MouseInput(Screen(keys),prefix=prefix,stream=StringIO())

    def test_sgr_and_x10_both_scroll_directions_without_button5_constant(self):
        for code,amount in ((64,-3),(65,3),(68,-3),(81,3)):
            with self.subTest(code=code):
                self.assertEqual(self.reader(map(ord,f'\x1b[<{code};10;5M')).read(),('scroll',amount))
                self.assertEqual(self.reader([curses.KEY_MOUSE,code+32,42,37]).read(),('scroll',amount))

    def test_click_release_motion_and_horizontal_wheel(self):
        r=self.reader(map(ord,'\x1b[<0;300;500M\x1b[<0;300;500m'))
        self.assertEqual(r.read(),('click',299,499));self.assertIsNone(r.read())
        for code in (3,32,66,67,128,256):
            self.assertIsNone(self.reader(map(ord,f'\x1b[<{code};10;5M')).read())
        self.assertEqual(self.reader([curses.KEY_MOUSE,32,42,37]).read(),('click',9,4))
        self.assertIsNone(self.reader([curses.KEY_MOUSE,35,42,37]).read())

    def test_sgr_terminfo_prefix_consumed_by_keypad(self):
        self.assertEqual(self.reader([curses.KEY_MOUSE,*map(ord,'65;10;5M')],b'\x1b[<').read(),('scroll',3))

    def test_escape_unknown_sequence_arrows_and_normal_keys_are_preserved(self):
        r=self.reader([27,ord('z'),curses.KEY_DOWN,ord('v')])
        self.assertEqual([r.read(),r.read(),r.read(),r.read()],[('key',27),('key',ord('z')),('key',curses.KEY_DOWN),('key',ord('v'))])
        self.assertEqual(self.reader([27]).read(),('key',27))
        self.assertEqual(self.reader(map(ord,'\x1b[B')).read(),('key',curses.KEY_DOWN))

    def test_broken_packets_and_bad_coordinates_are_ignored_and_timeout_restored(self):
        for keys in ([curses.KEY_MOUSE,96],list(map(ord,'\x1b[<65;0;1M')),list(map(ord,'\x1b[<65;-1;1M')),list(map(ord,'\x1b[<65;1;')),list(map(ord,'\x1b[<'+'9'*100+'M'))):
            r=self.reader(keys);self.assertIsNone(r.read());self.assertEqual(r.screen.timeouts[-1],200)

    def test_total_time_budget_is_bounded(self):
        r=self.reader(map(ord,'\x1b[<65;10;5M'))
        clock=iter([0,0,.3,.3,.3])
        with patch('teamkit.mouse_input.time.monotonic',side_effect=lambda:next(clock,.3)):
            self.assertEqual(r.read(),('key',27))
        self.assertEqual(r.screen.timeouts[-1],200)

    def test_terminal_modes_are_disabled_even_on_exception(self):
        class Terminal(StringIO):
            def isatty(self):return True
        out=Terminal();r=self.reader([]);r.stream=out
        with patch('teamkit.mouse_input.curses.mousemask'):
            with self.assertRaisesRegex(RuntimeError,'stop'):
                with r:raise RuntimeError('stop')
        self.assertIn('\x1b[?1000h\x1b[?1006h',out.getvalue())
        self.assertTrue(out.getvalue().endswith('\x1b[?1006l\x1b[?1000l'))
