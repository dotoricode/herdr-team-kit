"""Read terminal mouse packets without depending on ncurses button-five support."""
from collections import deque
import curses
import re
import sys
import time


class MouseInput:
    def __init__(self,screen,prefix=None,stream=None):
        self.screen=screen;self.stream=sys.stdout if stream is None else stream
        if prefix is None:
            try:prefix=curses.tigetstr('kmous')
            except curses.error:pass
        self.prefix=prefix;self.pending=deque();self.enabled=False

    def __enter__(self):
        # Keep keypad's arrow decoding, but own the raw mouse payload. Old macOS
        # ncurses aliases wheel-down to a motion bit and has no BUTTON5 constant.
        try:curses.mousemask(0)
        except curses.error:pass
        if self.stream.isatty():
            self.enabled=True
            try:self.stream.write('\x1b[?1000h\x1b[?1006h');self.stream.flush()
            except BaseException:
                self.__exit__(None,None,None);raise
        return self

    def __exit__(self,*unused):
        if self.enabled:
            self.stream.write('\x1b[?1006l\x1b[?1000l');self.stream.flush();self.enabled=False

    def key(self):return self.pending.popleft() if self.pending else self.screen.getch()

    def read(self):
        key=self.key()
        if key<0:return None
        if key not in (27,curses.KEY_MOUSE):return ('key',key)
        deadline=time.monotonic()+.25
        consumed=[]
        def take():
            remaining=deadline-time.monotonic()
            if remaining<=0:return -1
            self.screen.timeout(max(1,min(35,int(remaining*1000))))
            value=self.key()
            if value>=0:consumed.append(value)
            return value
        def escape():
            self.pending.extendleft(reversed(consumed));return ('key',27)
        try:
            x10=key==curses.KEY_MOUSE and self.prefix==b'\x1b[M'
            sgr=key==curses.KEY_MOUSE and self.prefix==b'\x1b[<'
            if key==27:
                if take()!=91:return escape()
                code=take()
                arrows={65:curses.KEY_UP,66:curses.KEY_DOWN,67:curses.KEY_RIGHT,68:curses.KEY_LEFT,72:curses.KEY_HOME,70:curses.KEY_END}
                if code in arrows:return ('key',arrows[code])
                if code!=60:return escape()
                sgr=True
            if x10:
                values=[take(),take(),take()]
                if any(v<32 or v>255 for v in values):return None
                button,x,y=(v-32 for v in values);suffix='M'
            elif sgr:
                payload=[]
                for _ in range(64):
                    code=take()
                    if code in (77,109):break
                    if code not in range(48,58) and code!=59:return None
                    payload.append(chr(code))
                else:return None
                raw=''.join(payload)
                if not re.fullmatch(r'\d{1,6};\d{1,6};\d{1,6}',raw):return None
                button,x,y=map(int,raw.split(';'));suffix=chr(code)
            else:return None
            if button>127 or x<1 or y<1 or suffix=='m':return None
            if button&192==64 and button&3 in (0,1) and not button&32:
                return ('scroll',-3 if button&3==0 else 3)
            if button&192==0 and not button&32 and button&3==0:
                return ('click',x-1,y-1)
            return None
        finally:self.screen.timeout(200)
