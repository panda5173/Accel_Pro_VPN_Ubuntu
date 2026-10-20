#!/usr/bin/env python3
"""AccelPro VPN Launcher v1.2

Desktop launcher and monitor for the AccelPro Linux client (Debian / Ubuntu LTS).
The password and TOTP are never handled or stored by this application.
"""
import os, re, sys, json, time, queue, shutil, socket, logging, subprocess, threading, tkinter as tk
from logging.handlers import RotatingFileHandler
from tkinter import messagebox, filedialog

VERSION = '1.2'
APP_NAME = 'AccelPro VPN'
APP = os.environ.get('ACCELPRO_VPN_HOME') or os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(APP, 'lib'))           # vendored pystray
CLIENT = os.path.join(APP, 'accelpro_client')
HELPER = os.path.join(APP, 'accelpro-helper')
ICON = os.path.join(APP, 'accelpro-vpn.png')
SYS_DEFAULTS = '/etc/accelpro-vpn/defaults.json'
XDG_CFG = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
XDG_STATE = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
CFG_DIR = os.path.join(XDG_CFG, 'accelpro-vpn')
CFG = os.path.join(CFG_DIR, 'settings.json')
LOG_DIR = os.path.join(XDG_STATE, 'accelpro-vpn')
AUTOSTART = os.path.join(XDG_CFG, 'autostart', 'accelpro-vpn.desktop')
RUNTIME = os.environ.get('XDG_RUNTIME_DIR') or f'/tmp/accelpro-vpn-{os.getuid()}'
SOCK = os.path.join(RUNTIME, 'accelpro-vpn.sock')
STATS_FILE = '/tmp/openvpn_status.log'
PID_FILE = '/tmp/openvpn.pid'
STALL_SECS = 25            # no data received from the VPN server for this long = link down
MAX_SERVICES = 4
URL_RE = re.compile(r'^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?$')
USER_RE = re.compile(r'^[A-Za-z0-9._@-]{1,64}$')
HOST_RE = re.compile(r'^[A-Za-z0-9.:-]{1,253}$')
F = 'Ubuntu'

THEMES = {
    'Dark': dict(bg='#0b1220', side='#0f172a', card='#121c30', card2='#0d1628', border='#22304a',
                 text='#e5edf8', muted='#8a9bb8', navsel='#2563eb', tile='#0f1a2e'),
    'Light': dict(bg='#eef2f7', side='#ffffff', card='#ffffff', card2='#f6f8fb', border='#d9e1ec',
                  text='#0f172a', muted='#64748b', navsel='#2563eb', tile='#f8fafc'),
}
C = dict(green='#22c55e', blue='#3b82f6', purple='#a855f7', orange='#f97316',
         red='#ef4444', amber='#f59e0b', brand='#ff3b1f', gray='#64748b', btn='#334155', btnh='#475569')


# ---------------------------------------------------------------- utilities
def _read_json(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def clean_services(lst):
    out = []
    for x in lst if isinstance(lst, list) else []:
        try:
            name, host, port = str(x['name']).strip()[:40], str(x['host']).strip(), int(x['port'])
            if name and HOST_RE.match(host) and 0 < port < 65536:
                out.append({'name': name, 'host': host, 'port': port})
        except Exception:
            continue
    return out[:MAX_SERVICES]


def load_cfg():
    """Built-in defaults <- /etc/accelpro-vpn/defaults.json (administrator) <- per-user settings."""
    d = {'theme': 'Dark', 'interval': 2, 'disconnect_on_quit': True, 'server': '', 'username': '',
         'notify': True, 'drop_alert': True, 'autostart': False, 'services': []}
    for src in (_read_json(SYS_DEFAULTS), _read_json(CFG)):
        d.update({k: v for k, v in src.items() if k in d})
    d['services'] = clean_services(d['services'])
    if d['interval'] not in (1, 2, 5):
        d['interval'] = 2
    if d['theme'] not in THEMES:
        d['theme'] = 'Dark'
    return d


def save_cfg(c):
    os.makedirs(CFG_DIR, exist_ok=True)
    tmp = CFG + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(c, f, indent=2)
    os.replace(tmp, CFG)


def setup_logger():
    lg = logging.getLogger('accelpro-vpn')
    lg.setLevel(logging.INFO)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        h = RotatingFileHandler(os.path.join(LOG_DIR, 'accelpro-vpn.log'), maxBytes=1_000_000, backupCount=3)
        h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        lg.addHandler(h)
    except Exception:
        lg.addHandler(logging.NullHandler())
    return lg


LOG = setup_logger()


def run(args, timeout=8):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)


def human(n):
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.0f} {u}' if u == 'B' else f'{n:.1f} {u}'
        n /= 1024
    return f'{n:.2f} TB'


def single_instance():
    """Return a listening socket for the first instance; otherwise ask the running
    instance to show its window and return None."""
    os.makedirs(RUNTIME, mode=0o700, exist_ok=True)
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.connect(SOCK)
        c.sendall(b'show')
        c.close()
        return None
    except OSError:
        c.close()
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(2)
    return srv


def physical_link_up():
    """True if any real network interface is up (airplane mode -> False)."""
    try:
        for n in os.listdir('/sys/class/net'):
            if n == 'lo' or n.startswith(('tun', 'tap', 'docker', 'veth', 'br-', 'virbr')):
                continue
            with open(f'/sys/class/net/{n}/operstate') as f:
                if f.read().strip() == 'up':
                    return True
    except Exception:
        return True
    return False


# ---------------------------------------------------------------- application
class App:
    def __init__(self, server_sock=None, minimized=False):
        self.cfg = load_cfg()
        self.server_sock = server_sock
        self.q = queue.Queue()
        self.state = 'unknown'   # connected/disconnected/connecting/disconnecting/reconnecting/unknown
        self.busy = self.quitting = self.refreshing = self.svc_busy = False
        self.counters, self.prev, self.dl, self.ul, self.hist = {}, None, 0.0, 0.0, []
        self.logs, self.since, self.updated = [], None, '—'
        self.page, self.w, self.tray, self._tray_state = 'Home', {}, None, None
        self.svc, self.drop_hits, self.dropped, self.drop_msg = {}, 0, False, ''
        self.last_rx, self.health = (0, 0.0), ''

        self.root = tk.Tk(className='accelpro-vpn')
        try:
            self._icon = tk.PhotoImage(file=ICON)
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass
        self.root.title(APP_NAME)
        self.root.geometry('1000x720')
        self.root.minsize(920, 660)
        self.root.protocol('WM_DELETE_WINDOW', self.hide)
        self.build()
        self.start_tray()
        self.log(f'{APP_NAME} {VERSION} started.')
        if self.server_sock:
            threading.Thread(target=self._ipc_loop, daemon=True).start()
        if not self.configured:
            self.root.after(400, lambda: self.go('Settings'))
        elif minimized and self.tray:
            self.root.withdraw()
        self.root.after(100, self.poll)
        self.root.after(300, self.tick)

    @property
    def configured(self):
        return bool(URL_RE.match(self.cfg['server'] or '') and USER_RE.match(self.cfg['username'] or ''))

    @property
    def T(self):
        return THEMES[self.cfg['theme']]

    def _ipc_loop(self):
        while True:
            try:
                conn, _ = self.server_sock.accept()
                if conn.recv(16) == b'show':
                    self.q.put(('call', self.show))
                conn.close()
            except Exception:
                return

    # ---------- widget helpers ----------
    def btn(self, parent, text, cmd, bg, fg='white', hover=None, padx=18, pady=8, size=10):
        b = tk.Label(parent, text=text, bg=bg, fg=fg, padx=padx, pady=pady, font=(F, size, 'bold'), cursor='hand2')
        b._base = bg
        b.bind('<Button-1>', lambda e: cmd())
        b.bind('<Enter>', lambda e: b.config(bg=hover or b._base))
        b.bind('<Leave>', lambda e: b.config(bg=b._base))
        return b

    def card(self, parent, **pack):
        T = self.T
        f = tk.Frame(parent, bg=T['card'], highlightbackground=T['border'], highlightthickness=1)
        f.pack(**pack)
        return f

    def banner(self, text, color, buttons):
        bn = tk.Frame(self.body, bg=color)
        bn.pack(fill='x', pady=(0, 10))
        tk.Label(bn, text=text, bg=color, fg='white', font=(F, 10, 'bold')).pack(side='left', padx=14, pady=8)
        for label, cmd, bg in reversed(buttons):
            self.btn(bn, label, cmd, bg, pady=5, size=9).pack(side='right', padx=6)

    def draw_logo(self, cv, x, y):
        T = self.T
        cv.create_polygon(x, y, x + 30, y + 18, x, y + 36, x + 9, y + 18, fill=C['brand'], outline='')
        cv.create_polygon(x + 6, y + 10, x + 20, y + 18, x + 6, y + 26, x + 10, y + 18, fill=cv['bg'], outline='')
        cv.create_text(x + 38, y + 17, text='accelpro', anchor='w', fill=T['text'], font=(F, 20, 'bold'))

    # ---------- layout ----------
    def build(self):
        T = self.T
        for c in self.root.winfo_children():
            c.destroy()
        self.root.configure(bg=T['bg'])
        self.w = {}

        side = tk.Frame(self.root, bg=T['side'], width=215, highlightbackground=T['border'], highlightthickness=1)
        side.pack(side='left', fill='y')
        side.pack_propagate(False)
        cv = tk.Canvas(side, width=200, height=48, bg=T['side'], highlightthickness=0)
        cv.pack(pady=(22, 0), padx=14, anchor='w')
        self.draw_logo(cv, 4, 6)
        tk.Label(side, text='Secure. Fast. Everywhere.', bg=T['side'], fg=T['muted'],
                 font=(F, 9)).pack(anchor='w', padx=22, pady=(0, 22))
        self.nav = {}
        for name, icon in (('Home', '⌂'), ('Statistics', '📈'), ('Connection Log', '🗎'),
                           ('Settings', '⚙'), ('About', 'ⓘ')):
            b = tk.Label(side, text=f'  {icon}   {name}', anchor='w', padx=12, pady=10, cursor='hand2')
            b.pack(fill='x', padx=12, pady=2)
            b.bind('<Button-1>', lambda e, n=name: self.go(n))
            self.nav[name] = b
        self.paint_nav()

        bottom = tk.Frame(side, bg=T['side'])
        bottom.pack(side='bottom', fill='x', padx=18, pady=16)
        z = tk.Frame(bottom, bg=T['side'])
        z.pack(fill='x', pady=(0, 14))
        tk.Label(z, text='🛡', bg=T['side'], fg=C['green'], font=(F, 20)).pack(side='left')
        tk.Label(z, text='Zero Trust\nSecure Access', justify='left', bg=T['side'], fg=T['text'],
                 font=(F, 9)).pack(side='left', padx=8)
        v = tk.Frame(bottom, bg=T['side'])
        v.pack(fill='x')
        tk.Label(v, text=f'Version {VERSION}', bg=T['side'], fg=T['muted'], font=(F, 8)).pack(side='left')
        self.w['side_dot'] = tk.Label(v, text='●', bg=T['side'], fg=C['gray'], font=(F, 10))
        self.w['side_dot'].pack(side='right')

        main = tk.Frame(self.root, bg=T['bg'])
        main.pack(side='left', fill='both', expand=True)
        head = tk.Frame(main, bg=T['bg'])
        head.pack(fill='x', padx=24, pady=(20, 10))
        ht = tk.Frame(head, bg=T['bg'])
        ht.pack(side='left')
        tk.Label(ht, text=APP_NAME, bg=T['bg'], fg=T['text'], font=(F, 20, 'bold')).pack(anchor='w')
        tk.Label(ht, text='Secure remote access', bg=T['bg'], fg=T['muted'], font=(F, 10)).pack(anchor='w')
        pill = tk.Frame(head, bg=T['card'], highlightbackground=T['border'], highlightthickness=1)
        pill.pack(side='right')
        pr = tk.Frame(pill, bg=T['card'])
        pr.pack(padx=14, pady=(8, 0))
        self.w['pill_dot'] = tk.Label(pr, text='●', bg=T['card'], fg=C['gray'], font=(F, 14))
        self.w['pill_dot'].pack(side='left')
        self.w['pill_txt'] = tk.Label(pr, text='Checking', bg=C['gray'], fg='white', font=(F, 9, 'bold'), padx=10, pady=2)
        self.w['pill_txt'].pack(side='left', padx=6)
        self.w['pill_sub'] = tk.Label(pill, text='', bg=T['card'], fg=T['muted'], font=(F, 8))
        self.w['pill_sub'].pack(padx=14, pady=(2, 8))

        self.body = tk.Frame(main, bg=T['bg'])
        self.body.pack(fill='both', expand=True, padx=24, pady=(0, 18))
        self.render()

    def paint_nav(self):
        T = self.T
        for n, b in self.nav.items():
            sel = n == self.page
            b.config(bg=T['navsel'] if sel else T['side'], fg='white' if sel else T['text'],
                     font=(F, 10, 'bold' if sel else 'normal'))

    def go(self, page):
        self.page = page
        self.paint_nav()
        self.render()

    def render(self):
        try:
            self.root.unbind_all('<Button-4>')
            self.root.unbind_all('<Button-5>')
        except Exception:
            pass
        for c in self.body.winfo_children():
            c.destroy()
        for k in [k for k in self.w if not k.startswith(('pill', 'side'))]:
            del self.w[k]
        getattr(self, 'page_' + self.page.replace(' ', '_').lower())()
        self.update_view()

    # ---------- shared page parts ----------
    def section_head(self, parent, title, live=True):
        T = self.T
        h = tk.Frame(parent, bg=T['card'])
        h.pack(fill='x', padx=16, pady=(12, 6))
        tk.Label(h, text='🗎  ' + title, bg=T['card'], fg=T['text'], font=(F, 12, 'bold')).pack(side='left')
        if live:
            tk.Label(h, text=' (auto-refresh)', bg=T['card'], fg=T['muted'], font=(F, 8)).pack(side='left')
            self.w['live_dot'] = tk.Label(h, text='●', bg=T['card'], fg=C['gray'], font=(F, 10))
            self.w['live_dot'].pack(side='left', padx=4)
            self.w['updated'] = tk.Label(h, text='', bg=T['card'], fg=T['muted'], font=(F, 8))
            self.w['updated'].pack(side='right')

    def stat_tiles(self, parent):
        T = self.T
        row = tk.Frame(parent, bg=T['card'])
        row.pack(fill='x', padx=14, pady=(4, 14))
        spec = (('TUN/TAP read bytes', 'TUN/TAP', 'Read', C['green'], '↑'),
                ('TUN/TAP write bytes', 'TUN/TAP', 'Write', C['blue'], '↓'),
                ('TCP/UDP read bytes', 'TCP/UDP', 'Read', C['purple'], '↑'),
                ('TCP/UDP write bytes', 'TCP/UDP', 'Write', C['orange'], '↓'))
        for i, (key, a, b, col, arr) in enumerate(spec):
            t = tk.Frame(row, bg=T['tile'], highlightbackground=col, highlightthickness=1)
            t.grid(row=0, column=i, sticky='nsew', padx=5)
            row.columnconfigure(i, weight=1)
            tk.Label(t, text=arr, bg=col, fg='white', font=(F, 14, 'bold'), width=2).pack(side='left', padx=(12, 8), pady=14)
            inner = tk.Frame(t, bg=T['tile'])
            inner.pack(side='left', pady=10)
            self.w['tile_' + key] = tk.Label(inner, text='—', bg=T['tile'], fg=col, font=(F, 14, 'bold'))
            self.w['tile_' + key].pack(anchor='w')
            tk.Label(inner, text=f'{a}\n{b}', justify='left', bg=T['tile'], fg=T['muted'], font=(F, 8)).pack(anchor='w')

    def service_tiles(self, parent):
        T = self.T
        row = tk.Frame(parent, bg=T['card'])
        row.pack(fill='x', padx=14, pady=(2, 14))
        if not self.cfg['services']:
            tk.Label(row, text='No services configured. Add internal servers in Settings → Connection Profile.',
                     bg=T['card'], fg=T['muted'], font=(F, 9)).pack(anchor='w', padx=6)
        for i, s in enumerate(self.cfg['services']):
            key = f"{s['host']}:{s['port']}"
            t = tk.Frame(row, bg=T['tile'], highlightbackground=T['border'], highlightthickness=1)
            t.grid(row=0, column=i, sticky='nsew', padx=5)
            row.columnconfigure(i, weight=1)
            d = tk.Label(t, text='●', bg=T['tile'], fg=C['gray'], font=(F, 16))
            d.pack(side='left', padx=(12, 8), pady=10)
            inner = tk.Frame(t, bg=T['tile'])
            inner.pack(side='left', pady=8)
            tk.Label(inner, text=s['name'], bg=T['tile'], fg=T['text'], font=(F, 10, 'bold')).pack(anchor='w')
            tk.Label(inner, text=key, bg=T['tile'], fg=T['muted'], font=(F, 8)).pack(anchor='w')
            st = tk.Label(inner, text='Checking…', bg=T['tile'], fg=T['muted'], font=(F, 9, 'bold'))
            st.pack(anchor='w')
            self.w['svc_' + key] = (t, d, st)

    def log_text(self, parent, key, **kw):
        T = self.T
        t = tk.Text(parent, relief='flat', font=('Monospace', 9), highlightthickness=0, fg=T['text'], **kw)
        t.tag_config('ok', foreground=C['green'])
        t.tag_config('err', foreground=C['red'])
        t.tag_config('ts', foreground=T['muted'])
        self.w[key] = t
        return t

    # ---------- pages ----------
    def page_home(self):
        T = self.T
        if not self.configured:
            self.banner('ℹ  Setup required – enter your AccelPro server URL and username.', '#1e3a8a',
                        [('Open Settings', lambda: self.go('Settings'), C['blue'])])
        if self.dropped:
            self.banner('⚠  ' + (self.drop_msg or 'VPN connection dropped unexpectedly'), '#7f1d1d',
                        [('⟳  Reconnect', self.connect, C['blue']), ('Dismiss', self.dismiss_drop, '#991b1b')])
        c = self.card(self.body, fill='x')
        top = tk.Frame(c, bg=T['card'])
        top.pack(fill='x', padx=18, pady=14)
        left = tk.Frame(top, bg=T['card'])
        left.pack(side='left', fill='x', expand=True)
        for icon, lab, val in (('🌐', 'Server', self.cfg['server'].replace('https://', '') or 'Not configured'),
                               ('👤', 'User', self.cfg['username'] or 'Not configured')):
            r = tk.Frame(left, bg=T['card'])
            r.pack(anchor='w', pady=3)
            tk.Label(r, text=icon, bg=T['card'], fg=C['blue'], font=(F, 15)).pack(side='left', padx=(0, 12))
            col = tk.Frame(r, bg=T['card'])
            col.pack(side='left')
            tk.Label(col, text=lab, bg=T['card'], fg=T['muted'], font=(F, 8)).pack(anchor='w')
            tk.Label(col, text=val, bg=T['card'], fg=T['text'], font=(F, 10)).pack(anchor='w')
        br = tk.Frame(left, bg=T['card'])
        br.pack(anchor='w', pady=(12, 0))
        self.w['main_btn'] = self.btn(br, 'Connect VPN', self.toggle, C['blue'], padx=26, pady=9)
        self.w['main_btn'].pack(side='left')
        self.btn(br, '⟳  Refresh', self.refresh_now, C['btn'], hover=C['btnh'], pady=9).pack(side='left', padx=10)
        right = tk.Frame(top, bg=T['card'])
        right.pack(side='right', padx=10)
        tk.Label(right, text='🕒  Connection Uptime', bg=T['card'], fg=T['text'], font=(F, 9)).pack(anchor='w')
        self.w['uptime'] = tk.Label(right, text='--:--:--', bg=T['card'], fg=C['green'], font=(F, 22, 'bold'))
        self.w['uptime'].pack(anchor='w')
        self.w['since'] = tk.Label(right, text='', bg=T['card'], fg=T['muted'], font=(F, 8))
        self.w['since'].pack(anchor='w')

        s = self.card(self.body, fill='x', pady=(12, 0))
        self.section_head(s, 'Live Statistics')
        self.stat_tiles(s)
        sp = self.card(self.body, fill='x', pady=12)
        self.section_head(sp, 'Service Reachability', live=False)
        self.service_tiles(sp)
        l = self.card(self.body, fill='both', expand=True)
        self.section_head(l, 'Recent Log', live=False)
        self.log_text(l, 'recent', height=5, bg=T['card'], padx=16).pack(fill='both', expand=True, pady=(0, 10))

    def page_statistics(self):
        T = self.T
        s = self.card(self.body, fill='x')
        self.section_head(s, 'Live Statistics')
        self.stat_tiles(s)
        g = self.card(self.body, fill='both', expand=True, pady=12)
        h = tk.Frame(g, bg=T['card'])
        h.pack(fill='x', padx=16, pady=(12, 4))
        tk.Label(h, text='Throughput', bg=T['card'], fg=T['text'], font=(F, 12, 'bold')).pack(side='left')
        self.w['rates'] = tk.Label(h, text='', bg=T['card'], fg=T['muted'], font=(F, 9))
        self.w['rates'].pack(side='right')
        self.w['graph'] = tk.Canvas(g, bg=T['card2'], height=220, highlightthickness=0)
        self.w['graph'].pack(fill='both', expand=True, padx=16, pady=(4, 14))
        self.w['graph'].bind('<Configure>', lambda e: self.draw_graph())

    def page_connection_log(self):
        T = self.T
        c = self.card(self.body, fill='both', expand=True)
        h = tk.Frame(c, bg=T['card'])
        h.pack(fill='x', padx=16, pady=12)
        tk.Label(h, text='Connection Log', bg=T['card'], fg=T['text'], font=(F, 12, 'bold')).pack(side='left')
        self.btn(h, 'Clear', self.clear_log, C['btn'], hover=C['btnh'], pady=5, size=9).pack(side='right')
        self.btn(h, 'Copy', self.copy_log, C['btn'], hover=C['btnh'], pady=5, size=9).pack(side='right', padx=8)
        self.log_text(c, 'full', bg=T['card2'], padx=12, pady=8).pack(fill='both', expand=True, padx=16, pady=(0, 14))

    def page_settings(self):
        T = self.T
        outer = tk.Canvas(self.body, bg=T['bg'], highlightthickness=0)
        sb = tk.Scrollbar(self.body, orient='vertical', command=outer.yview)
        outer.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        outer.pack(side='left', fill='both', expand=True)
        holder = tk.Frame(outer, bg=T['bg'])
        win = outer.create_window((0, 0), window=holder, anchor='nw')
        holder.bind('<Configure>', lambda e: outer.configure(scrollregion=outer.bbox('all')))
        outer.bind('<Configure>', lambda e: outer.itemconfig(win, width=e.width))
        self.root.bind_all('<Button-4>', lambda e: outer.yview_scroll(-2, 'units'))
        self.root.bind_all('<Button-5>', lambda e: outer.yview_scroll(2, 'units'))

        def section(t):
            f = tk.Frame(holder, bg=T['card'], highlightbackground=T['border'], highlightthickness=1)
            f.pack(fill='x', pady=(0, 12))
            tk.Label(f, text=t, bg=T['card'], fg=T['text'], font=(F, 12, 'bold')).pack(anchor='w', padx=20, pady=(14, 2))
            return f

        def row(parent, t, sub):
            r = tk.Frame(parent, bg=T['card'])
            r.pack(fill='x', padx=20, pady=7)
            x = tk.Frame(r, bg=T['card'])
            x.pack(side='left')
            tk.Label(x, text=t, bg=T['card'], fg=T['text'], font=(F, 10, 'bold')).pack(anchor='w')
            tk.Label(x, text=sub, bg=T['card'], fg=T['muted'], font=(F, 9)).pack(anchor='w')
            o = tk.Frame(r, bg=T['card'])
            o.pack(side='right')
            return o

        def choices(o, key, values, fmt=str, setter=None):
            for v in values:
                sel = self.cfg.get(key) == v
                self.btn(o, fmt(v), (lambda v=v: setter(v)) if setter else (lambda v=v: self.set_cfg(key, v)),
                         C['blue'] if sel else C['btn'], hover=C['btnh'], padx=14, pady=6, size=9).pack(side='left', padx=3)

        onoff = lambda v: 'On' if v else 'Off'
        g = section('General')
        choices(row(g, 'Theme', 'Application colour scheme'), 'theme', ('Dark', 'Light'))
        choices(row(g, 'Auto-refresh', 'Status, statistics and service check interval'), 'interval', (1, 2, 5),
                lambda v: f'{v} sec')
        choices(row(g, 'Desktop notifications', 'Connected / disconnected / sign-in / failure pop-ups'),
                'notify', (True, False), onoff)
        choices(row(g, 'Connection drop alert', 'Alert with Reconnect button if the tunnel drops'),
                'drop_alert', (True, False), onoff)
        choices(row(g, 'Quit behaviour', 'Disconnect VPN automatically when quitting'),
                'disconnect_on_quit', (True, False), lambda v: 'Disconnect' if v else 'Keep VPN')
        choices(row(g, 'Start at login', 'Start minimised to the tray when you log in'),
                'autostart', (True, False), onoff, setter=self.set_autostart)
        tk.Frame(g, bg=T['card'], height=8).pack()

        p = section('Connection Profile')
        tk.Label(p, text='Enter the server URL and username provided by your administrator. The VPN certificate is '
                         'issued fresh by the server after every sign-in and is never stored here.',
                 bg=T['card'], fg=T['muted'], font=(F, 9), wraplength=640, justify='left').pack(anchor='w', padx=20)
        ents = {}
        for key, lab, hint in (('server', 'Server URL', 'https://vpn.example.com:9555'), ('username', 'Username', '')):
            r = tk.Frame(p, bg=T['card'])
            r.pack(fill='x', padx=20, pady=5)
            tk.Label(r, text=lab, width=14, anchor='w', bg=T['card'], fg=T['text'], font=(F, 10)).pack(side='left')
            e = tk.Entry(r, bg=T['card2'], fg=T['text'], insertbackground=T['text'], relief='flat',
                         highlightbackground=T['border'], highlightthickness=1, font=(F, 10))
            e.insert(0, self.cfg[key])
            e.pack(side='left', fill='x', expand=True, ipady=5)
            if hint:
                tk.Label(r, text=f'e.g. {hint}', bg=T['card'], fg=T['muted'], font=(F, 8)).pack(side='left', padx=8)
            ents[key] = e
        r = tk.Frame(p, bg=T['card'])
        r.pack(fill='x', padx=20, pady=5)
        tk.Label(r, text='Services', width=14, anchor='nw', bg=T['card'], fg=T['text'], font=(F, 10)).pack(side='left', anchor='n')
        svc = tk.Text(r, height=4, bg=T['card2'], fg=T['text'], insertbackground=T['text'], relief='flat',
                      highlightbackground=T['border'], highlightthickness=1, font=('Monospace', 9))
        svc.insert('1.0', '\n'.join(f"{x['name']}, {x['host']}, {x['port']}" for x in self.cfg['services']))
        svc.pack(side='left', fill='x', expand=True)
        tk.Label(p, text=f'Optional, up to {MAX_SERVICES}, one per line:  Name, Host/IP, TCP port   '
                         '(e.g.  File Server, 10.0.0.10, 445)',
                 bg=T['card'], fg=T['muted'], font=(F, 8)).pack(anchor='w', padx=150)

        def save():
            srv = ents['server'].get().strip().rstrip('/')
            usr = ents['username'].get().strip()
            if not URL_RE.match(srv):
                messagebox.showerror(APP_NAME, 'Server URL must look like:\nhttps://vpn.example.com:9555')
                return
            if not USER_RE.match(usr):
                messagebox.showerror(APP_NAME, 'Username may contain only letters, digits and . _ @ -')
                return
            lst = []
            for ln in svc.get('1.0', 'end').splitlines():
                parts = [x.strip() for x in ln.split(',')]
                if len(parts) == 3 and parts[2].isdigit() and HOST_RE.match(parts[1]) and parts[0]:
                    lst.append({'name': parts[0], 'host': parts[1], 'port': int(parts[2])})
                elif ln.strip():
                    messagebox.showerror(APP_NAME, f'Invalid service line:\n{ln}')
                    return
            if self.state in ('connected', 'reconnecting') and (srv != self.cfg['server'] or usr != self.cfg['username']):
                messagebox.showinfo(APP_NAME, 'The new server/username will be used on the next Connect.')
            self.cfg.update(server=srv, username=usr, services=clean_services(lst))
            self.svc = {}
            save_cfg(self.cfg)
            self.log('Connection profile saved.', 'ok')
            self.build()

        br = tk.Frame(p, bg=T['card'])
        br.pack(fill='x', padx=20, pady=(10, 16))
        self.btn(br, 'Save Profile', save, C['blue'], pady=6, size=9).pack(side='left')
        self.btn(br, 'Export…', self.export_profile, C['btn'], hover=C['btnh'], pady=6, size=9).pack(side='left', padx=8)
        self.btn(br, 'Import…', self.import_profile, C['btn'], hover=C['btnh'], pady=6, size=9).pack(side='left')

        c = section('AccelPro Client')
        tk.Label(c, text=self.client_info(), bg=T['card'], fg=T['muted'], font=('Monospace', 9),
                 justify='left').pack(anchor='w', padx=20)
        br = tk.Frame(c, bg=T['card'])
        br.pack(fill='x', padx=20, pady=(10, 16))
        self.btn(br, 'Replace Client…', self.replace_client, C['btn'], hover=C['btnh'], pady=6, size=9).pack(side='left')
        self.btn(br, 'Quit Application', self.quit, C['red'], hover='#dc2626', pady=6, size=9).pack(side='right')

    def page_about(self):
        T = self.T
        c = self.card(self.body, fill='both', expand=True)
        cv = tk.Canvas(c, width=260, height=54, bg=T['card'], highlightthickness=0)
        cv.pack(anchor='w', padx=20, pady=(20, 6))
        self.draw_logo(cv, 4, 8)
        text = (f'{APP_NAME} Launcher  •  Version {VERSION}\n'
                'Unofficial desktop launcher for the AccelPro Linux client.\n\n'
                f"Server:  {self.cfg['server'] or 'Not configured'}\nUser:    {self.cfg['username'] or 'Not configured'}\n\n"
                '• Authentication uses the server\'s own WebAuth page.\n'
                '• The password can be saved by the browser\'s password manager; TOTP is entered at each sign-in.\n'
                '• This launcher never stores the password or TOTP.\n'
                '• Administrator access is requested only to connect, disconnect or replace the client.\n\n'
                f'Settings:  {CFG}\nLog file:  {os.path.join(LOG_DIR, "accelpro-vpn.log")}')
        tk.Label(c, text=text, justify='left', bg=T['card'], fg=T['text'], font=(F, 10)).pack(anchor='w', padx=22)

    # ---------- settings actions ----------
    def set_cfg(self, key, val):
        self.cfg[key] = val
        save_cfg(self.cfg)
        self.build()

    def set_autostart(self, on):
        try:
            if on:
                os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
                with open(AUTOSTART, 'w') as f:
                    f.write('[Desktop Entry]\nType=Application\nName=AccelPro VPN\nExec=accelpro-vpn --minimized\n'
                            'Icon=accelpro-vpn\nTerminal=false\nX-GNOME-Autostart-enabled=true\n')
            elif os.path.exists(AUTOSTART):
                os.remove(AUTOSTART)
        except Exception as e:
            messagebox.showerror(APP_NAME, f'Could not update autostart:\n{e}')
            return
        self.set_cfg('autostart', on)

    def client_info(self):
        try:
            st = os.stat(CLIENT)
            return (f'Installed: {CLIENT}\nSize: {human(st.st_size)}    Modified: '
                    f"{time.strftime('%d %b %Y %H:%M', time.localtime(st.st_mtime))}")
        except Exception:
            return 'AccelPro client not installed.'

    def replace_client(self):
        if self.state in ('connected', 'reconnecting'):
            messagebox.showwarning(APP_NAME, 'Disconnect the VPN before replacing the client.')
            return
        f = filedialog.askopenfilename(title='Select AccelPro Linux client', initialdir=os.path.expanduser('~/Downloads'))
        if not f:
            return
        rc, out = run(['pkexec', HELPER, 'install-client', os.path.realpath(f)], timeout=120)
        self.log(f'AccelPro client replaced from {os.path.basename(f)}.' if rc == 0
                 else f'Client replacement failed: {out or rc}', 'ok' if rc == 0 else 'err')
        self.build()

    def export_profile(self):
        f = filedialog.asksaveasfilename(title='Export profile', defaultextension='.json',
                                         initialfile='accelpro-profile.json')
        if f:
            with open(f, 'w') as fh:
                json.dump({k: self.cfg[k] for k in ('server', 'username', 'services')}, fh, indent=2)
            self.log('Profile exported (contains no password).', 'ok')

    def import_profile(self):
        f = filedialog.askopenfilename(title='Import profile', filetypes=[('JSON', '*.json')])
        if not f:
            return
        try:
            d = _read_json(f)
            srv, usr = str(d.get('server', '')).rstrip('/'), str(d.get('username', ''))
            assert URL_RE.match(srv) and USER_RE.match(usr), 'invalid server URL or username'
            self.cfg.update(server=srv, username=usr,
                            services=clean_services(d.get('services', self.cfg['services'])))
            save_cfg(self.cfg)
            self.svc = {}
            self.log('Profile imported.', 'ok')
            self.build()
        except Exception as e:
            messagebox.showerror(APP_NAME, f'Invalid profile file:\n{e}')

    # ---------- view update ----------
    def update_view(self):
        T, w, st = self.T, self.w, self.state
        col, label, sub = {
            'connected': (C['green'], 'Connected', 'VPN Tunnel Active'),
            'disconnected': (C['red'], 'Disconnected', 'VPN Tunnel Inactive'),
            'connecting': (C['amber'], 'Connecting', 'Waiting for sign-in'),
            'reconnecting': (C['amber'], 'Link Lost', self.health or 'Waiting for network'),
            'disconnecting': (C['amber'], 'Disconnecting', 'Stopping tunnel'),
        }.get(st, (C['gray'], 'Checking', ''))
        w['pill_dot'].config(fg=col)
        w['pill_txt'].config(text=label, bg=col)
        w['pill_sub'].config(text=sub)
        w['side_dot'].config(fg=col)
        live, tunnel = st == 'connected', st in ('connected', 'reconnecting')
        if 'main_btn' in w:
            b = w['main_btn']
            if self.busy:
                b._base, txt = C['btnh'], 'Please wait…'
            elif not self.configured and not tunnel:
                b._base, txt = C['blue'], '⚙  Set up profile'
            elif tunnel:
                b._base, txt = '#7f1d1d', '⏻  Disconnect VPN'
            else:
                b._base, txt = C['blue'], '⏻  Connect VPN'
            b.config(text=txt, bg=b._base)
        if 'uptime' in w:
            if tunnel and self.since:
                s = int(time.time() - self.since)
                w['uptime'].config(text=f'{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}',
                                   fg=C['green'] if live else C['amber'])
                w['since'].config(text='Since ' + time.strftime('%d %b %Y, %H:%M', time.localtime(self.since)))
            else:
                w['uptime'].config(text='--:--:--', fg=T['muted'])
                w['since'].config(text='Not connected')
        if 'live_dot' in w:
            w['live_dot'].config(fg=C['green'] if live else C['amber'] if tunnel else C['gray'])
            w['updated'].config(text='Last updated: ' + self.updated)
        for k, v in self.counters.items():
            if 'tile_' + k in w:
                w['tile_' + k].config(text=human(v) if tunnel else '—')
        if 'rates' in w:
            w['rates'].config(text=f'↓ {human(self.dl)}/s    ↑ {human(self.ul)}/s' if live else 'Not connected')
            self.draw_graph()
        for s in self.cfg['services']:
            key = f"{s['host']}:{s['port']}"
            if 'svc_' + key not in w:
                continue
            t, d, lab = w['svc_' + key]
            ms = self.svc.get(key, 'na')
            if st == 'reconnecting':
                c2, txt = C['amber'], 'VPN link lost'
            elif not live:
                c2, txt = C['gray'], 'VPN not connected'
            elif ms == 'na':
                c2, txt = C['gray'], 'Checking…'
            elif ms is None:
                c2, txt = C['red'], 'Unreachable'
            else:
                c2, txt = (C['green'] if ms < 150 else C['amber']), f'Reachable • {ms:.0f} ms'
            d.config(fg=c2)
            lab.config(text=txt, fg=c2)
            t.config(highlightbackground=c2 if live else T['border'])
        if 'recent' in w:
            self.fill_log(w['recent'], self.logs[-9:])
        if 'full' in w:
            self.fill_log(w['full'], self.logs)
        self.update_tray()

    def fill_log(self, t, rows):
        t.config(state='normal')
        t.delete('1.0', 'end')
        for ts, msg, tag in rows:
            t.insert('end', f'{ts}   ', 'ts')
            t.insert('end', msg + '\n', tag)
        t.see('end')
        t.config(state='disabled')

    def draw_graph(self):
        g = self.w.get('graph')
        if not g or not g.winfo_exists():
            return
        T = self.T
        g.delete('all')
        W, H = g.winfo_width(), g.winfo_height()
        for i in range(1, 4):
            g.create_line(0, H * i / 4, W, H * i / 4, fill=T['border'])
        data = self.hist[-60:]
        if len(data) < 2:
            g.create_text(W / 2, H / 2, text='Waiting for data…', fill=T['muted'], font=(F, 10))
            return
        mx = max(max(d, u) for d, u in data) or 1
        for idx, col in ((0, C['blue']), (1, C['green'])):
            pts = []
            for i, v in enumerate(data):
                pts += [i * W / 59, H - 10 - (v[idx] / mx) * (H - 20)]
            g.create_line(*pts, fill=col, width=2, smooth=True)
        g.create_text(8, 8, anchor='nw', text=f'peak {human(mx)}/s', fill=T['muted'], font=(F, 8))
        g.create_text(W - 8, 8, anchor='ne', text='● Download   ● Upload', fill=T['muted'], font=(F, 8))

    # ---------- logging ----------
    def log(self, msg, tag=''):
        msg = re.sub(r'https://\S+/webauth/login\?\S+', '[sign-in link]', msg.strip())
        if not msg:
            return
        self.logs.append((time.strftime('%H:%M:%S'), msg, tag))
        self.logs = self.logs[-500:]
        (LOG.warning if tag == 'err' else LOG.info)(msg)
        for k, n in (('recent', 9), ('full', 500)):
            if k in self.w and self.w[k].winfo_exists():
                self.fill_log(self.w[k], self.logs[-n:])

    def clear_log(self):
        self.logs = []
        self.update_view()

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append('\n'.join(f'{a}  {b}' for a, b, _ in self.logs))

    def notify(self, title, body, urgent=False, reconnect=False):
        if not self.cfg.get('notify', True) or not shutil.which('notify-send'):
            return
        args = ['notify-send', '-a', APP_NAME, '-i', 'accelpro-vpn', '-u', 'critical' if urgent else 'normal', title, body]

        def work():
            if reconnect:
                try:
                    r = subprocess.run(args[:1] + ['-A', 'reconnect=Reconnect'] + args[1:],
                                       capture_output=True, text=True, timeout=600)
                    if r.stdout.strip() == 'reconnect':
                        self.q.put(('call', self.connect))
                    return
                except Exception:
                    pass
            subprocess.run(args, capture_output=True, timeout=10)
        threading.Thread(target=work, daemon=True).start()

    # ---------- VPN actions ----------
    def toggle(self):
        if not self.busy:
            self.disconnect() if self.state in ('connected', 'reconnecting') else self.connect()

    def connect(self):
        if self.busy:
            return
        if not self.configured:
            self.show()
            self.go('Settings')
            messagebox.showinfo(APP_NAME, 'Enter your AccelPro server URL and username, then click Save Profile.')
            return
        if not os.path.isfile(CLIENT) or not os.path.isfile(HELPER):
            messagebox.showerror(APP_NAME, 'AccelPro client is missing.\nReinstall the accelpro-vpn package.')
            return
        self.busy, self.dropped, self.state = True, False, 'connecting'
        self.log('Launching AccelPro client…')
        if self.page == 'Home':
            self.render()
        else:
            self.update_view()
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        try:
            p = subprocess.Popen(['pkexec', HELPER, 'start', '--url', self.cfg['server'], '--username', self.cfg['username']],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in p.stdout:
                m = re.search(r'https://\S+/webauth/login\?\S+', line)
                if m:
                    self.q.put(('url', m.group(0)))
                elif line.startswith('Executing OpenVPN'):
                    self.q.put(('log', 'OpenVPN started.'))
                elif line.strip() and not re.search(r'Please open the following URL|After logging in through the browser', line):
                    self.q.put(('log', line))
            self.q.put(('connected_done', p.wait()))
        except Exception as e:
            self.q.put(('connected_done', str(e)))

    def open_auth(self, url):
        self.log('Waiting for authentication (password + TOTP)…')
        self.notify('AccelPro sign-in', 'Complete sign-in in the AccelPro window.')
        for c in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'microsoft-edge'):
            b = shutil.which(c)
            if b:
                subprocess.Popen([b, f'--app={url}', '--window-size=470,640', '--no-first-run'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                return
        subprocess.Popen(['xdg-open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def disconnect(self, then_quit=False):
        if self.busy and not then_quit:
            return
        self.busy, self.state = True, 'disconnecting'
        self.log('Disconnecting VPN…')
        self.update_view()

        def work():
            rc, out = run(['pkexec', HELPER, 'stop'], timeout=60)
            self.q.put(('stopped', (rc, out, then_quit)))
        threading.Thread(target=work, daemon=True).start()

    def dismiss_drop(self):
        self.dropped = False
        self.render()

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        _, s = run([CLIENT, '--status'], timeout=5)
        low = s.lower()
        up = ('connected' in low and 'disconnected' not in low) or self.state in ('connected', 'connecting', 'reconnecting')
        if self.cfg['disconnect_on_quit'] and up:
            self.show()
            self.disconnect(then_quit=True)
        else:
            self.exit_now()

    def exit_now(self):
        try:
            if self.server_sock:
                self.server_sock.close()
                os.unlink(SOCK)
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self.root.after(50, self.root.destroy)

    def hide(self):
        self.root.withdraw() if self.tray else self.quit()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # ---------- refresh ----------
    def tick(self):
        if not self.quitting:
            if not self.refreshing:
                self.refreshing = True
                threading.Thread(target=self._refresh_worker, daemon=True).start()
            if not self.svc_busy and self.state == 'connected' and self.cfg['services']:
                self.svc_busy = True
                threading.Thread(target=self._service_worker, daemon=True).start()
        self.root.after(int(self.cfg['interval']) * 1000, self.tick)

    def refresh_now(self):
        if not self.refreshing:
            self.refreshing = True
            threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        _, status = run([CLIENT, '--status'], timeout=3)
        counters = {}
        try:
            with open(STATS_FILE) as f:
                for line in f:
                    if ',' in line:
                        k, v = line.split(',', 1)
                        if v.strip().isdigit():
                            counters[k.strip()] = int(v)
        except Exception:
            pass
        since = None
        try:
            since = os.path.getmtime(PID_FILE)
        except Exception:
            pass
        self.q.put(('refresh', (status, counters, since, physical_link_up())))

    def _service_worker(self):
        res = {}

        def probe(s):
            key = f"{s['host']}:{s['port']}"
            t0 = time.time()
            try:
                with socket.create_connection((s['host'], int(s['port'])), timeout=1.5):
                    res[key] = (time.time() - t0) * 1000
            except Exception:
                res[key] = None
        ts = [threading.Thread(target=probe, args=(s,), daemon=True) for s in self.cfg['services']]
        for t in ts:
            t.start()
        for t in ts:
            t.join(2)
        self.q.put(('svc', res))

    def apply_refresh(self, status, counters, since, link_up=True):
        self.refreshing = False
        low = status.lower()
        connected = 'connected' in low and 'disconnected' not in low
        now = time.time()

        # AccelPro keeps reporting "connected" while offline, so verify link health ourselves.
        health = ''
        if connected:
            rx = counters.get('TCP/UDP read bytes')
            if rx is not None and (rx != self.last_rx[0] or not self.last_rx[1]):
                self.last_rx = (rx, now)
            if not link_up:
                health = 'No network (Wi-Fi/LAN off)'
                self.last_rx = (self.last_rx[0], now)          # offline time is not server silence
            elif self.last_rx[1] and now - self.last_rx[1] > STALL_SECS:
                health = f'No response from server ({int(now - self.last_rx[1])}s)'
        else:
            self.last_rx = (0, 0.0)
        self.health = health

        if not self.busy:
            new = ('reconnecting' if health else 'connected') if connected else \
                  ('disconnected' if 'disconnected' in low else 'unknown')
            if new == 'disconnected' and self.state in ('connected', 'reconnecting'):
                self.drop_hits += 1
                if self.drop_hits < 2:                         # confirm on next poll
                    new = self.state
                else:
                    self.log('VPN connection dropped unexpectedly.', 'err')
                    self._drop_alert('The VPN tunnel disconnected unexpectedly.', reconnect=True)
            else:
                self.drop_hits = 0
            if new == 'reconnecting' and self.state != 'reconnecting':
                self.log(f'VPN link lost: {health}. OpenVPN will retry automatically.', 'err')
                self._drop_alert(f'Link lost: {health}. Tunnel will resume when the network returns.')
                self.svc = {}
            if new == 'connected' and self.state == 'reconnecting':
                self.last_rx = (self.last_rx[0], now)
                self.log('VPN link restored.', 'ok')
                self.notify(APP_NAME, 'Link restored. Secure tunnel is active again.')
                self._clear_drop()
            elif new == 'connected' and self.state != 'connected':
                self.log('VPN Connected.', 'ok')
                self.notify('AccelPro VPN connected', 'Secure tunnel is active.')
                self._clear_drop()
            if new == 'disconnected':
                self.svc = {}
            self.state = new

        self.since = since if connected else None
        if counters and connected:
            if self.prev:
                dt = max(now - self.prev[0], 0.5)
                self.dl = max(counters.get('TUN/TAP write bytes', 0) - self.prev[1].get('TUN/TAP write bytes', 0), 0) / dt
                self.ul = max(counters.get('TUN/TAP read bytes', 0) - self.prev[1].get('TUN/TAP read bytes', 0), 0) / dt
                self.hist = (self.hist + [(self.dl, self.ul)])[-120:]
            self.prev, self.counters = (now, counters), counters
        elif not connected:
            self.prev, self.dl, self.ul, self.hist = None, 0, 0, []
        self.updated = time.strftime('%d %b %Y, %H:%M:%S')
        self.update_view()

    def _drop_alert(self, msg, reconnect=False):
        if not self.cfg.get('drop_alert', True) or self.dropped:
            return
        self.dropped, self.drop_msg = True, msg
        self.notify(APP_NAME, msg, urgent=True, reconnect=reconnect)
        self.show()
        if self.page == 'Home':
            self.render()

    def _clear_drop(self):
        if self.dropped:
            self.dropped = False
            if self.page == 'Home':
                self.render()

    # ---------- event loop ----------
    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == 'log':
                    self.log(val)
                elif kind == 'url':
                    self.open_auth(val)
                elif kind == 'connected_done':
                    self.busy = False
                    if val == 0:
                        self.log('Certificate received successfully.', 'ok')
                    elif val in (126, 127):
                        self.log('Authorization cancelled.', 'err')
                        self.state = 'unknown'
                    else:
                        self.log(f'Connection failed (code {val}). See Connection Log.', 'err')
                        self.notify(APP_NAME, 'Connection failed. Check the Connection Log.', urgent=True)
                        self.state = 'unknown'
                    self.update_view()
                    self.refresh_now()
                elif kind == 'stopped':
                    rc, out, then_quit = val
                    self.busy = False
                    if out:
                        self.log(out)
                    if then_quit:
                        if rc in (126, 127):
                            self.quitting = False
                            self.log('Quit cancelled – VPN still connected.', 'err')
                            self.state = 'unknown'
                            self.update_view()
                        else:
                            self.log('VPN disconnected. Exiting…', 'ok')
                            self.exit_now()
                            return
                    else:
                        self.state, self.drop_hits = 'unknown', 0
                        if rc not in (126, 127):
                            self.notify('AccelPro VPN disconnected', 'VPN tunnel stopped.')
                        self.update_view()
                        self.refresh_now()
                elif kind == 'refresh':
                    self.apply_refresh(*val)
                elif kind == 'svc':
                    self.svc_busy = False
                    if self.state == 'connected':
                        self.svc = val
                        self.update_view()
                elif kind == 'call':
                    val()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.root.after(120, self.poll)

    # ---------- tray ----------
    def tray_image(self, color):
        from PIL import Image, ImageDraw
        im = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.polygon([(6, 4), (58, 32), (6, 60), (20, 32)], fill=C['brand'])
        d.ellipse((42, 42, 62, 62), fill=color, outline='white', width=2)
        return im

    def start_tray(self):
        try:
            import pystray
            post = lambda f: (lambda icon, item: self.q.put(('call', f)))
            menu = pystray.Menu(
                pystray.MenuItem(lambda i: f'{APP_NAME} - {self.state.capitalize()}', None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Show Application', post(self.show), default=True),
                pystray.MenuItem('Connection Status', post(lambda: (self.show(), self.go('Home')))),
                pystray.MenuItem('Connect VPN', post(self.connect)),
                pystray.MenuItem('Disconnect VPN', post(self.disconnect)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Quit', post(self.quit)))
            self.tray = pystray.Icon('accelpro-vpn', self.tray_image(C['gray']), APP_NAME, menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception as e:
            self.tray = None
            LOG.warning('Tray unavailable: %s', e)

    def update_tray(self):
        if not self.tray or self._tray_state == self.state:
            return
        self._tray_state = self.state
        col = {'connected': C['green'], 'disconnected': C['red']}.get(self.state, C['amber'])
        try:
            self.tray.icon = self.tray_image(col)
            self.tray.title = f'{APP_NAME} - {self.state.capitalize()}'
            self.tray.update_menu()
        except Exception:
            pass


def cleanup_legacy_launcher():
    """Remove the per-user launcher left by the earlier script-based installation."""
    old = os.path.expanduser('~/.local/share/applications/AccelPro-VPN.desktop')
    try:
        with open(old) as f:
            if '.local/share/accelpro-vpn' in f.read():
                os.remove(old)
    except Exception:
        pass


def main():
    if '--version' in sys.argv:
        print(f'accelpro-vpn {VERSION}')
        return 0
    srv = single_instance()
    if srv is None:
        return 0
    cleanup_legacy_launcher()
    try:
        App(srv, minimized='--minimized' in sys.argv).root.mainloop()
    except Exception:
        LOG.exception('Fatal error')
        raise
    return 0


if __name__ == '__main__':
    sys.exit(main())
