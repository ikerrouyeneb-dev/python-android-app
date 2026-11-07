# -*- coding: utf-8 -*-
"""
Clavier Bluetooth pour Android (version améliorée)
--------------------------------------------------
Envoie le texte et les raccourcis vers un PC Windows via Bluetooth Classic
(RFCOMM / SPP). Protocole : un objet JSON par ligne.

  {"type": "text", "content": "..."}
  {"type": "key", "key": "enter"}
  {"type": "hotkey", "keys": ["ctrl", "shift", "esc"]}
  {"type": "mouse_move", "dx": 12, "dy": -5}              (déplacement relatif)
  {"type": "mouse_click", "button": "left", "count": 1}   (left/right/middle, 1 ou 2)
  {"type": "mouse_scroll", "amount": 1}                   (>0 haut, <0 bas)
  {"type": "mouse_button", "button": "left", "action": "down"}   (down/up, glisser)
"""

import json
import os
import queue
import threading
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.utils import platform

# Nom par défaut du PC (modifiable ensuite via le bouton « Choisir l'appareil »).
COMPUTER_NAME = "5G"

# UUID standard du Serial Port Profile (SPP).
SPP_UUID = "00001101-0000-1000-8000-00805F9B34FB"

APP_TITLE = "Clavier Bluetooth"

# Polices système Android contenant l'arabe (la première trouvée est utilisée).
ARABIC_FONT_CANDIDATES = [
    "/system/fonts/NotoNaskhArabic-Regular.ttf",
    "/system/fonts/NotoNaskhArabicUI-Regular.ttf",
    "/system/fonts/NotoSansArabic-Regular.ttf",
    "/system/fonts/NotoSansArabicUI-Regular.ttf",
    "/system/fonts/DroidSansArabic.ttf",
]

# Touches disponibles dans le créateur de raccourcis : (libellé, nom envoyé).
MODIFIER_KEYS = [("Ctrl", "ctrl"), ("Alt", "alt"), ("Maj", "shift"), ("Win", "win")]
FUNCTION_KEYS = [("F{}".format(i), "f{}".format(i)) for i in range(1, 13)]
SPECIAL_KEYS = [
    ("Échap", "esc"), ("Tab", "tab"), ("Entrée", "enter"), ("Espace", "space"),
    ("Retour", "backspace"), ("Suppr", "delete"), ("Début", "home"), ("Fin", "end"),
    ("Page ↑", "pageup"), ("Page ↓", "pagedown"), ("Inser", "insert"),
    ("↑", "up"), ("↓", "down"), ("←", "left"), ("→", "right"),
]
LETTER_KEYS = [(c.upper(), c) for c in "abcdefghijklmnopqrstuvwxyz"]
DIGIT_KEYS = [(str(i), str(i)) for i in range(10)]

MODIFIER_NAMES = [k for _, k in MODIFIER_KEYS]
KEY_LABELS = {}
for _group in (MODIFIER_KEYS, FUNCTION_KEYS, SPECIAL_KEYS, LETTER_KEYS, DIGIT_KEYS):
    for _label, _key in _group:
        KEY_LABELS[_key] = _label

# Couleurs
BG_APP = (0.035, 0.055, 0.09, 1)
BLUE = (0.05, 0.42, 0.72, 1)
GREEN = (0.10, 0.42, 0.31, 1)
ORANGE = (0.38, 0.28, 0.13, 1)
RED = (0.35, 0.20, 0.24, 1)
GREY = (0.32, 0.34, 0.40, 1)
KEY_BG = (0.13, 0.20, 0.31, 1)
KEY_SELECTED = (0.05, 0.55, 0.40, 1)
FAV_BG = (0.45, 0.33, 0.08, 1)


def android_sdk():
    """Numéro de version Android, ou 0 hors Android."""
    if platform != "android":
        return 0
    try:
        from jnius import autoclass
        Version = autoclass("android.os.Build$VERSION")
        return int(Version.SDK_INT)
    except Exception:
        return 0


def combo_name(keys):
    """Ex. ['ctrl', 'shift', 'esc'] -> 'Ctrl+Maj+Échap'."""
    return "+".join(KEY_LABELS.get(k, k.upper()) for k in keys)


# ======================================================================
#  Gestion Bluetooth
# ======================================================================

class BluetoothManager:
    """Connexion RFCOMM + envoi via une file d'attente et un thread dédié."""

    def __init__(self, status_callback=None):
        self.socket = None
        self.output_stream = None
        self.input_stream = None
        self.connected = False
        self.lock = threading.Lock()
        self.status_callback = status_callback
        self.send_queue = queue.Queue()
        self._stop = threading.Event()

    def _status(self, message, state="info"):
        if self.status_callback:
            Clock.schedule_once(
                lambda _dt: self.status_callback(message, state), 0
            )

    def _close_socket(self):
        with self.lock:
            self.connected = False
            self._stop.set()
            stream_out = self.output_stream
            stream_in = self.input_stream
            sock = self.socket
            self.output_stream = None
            self.input_stream = None
            self.socket = None

        for item in (stream_out, stream_in, sock):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass

    @staticmethod
    def _adapter():
        from jnius import autoclass
        BluetoothAdapter = autoclass("android.bluetooth.BluetoothAdapter")
        adapter = BluetoothAdapter.getDefaultAdapter()
        if adapter is None:
            raise RuntimeError("Ce téléphone ne prend pas en charge le Bluetooth.")
        if not adapter.isEnabled():
            raise RuntimeError(
                "Le Bluetooth est désactivé. Activez-le dans les réglages puis réessayez."
            )
        return adapter

    def list_bonded(self):
        """Retourne la liste [(nom, adresse)] des appareils déjà appairés."""
        adapter = self._adapter()
        bonded = adapter.getBondedDevices()
        result = []
        if bonded is not None:
            for device in bonded.toArray():
                address = str(device.getAddress())
                try:
                    name = device.getName()
                except Exception:
                    name = None
                result.append((str(name) if name else address, address))
        result.sort(key=lambda item: item[0].lower())
        return result

    def connect(self, device_name, device_address=None, callback=None):
        """Connexion à un appareil appairé (à exécuter dans un thread)."""

        def done(success, message):
            if callback:
                Clock.schedule_once(lambda _dt: callback(success, message), 0)

        if platform != "android":
            done(False, "L'application doit être lancée sur Android.")
            return

        try:
            from jnius import autoclass
            UUID = autoclass("java.util.UUID")
            adapter = self._adapter()

            bonded = adapter.getBondedDevices()
            if bonded is None:
                raise RuntimeError("Aucun appareil appairé sur ce téléphone.")

            target = None
            names = []
            for device in bonded.toArray():
                try:
                    name = device.getName()
                except Exception:
                    name = ""
                if name:
                    names.append(str(name))
                if device_address:
                    if str(device.getAddress()) == device_address:
                        target = device
                        break
                elif name and str(name).strip().lower() == device_name.strip().lower():
                    target = device
                    break

            if target is None:
                liste = ", ".join(names) if names else "aucun"
                raise RuntimeError(
                    "Appareil « {} » introuvable parmi les appareils appairés ({}).".format(
                        device_name, liste
                    )
                )

            self._close_socket()
            adapter.cancelDiscovery()

            sock = target.createRfcommSocketToServiceRecord(UUID.fromString(SPP_UUID))
            self._status("Connexion Bluetooth en cours…", "info")
            sock.connect()

            out_stream = sock.getOutputStream()
            in_stream = sock.getInputStream()

            with self.lock:
                self.socket = sock
                self.output_stream = out_stream
                self.input_stream = in_stream
                self.connected = True
                self._stop = threading.Event()
                self.send_queue = queue.Queue()
                stop = self._stop
                q = self.send_queue

            threading.Thread(
                target=self._writer_loop, args=(stop, q, out_stream), daemon=True
            ).start()

            self._status("Connecté au PC : " + device_name, "success")
            done(True, "Connexion réussie.")
        except Exception as exc:
            self._close_socket()
            message = str(exc) or exc.__class__.__name__
            self._status("Échec de la connexion : " + message, "error")
            done(False, message)

    def _writer_loop(self, stop, q, out_stream):
        """Envoie les messages en arrière-plan, regroupés pour plus de fluidité."""
        while not stop.is_set():
            try:
                first = q.get(timeout=0.3)
            except queue.Empty:
                continue
            batch = [first]
            while len(batch) < 200:
                try:
                    batch.append(q.get_nowait())
                except queue.Empty:
                    break
            try:
                data = "".join(
                    json.dumps(p, ensure_ascii=False) + "\n" for p in batch
                ).encode("utf-8")
                out_stream.write(data)
                out_stream.flush()
            except Exception as exc:
                if not stop.is_set():
                    self._close_socket()
                    self._status("Connexion perdue : " + str(exc), "error")
                return

    def send(self, payload):
        """Met un message en file d'attente. Retourne False si non connecté."""
        with self.lock:
            if not self.connected:
                return False
            q = self.send_queue
        q.put(payload)
        return True

    def disconnect(self):
        self._close_socket()
        self._status("Non connecté", "disconnected")


# ======================================================================
#  Widgets
# ======================================================================

class RoundedButton(Button):
    """Bouton à fond arrondi dont la couleur peut changer."""

    def __init__(self, bg=(0.12, 0.22, 0.36, 1), **kwargs):
        super().__init__(**kwargs)
        self.background_normal = ""
        self.background_down = ""
        self.background_color = (0, 0, 0, 0)
        self.color = (1, 1, 1, 1)
        self.bold = True
        self.halign = "center"
        self.valign = "middle"
        self.bind(size=lambda obj, val: setattr(obj, "text_size", val))
        with self.canvas.before:
            from kivy.graphics import Color, RoundedRectangle
            self._color = Color(*bg)
            self._rect = RoundedRectangle(
                pos=self.pos, size=self.size, radius=[dp(12)]
            )
        self.bind(pos=self._update_rect, size=self._update_rect)

    @property
    def bg_color(self):
        return tuple(self._color.rgba)

    @bg_color.setter
    def bg_color(self, value):
        self._color.rgba = value

    def _update_rect(self, *_):
        self._rect.pos = self.pos
        self._rect.size = self.size


def left_label(text, height=28, color=(0.78, 0.86, 0.96, 1), **kwargs):
    label = Label(
        text=text, size_hint_y=None, height=dp(height),
        halign="left", valign="middle", color=color, **kwargs
    )
    label.bind(size=lambda obj, val: setattr(obj, "text_size", val))
    return label


# ======================================================================
#  Widgets : répétition automatique et pavé tactile
# ======================================================================

class RepeatButton(RoundedButton):
    """Bouton qui répète son action tant qu'on le maintient appuyé.

    `action` est appelée à l'appui, puis en boucle après `delay` secondes.
    Si l'action retourne False (non connecté, échec), la répétition s'arrête.
    """

    def __init__(self, action, delay=0.4, interval=0.05, **kwargs):
        super().__init__(**kwargs)
        self.action = action
        self.delay = delay
        self.interval = interval
        self._start_ev = None
        self._rep_ev = None
        self.bind(state=self._on_state)

    def _cancel(self):
        if self._start_ev is not None:
            self._start_ev.cancel()
            self._start_ev = None
        if self._rep_ev is not None:
            self._rep_ev.cancel()
            self._rep_ev = None

    def _on_state(self, _inst, state):
        if state == "down":
            self._cancel()
            if self.action() is False:
                return
            self._start_ev = Clock.schedule_once(self._begin, self.delay)
        else:
            self._cancel()

    def _begin(self, _dt):
        self._start_ev = None
        self._rep_ev = Clock.schedule_interval(self._tick, self.interval)

    def _tick(self, _dt):
        if self.action() is False:
            self._cancel()


class TouchArea(Label):
    """Zone tactile : mode 'move' (déplacer + toucher = clic) ou 'scroll' (molette)."""

    BASE_GAIN = 2.5          # pixels PC par dp de mouvement du doigt (vitesse 1x)
    SCROLL_STEP = 12         # dp de glissement pour un cran de molette

    def __init__(self, app, mode="move", **kwargs):
        super().__init__(**kwargs)
        self.app = app
        self.mode = mode
        self._acc_x = 0.0
        self._acc_y = 0.0
        self.halign = "center"
        self.valign = "middle"
        self.bind(size=lambda obj, val: setattr(obj, "text_size", val))
        with self.canvas.before:
            Color(0.09, 0.13, 0.20, 1)
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(14)])
        self.bind(pos=self._update_rect, size=self._update_rect)
        self._ev = Clock.schedule_interval(self._flush, 1 / 30.0)

    def _update_rect(self, *_):
        self._rect.pos = self.pos
        self._rect.size = self.size

    def stop(self):
        if self._ev is not None:
            self._ev.cancel()
            self._ev = None

    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos):
            return False
        touch.grab(self)
        touch.ud["pad_t"] = time.time()
        touch.ud["pad_dist"] = 0.0
        return True

    def on_touch_move(self, touch):
        if touch.grab_current is not self:
            return False
        touch.ud["pad_dist"] = touch.ud.get("pad_dist", 0.0) + abs(touch.dx) + abs(touch.dy)
        one_dp = dp(1)
        if self.mode == "move":
            gain = self.BASE_GAIN * self.app.mouse_speed
            self._acc_x += touch.dx / one_dp * gain
            self._acc_y += -touch.dy / one_dp * gain   # l'axe Y de Kivy est inversé
        else:
            self._acc_y += touch.dy / one_dp
        return True

    def on_touch_up(self, touch):
        if touch.grab_current is not self:
            return False
        touch.ungrab(self)
        if (
            self.mode == "move"
            and touch.ud.get("pad_dist", 0.0) < dp(8)
            and time.time() - touch.ud.get("pad_t", 0) < 0.3
        ):
            self.app.send_mouse_click("left", 1)
        return True

    def _flush(self, _dt):
        if self.mode == "move":
            ix, iy = int(self._acc_x), int(self._acc_y)
            if ix or iy:
                self._acc_x -= ix
                self._acc_y -= iy
                self.app.send_mouse_move(ix, iy)
        else:
            steps = int(self._acc_y / self.SCROLL_STEP)
            if steps:
                self._acc_y -= steps * self.SCROLL_STEP
                self.app.send_mouse_scroll(steps)


# ======================================================================
#  Application
# ======================================================================

class KeyboardApp(App):
    def build(self):
        self.title = APP_TITLE
        self.bt = BluetoothManager(self.update_status)
        self.last_live_text = ""
        self.live_enabled = True
        self._ignore_text_change = False
        self._connect_busy = False

        # Configuration enregistrée (appareil choisi + raccourcis favoris)
        self.device_name = COMPUTER_NAME
        self.device_address = None
        self.favorites = []
        self.mouse_speed = 1.0
        self.mouse_label = None
        self.dragging = False
        self.load_config()

        # Police arabe (si disponible sur le téléphone)
        self.arabic_font = None
        for path in ARABIC_FONT_CANDIDATES:
            if os.path.exists(path):
                self.arabic_font = path
                break
        self.arabic_mode = False

        Window.clearcolor = BG_APP

        scroll = ScrollView(do_scroll_x=False, bar_width=dp(3))
        root = BoxLayout(
            orientation="vertical",
            padding=[dp(14), dp(12), dp(14), dp(12)],
            spacing=dp(9),
            size_hint_y=None,
        )
        root.bind(minimum_height=root.setter("height"))
        scroll.add_widget(root)

        # --- En-tête et état ---
        root.add_widget(left_label(
            "[b]Clavier Bluetooth[/b]", height=44, markup=True,
            font_size="21sp", color=(0.92, 0.96, 1, 1),
        ))
        self.status_label = left_label(
            "● Non connecté", color=(1, 0.55, 0.48, 1)
        )
        root.add_widget(self.status_label)

        # --- Choix de l'appareil et connexion ---
        device_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.device_button = RoundedButton(
            text=self.device_button_text(), bg=GREY, font_size="14sp"
        )
        self.device_button.bind(on_release=self.open_device_picker)
        device_row.add_widget(self.device_button)
        root.add_widget(device_row)

        connection_row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        self.connect_button = RoundedButton(text="Se connecter", bg=BLUE)
        self.connect_button.bind(on_release=self.on_connect)
        connection_row.add_widget(self.connect_button)
        disconnect_button = RoundedButton(text="Déconnecter", bg=RED)
        disconnect_button.bind(on_release=lambda *_: self.bt.disconnect())
        connection_row.add_widget(disconnect_button)
        root.add_widget(connection_row)

        # --- Options de saisie ---
        live_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        self.live_button = RoundedButton(
            text="Saisie directe : activée", bg=GREEN, font_size="12sp"
        )
        self.live_button.bind(on_release=self.toggle_live)
        live_row.add_widget(self.live_button)
        self.font_button = RoundedButton(
            text="Police arabe : non", bg=GREY, font_size="12sp"
        )
        self.font_button.bind(on_release=self.toggle_font)
        live_row.add_widget(self.font_button)
        clear_button = RoundedButton(text="Effacer", bg=GREY, font_size="12sp")
        clear_button.bind(on_release=self.clear_input)
        live_row.add_widget(clear_button)
        root.add_widget(live_row)

        # --- Zone de saisie ---
        self.text_input = TextInput(
            hint_text="Écrivez ici… les nouveaux caractères sont envoyés directement au PC",
            multiline=True,
            size_hint_y=None,
            height=dp(125),
            font_size="18sp",
            padding=[dp(12), dp(12)],
            foreground_color=(0.95, 0.97, 1, 1),
            hint_text_color=(0.55, 0.62, 0.72, 1),
            background_normal="",
            background_active="",
            background_color=(0.09, 0.13, 0.20, 1),
            cursor_color=(0.25, 0.72, 1, 1),
        )
        # Désactive les suggestions/correction automatique du clavier Android
        # (elles remplacent des mots entiers et désynchronisent la saisie directe).
        try:
            self.text_input.keyboard_suggestions = False
        except Exception:
            pass
        self.text_input.bind(text=self.on_text_changed)
        root.add_widget(self.text_input)

        send_row = BoxLayout(size_hint_y=None, height=dp(43), spacing=dp(8))
        send_all = RoundedButton(text="Envoyer tout le texte", bg=BLUE)
        send_all.bind(on_release=self.send_all_text)
        send_row.add_widget(send_all)
        enter_button = RoundedButton(text="Entrée", bg=(0.20, 0.30, 0.43, 1))
        enter_button.bind(on_release=lambda *_: self.send_key("enter"))
        send_row.add_widget(enter_button)
        root.add_widget(send_row)

        # --- Touches de contrôle ---
        root.add_widget(left_label(
            "[b]Navigation et correction[/b]  (maintenir = répéter)",
            height=25, markup=True,
        ))
        controls = GridLayout(
            cols=5, spacing=dp(7), size_hint_y=None,
            row_default_height=dp(44), row_force_default=True,
        )
        controls.bind(minimum_height=controls.setter("height"))
        for label, key in [
            ("Retour", "backspace"), ("Suppr", "delete"),
            ("Début", "home"), ("Fin", "end"), ("Tab", "tab"),
            ("←", "left"), ("→", "right"),
            ("↑", "up"), ("↓", "down"), ("Espace", "space"),
        ]:
            btn = RepeatButton(
                action=lambda k=key: self.send_key(k),
                text=label, bg=KEY_BG, font_size="13sp",
            )
            controls.add_widget(btn)
        root.add_widget(controls)

        # --- Raccourcis rapides ---
        root.add_widget(left_label("[b]Raccourcis rapides[/b]", height=25, markup=True))
        shortcuts = GridLayout(
            cols=3, spacing=dp(7), size_hint_y=None,
            row_default_height=dp(42), row_force_default=True,
        )
        shortcuts.bind(minimum_height=shortcuts.setter("height"))
        for label, keys, repeat in [
            ("Ctrl+C", ["ctrl", "c"], False), ("Ctrl+V", ["ctrl", "v"], False),
            ("Ctrl+X", ["ctrl", "x"], False), ("Ctrl+A", ["ctrl", "a"], False),
            ("Ctrl+Z", ["ctrl", "z"], False), ("Ctrl+S", ["ctrl", "s"], False),
            ("Ctrl+←", ["ctrl", "left"], True), ("Ctrl+→", ["ctrl", "right"], True),
            ("Alt+Tab", ["alt", "tab"], False),
        ]:
            if repeat:
                btn = RepeatButton(
                    action=lambda ks=keys: self.send_hotkey(ks),
                    delay=0.5, interval=0.12,
                    text=label, bg=(0.17, 0.25, 0.38, 1),
                )
            else:
                btn = RoundedButton(text=label, bg=(0.17, 0.25, 0.38, 1))
                btn.bind(on_release=lambda _b, ks=keys: self.send_hotkey(ks))
            shortcuts.add_widget(btn)
        root.add_widget(shortcuts)

        # --- Favoris ---
        root.add_widget(left_label(
            "[b]Raccourcis favoris[/b]  (appui long = supprimer)",
            height=25, markup=True,
        ))
        self.favorites_grid = GridLayout(
            cols=3, spacing=dp(7), size_hint_y=None,
            row_default_height=dp(42), row_force_default=True,
        )
        self.favorites_grid.bind(minimum_height=self.favorites_grid.setter("height"))
        root.add_widget(self.favorites_grid)
        self.refresh_favorites()

        # --- Créateur de raccourci ---
        tools_row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        mouse_button = RoundedButton(text="Souris (pavé tactile)", bg=GREEN, font_size="14sp")
        mouse_button.bind(on_release=self.open_mouse_pad)
        tools_row.add_widget(mouse_button)
        builder_button = RoundedButton(text="Créer un raccourci", bg=BLUE, font_size="14sp")
        builder_button.bind(on_release=self.open_shortcut_builder)
        tools_row.add_widget(builder_button)
        root.add_widget(tools_row)

        note = Label(
            text="Le programme récepteur doit tourner sur Windows et les deux appareils doivent être appairés.",
            size_hint_y=None, height=dp(40), font_size="11sp",
            color=(0.58, 0.67, 0.78, 1), halign="center", valign="middle",
        )
        note.bind(size=lambda obj, val: setattr(obj, "text_size", val))
        root.add_widget(note)

        return scroll

    # ------------------------------------------------------------------
    #  Configuration (appareil + favoris)
    # ------------------------------------------------------------------

    def config_path(self):
        return os.path.join(self.user_data_dir, "config.json")

    def load_config(self):
        try:
            with open(self.config_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            self.device_name = data.get("device_name") or COMPUTER_NAME
            self.device_address = data.get("device_address")
            speed = float(data.get("mouse_speed", 1.0))
            self.mouse_speed = speed if speed in self.MOUSE_SPEEDS else 1.0
            favs = data.get("favorites", [])
            self.favorites = [
                {"name": str(f["name"]), "keys": [str(k) for k in f["keys"]]}
                for f in favs
                if isinstance(f, dict) and f.get("keys")
            ]
        except Exception:
            pass

    def save_config(self):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self.config_path(), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "device_name": self.device_name,
                        "device_address": self.device_address,
                        "mouse_speed": self.mouse_speed,
                        "favorites": self.favorites,
                    },
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as exc:
            self.update_status("Impossible d'enregistrer : " + str(exc), "error")

    def device_button_text(self):
        return "Appareil : {}  (changer)".format(self.device_name)

    # ------------------------------------------------------------------
    #  Permissions Android
    # ------------------------------------------------------------------

    def on_start(self):
        if platform == "android":
            self.request_bt_permissions()

    def request_bt_permissions(self):
        try:
            from android.permissions import request_permissions, Permission

            permissions = []
            sdk = android_sdk()
            if sdk >= 31:
                permissions.extend([
                    Permission.BLUETOOTH_CONNECT,
                    Permission.BLUETOOTH_SCAN,
                ])
            else:
                permissions.append(Permission.BLUETOOTH)
                if sdk >= 23:
                    permissions.append(Permission.ACCESS_FINE_LOCATION)

            request_permissions(permissions, self.permission_result)
        except Exception as exc:
            self.update_status("Impossible de demander les permissions : " + str(exc), "error")

    def permission_result(self, permissions, grants):
        denied = []
        try:
            for permission, granted in zip(permissions, grants):
                if not granted:
                    denied.append(permission)
        except Exception:
            pass
        if denied:
            self.update_status(
                "Certaines permissions Bluetooth sont refusées. Autorisez-les dans les réglages de l'application.",
                "error",
            )

    # ------------------------------------------------------------------
    #  Interface et connexion
    # ------------------------------------------------------------------

    def update_status(self, message, state="info"):
        colors = {
            "success": (0.35, 0.92, 0.62, 1),
            "error": (1, 0.48, 0.43, 1),
            "disconnected": (1, 0.55, 0.48, 1),
            "info": (0.55, 0.78, 1, 1),
        }
        self.status_label.text = "● " + message
        self.status_label.color = colors.get(state, colors["info"])
        mouse_label = getattr(self, "mouse_label", None)
        if mouse_label is not None:
            mouse_label.text = "● " + message
            mouse_label.color = colors.get(state, colors["info"])

    def open_device_picker(self, *_):
        if platform != "android":
            self.update_status("Cette fonction n'existe que sur Android.", "error")
            return
        try:
            devices = self.bt.list_bonded()
        except Exception as exc:
            self.update_status("Impossible de lister les appareils : " + str(exc), "error")
            return

        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        popup = Popup(
            title="Choisir l'appareil (PC)", content=content,
            size_hint=(0.92, 0.8), auto_dismiss=True,
        )

        if not devices:
            content.add_widget(Label(
                text="Aucun appareil appairé.\nAppairez d'abord le PC dans les réglages Bluetooth du téléphone.",
                halign="center",
            ))
        else:
            scroll = ScrollView(do_scroll_x=False)
            grid = GridLayout(
                cols=1, spacing=dp(6), size_hint_y=None,
                row_default_height=dp(48), row_force_default=True,
            )
            grid.bind(minimum_height=grid.setter("height"))
            for name, address in devices:
                selected = address == self.device_address or (
                    not self.device_address and name == self.device_name
                )
                btn = RoundedButton(
                    text=("✓ " if selected else "") + "{}\n{}".format(name, address),
                    bg=KEY_SELECTED if selected else KEY_BG, font_size="14sp",
                )
                btn.bind(
                    on_release=lambda _b, n=name, a=address, p=popup:
                    self.select_device(n, a, p)
                )
                grid.add_widget(btn)
            scroll.add_widget(grid)
            content.add_widget(scroll)

        close = RoundedButton(text="Fermer", bg=GREY, size_hint_y=None, height=dp(44))
        close.bind(on_release=popup.dismiss)
        content.add_widget(close)
        popup.open()

    def select_device(self, name, address, popup):
        if self.bt.connected:
            self.bt.disconnect()
        self.device_name = name
        self.device_address = address
        self.device_button.text = self.device_button_text()
        self.save_config()
        popup.dismiss()
        self.update_status("Appareil sélectionné : " + name, "info")

    def on_connect(self, *_):
        if self._connect_busy:
            return
        if platform != "android":
            self.update_status("Cette fonction n'existe que sur Android.", "error")
            return

        self._connect_busy = True
        self.connect_button.text = "Connexion…"
        self.connect_button.disabled = True

        def finished(success, message):
            # Appelé dans le thread principal (via Clock).
            self._connect_busy = False
            self.connect_button.disabled = False
            self.connect_button.text = "Se connecter"
            if success:
                self.update_status("Connecté au PC : " + self.device_name, "success")
            else:
                self.update_status("Échec de la connexion : " + message, "error")

        threading.Thread(
            target=self.bt.connect,
            args=(self.device_name, self.device_address, finished),
            daemon=True,
        ).start()

    def toggle_live(self, *_):
        self.live_enabled = not self.live_enabled
        if self.live_enabled:
            self.live_button.text = "Saisie directe : activée"
            self.live_button.bg_color = GREEN
            self.last_live_text = self.text_input.text
            self.update_status("Saisie directe activée.", "info")
        else:
            self.live_button.text = "Saisie directe : arrêtée"
            self.live_button.bg_color = ORANGE
            self.update_status("Saisie directe arrêtée.", "info")

    def toggle_font(self, *_):
        if not self.arabic_font:
            self.update_status("Aucune police arabe trouvée sur ce téléphone.", "error")
            return
        self.arabic_mode = not self.arabic_mode
        if self.arabic_mode:
            self.text_input.font_name = self.arabic_font
            self.font_button.text = "Police arabe : oui"
            self.font_button.bg_color = GREEN
        else:
            self.text_input.font_name = "Roboto"
            self.font_button.text = "Police arabe : non"
            self.font_button.bg_color = GREY

    # ------------------------------------------------------------------
    #  Synchronisation du texte
    # ------------------------------------------------------------------

    def on_text_changed(self, _instance, new_text):
        if self._ignore_text_change:
            return
        if not self.live_enabled or not self.bt.connected:
            self.last_live_text = new_text
            return

        old_text = self.last_live_text
        if new_text == old_text:
            return

        # Ajout en fin de texte : envoyer seulement la partie nouvelle.
        if new_text.startswith(old_text):
            self.send_text_chunk(new_text[len(old_text):])
            self.last_live_text = new_text
            return

        # Suppression en fin de texte : autant de Retour arrière que de caractères.
        if old_text.startswith(new_text):
            self.send_backspaces(len(old_text) - len(new_text))
            self.last_live_text = new_text
            return

        # Modification au milieu : on met à jour la référence et on prévient.
        self.last_live_text = new_text
        self.update_status(
            "Texte modifié au milieu : arrêtez la saisie directe puis envoyez tout le texte si besoin.",
            "info",
        )

    def send_text_chunk(self, text):
        if text and not self.bt.send({"type": "text", "content": text}):
            self.update_status("Non connecté. Appuyez d'abord sur « Se connecter ».", "error")

    def send_backspaces(self, count):
        for _ in range(count):
            if not self.bt.send({"type": "key", "key": "backspace"}):
                break

    def send_all_text(self, *_):
        text = self.text_input.text
        if not text:
            return
        if not self.bt.connected:
            self.update_status("Connectez-vous d'abord au PC.", "error")
            return
        if self.bt.send({"type": "text", "content": text}):
            self.last_live_text = text
            self.update_status("Texte envoyé.", "success")
        else:
            self.update_status("Échec de l'envoi du texte.", "error")

    def clear_input(self, *_):
        self._ignore_text_change = True
        self.text_input.text = ""
        self._ignore_text_change = False
        self.last_live_text = ""
        self.update_status("Champ effacé sur le téléphone uniquement.", "info")

    # ------------------------------------------------------------------
    #  Touches et raccourcis
    # ------------------------------------------------------------------

    def send_key(self, key):
        if not self.bt.connected:
            self.update_status("Connectez-vous d'abord au PC.", "error")
            return False
        if not self.bt.send({"type": "key", "key": key}):
            self.update_status("Échec de l'envoi de la touche.", "error")
            return False
        return True

    def send_hotkey(self, keys):
        if not self.bt.connected:
            self.update_status("Connectez-vous d'abord au PC.", "error")
            return False
        if not self.bt.send({"type": "hotkey", "keys": list(keys)}):
            self.update_status("Échec de l'envoi du raccourci.", "error")
            return False
        return True

    def send_combo(self, keys):
        """Une seule touche -> 'key', sinon -> 'hotkey'."""
        if len(keys) == 1:
            return self.send_key(keys[0])
        return self.send_hotkey(keys)

    # ------------------------------------------------------------------
    #  Souris (pavé tactile)
    # ------------------------------------------------------------------

    MOUSE_SPEEDS = [0.6, 1.0, 1.5, 2.0]

    def _mouse_send(self, payload):
        if not self.bt.connected:
            self.update_status("Connectez-vous d'abord au PC.", "error")
            return False
        return self.bt.send(payload)

    def send_mouse_move(self, dx, dy):
        return self._mouse_send({"type": "mouse_move", "dx": dx, "dy": dy})

    def send_mouse_click(self, button="left", count=1):
        return self._mouse_send(
            {"type": "mouse_click", "button": button, "count": count}
        )

    def send_mouse_scroll(self, amount):
        return self._mouse_send({"type": "mouse_scroll", "amount": amount})

    def open_mouse_pad(self, *_):
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        popup = Popup(
            title="Souris", content=content,
            size_hint=(0.97, 0.95), auto_dismiss=False,
        )

        self.mouse_label = left_label(
            self.status_label.text, height=24, font_size="12sp",
            color=self.status_label.color,
        )
        content.add_widget(self.mouse_label)

        pad_row = BoxLayout(spacing=dp(8))
        pad = TouchArea(
            self, mode="move",
            text="Glissez pour déplacer le curseur\nTouchez pour cliquer",
            font_size="14sp", color=(0.55, 0.62, 0.72, 1),
        )
        strip = TouchArea(
            self, mode="scroll", text="Défiler\n↕", font_size="13sp",
            color=(0.55, 0.62, 0.72, 1), size_hint_x=None, width=dp(64),
        )
        pad_row.add_widget(pad)
        pad_row.add_widget(strip)
        content.add_widget(pad_row)

        click_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        left_btn = RoundedButton(text="Clic gauche", bg=BLUE, font_size="13sp")
        right_btn = RoundedButton(text="Clic droit", bg=BLUE, font_size="13sp")
        double_btn = RoundedButton(text="Double clic", bg=BLUE, font_size="13sp")
        left_btn.bind(on_release=lambda *_: self.send_mouse_click("left", 1))
        right_btn.bind(on_release=lambda *_: self.send_mouse_click("right", 1))
        double_btn.bind(on_release=lambda *_: self.send_mouse_click("left", 2))
        for b in (left_btn, right_btn, double_btn):
            click_row.add_widget(b)
        content.add_widget(click_row)

        opt_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        drag_btn = RoundedButton(
            text="Glisser : non", bg=GREY, font_size="13sp"
        )
        speed_btn = RoundedButton(
            text="Vitesse : {:g}x".format(self.mouse_speed), bg=GREY, font_size="13sp"
        )
        close_btn = RoundedButton(text="Fermer", bg=RED, font_size="13sp")
        for b in (drag_btn, speed_btn, close_btn):
            opt_row.add_widget(b)
        content.add_widget(opt_row)

        def toggle_drag(*_):
            want = not self.dragging
            if self._mouse_send({
                "type": "mouse_button", "button": "left",
                "action": "down" if want else "up",
            }):
                self.dragging = want
                drag_btn.text = "Glisser : oui" if want else "Glisser : non"
                drag_btn.bg_color = GREEN if want else GREY

        def cycle_speed(*_):
            speeds = self.MOUSE_SPEEDS
            i = speeds.index(self.mouse_speed) if self.mouse_speed in speeds else 1
            self.mouse_speed = speeds[(i + 1) % len(speeds)]
            speed_btn.text = "Vitesse : {:g}x".format(self.mouse_speed)
            self.save_config()

        def close(*_):
            if self.dragging:
                self.bt.send({"type": "mouse_button", "button": "left", "action": "up"})
                self.dragging = False
            pad.stop()
            strip.stop()
            self.mouse_label = None
            popup.dismiss()

        drag_btn.bind(on_release=toggle_drag)
        speed_btn.bind(on_release=cycle_speed)
        close_btn.bind(on_release=close)
        popup.open()

    # ------------------------------------------------------------------
    #  Favoris
    # ------------------------------------------------------------------

    def refresh_favorites(self):
        self.favorites_grid.clear_widgets()
        if not self.favorites:
            self.favorites_grid.cols = 1
            empty = Label(
                text="Aucun favori. Créez un raccourci puis ajoutez-le aux favoris.",
                font_size="12sp", color=(0.58, 0.67, 0.78, 1),
            )
            empty.bind(size=lambda obj, val: setattr(obj, "text_size", val))
            empty.halign = "left"
            empty.valign = "middle"
            self.favorites_grid.add_widget(empty)
            return

        self.favorites_grid.cols = 3
        for fav in self.favorites:
            self.favorites_grid.add_widget(self.make_favorite_button(fav))

    def make_favorite_button(self, fav):
        btn = RoundedButton(text=fav["name"], bg=FAV_BG, font_size="13sp")
        state = {"event": None, "long": False}

        def on_press(_btn):
            state["long"] = False

            def fire(_dt):
                state["long"] = True
                self.confirm_delete_favorite(fav)

            state["event"] = Clock.schedule_once(fire, 0.8)

        def on_release(_btn):
            if state["event"] is not None:
                state["event"].cancel()
                state["event"] = None
            if not state["long"]:
                self.send_combo(fav["keys"])

        btn.bind(on_press=on_press, on_release=on_release)
        return btn

    def add_favorite(self, keys):
        keys = list(keys)
        if not keys:
            return False
        if any(f["keys"] == keys for f in self.favorites):
            self.update_status("Ce raccourci est déjà dans les favoris.", "info")
            return False
        self.favorites.append({"name": combo_name(keys), "keys": keys})
        self.save_config()
        self.refresh_favorites()
        self.update_status("Ajouté aux favoris : " + combo_name(keys), "success")
        return True

    def confirm_delete_favorite(self, fav):
        content = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(10))
        content.add_widget(Label(
            text="Supprimer le favori « {} » ?".format(fav["name"]),
            halign="center",
        ))
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        popup = Popup(
            title="Supprimer le favori", content=content,
            size_hint=(0.85, 0.35),
        )
        yes = RoundedButton(text="Supprimer", bg=RED)
        no = RoundedButton(text="Annuler", bg=GREY)

        def do_delete(*_):
            self.favorites = [f for f in self.favorites if f["keys"] != fav["keys"]]
            self.save_config()
            self.refresh_favorites()
            popup.dismiss()
            self.update_status("Favori supprimé.", "info")

        yes.bind(on_release=do_delete)
        no.bind(on_release=popup.dismiss)
        row.add_widget(yes)
        row.add_widget(no)
        content.add_widget(row)
        popup.open()

    # ------------------------------------------------------------------
    #  Créateur de raccourci
    # ------------------------------------------------------------------

    def open_shortcut_builder(self, *_):
        selected_mods = []     # ordre fixe appliqué à l'affichage
        main_key = {"value": None}
        buttons = {}

        content = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(8))
        popup = Popup(
            title="Créer un raccourci", content=content,
            size_hint=(0.97, 0.95), auto_dismiss=False,
        )

        preview = Label(
            text="Choisissez des touches…", size_hint_y=None, height=dp(40),
            font_size="18sp", bold=True, color=(0.35, 0.92, 0.62, 1),
        )
        content.add_widget(preview)

        def current_keys():
            mods = [k for k in MODIFIER_NAMES if k in selected_mods]
            return mods + ([main_key["value"]] if main_key["value"] else [])

        def refresh():
            keys = current_keys()
            preview.text = combo_name(keys) if keys else "Choisissez des touches…"
            for key, btn in buttons.items():
                active = key in selected_mods or key == main_key["value"]
                btn.bg_color = KEY_SELECTED if active else KEY_BG

        def on_key(key):
            if key in MODIFIER_NAMES:
                if key in selected_mods:
                    selected_mods.remove(key)
                else:
                    selected_mods.append(key)
            else:
                # Une seule touche principale à la fois.
                main_key["value"] = None if main_key["value"] == key else key
            refresh()

        scroll = ScrollView(do_scroll_x=False)
        body = BoxLayout(
            orientation="vertical", spacing=dp(6), size_hint_y=None,
            padding=[0, 0, dp(4), 0],
        )
        body.bind(minimum_height=body.setter("height"))

        def section(title, keys, cols, row_height=40):
            body.add_widget(left_label(title, height=24, font_size="13sp"))
            grid = GridLayout(
                cols=cols, spacing=dp(5), size_hint_y=None,
                row_default_height=dp(row_height), row_force_default=True,
            )
            grid.bind(minimum_height=grid.setter("height"))
            for label, key in keys:
                btn = RoundedButton(text=label, bg=KEY_BG, font_size="13sp")
                btn.bind(on_release=lambda _b, k=key: on_key(k))
                buttons[key] = btn
                grid.add_widget(btn)
            body.add_widget(grid)

        section("Modificateurs (plusieurs possibles)", MODIFIER_KEYS, 4)
        section("Touches de fonction", FUNCTION_KEYS, 6)
        section("Touches spéciales", SPECIAL_KEYS, 4)
        section("Lettres", LETTER_KEYS, 7)
        section("Chiffres", DIGIT_KEYS, 5)

        scroll.add_widget(body)
        content.add_widget(scroll)

        # Boutons d'action
        row1 = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        send_btn = RoundedButton(text="Envoyer", bg=BLUE)
        fav_btn = RoundedButton(text="★ Favori", bg=FAV_BG)
        row1.add_widget(send_btn)
        row1.add_widget(fav_btn)
        content.add_widget(row1)

        row2 = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        reset_btn = RoundedButton(text="Réinitialiser", bg=GREY)
        close_btn = RoundedButton(text="Fermer", bg=RED)
        row2.add_widget(reset_btn)
        row2.add_widget(close_btn)
        content.add_widget(row2)

        def do_send(*_):
            keys = current_keys()
            if not keys:
                preview.text = "Choisissez au moins une touche."
                return
            self.send_combo(keys)

        def do_fav(*_):
            keys = current_keys()
            if not keys:
                preview.text = "Choisissez au moins une touche."
                return
            if self.add_favorite(keys):
                preview.text = "★ Ajouté : " + combo_name(keys)

        def do_reset(*_):
            del selected_mods[:]
            main_key["value"] = None
            refresh()

        send_btn.bind(on_release=do_send)
        fav_btn.bind(on_release=do_fav)
        reset_btn.bind(on_release=do_reset)
        close_btn.bind(on_release=popup.dismiss)

        popup.open()

    # ------------------------------------------------------------------
    #  Cycle de vie
    # ------------------------------------------------------------------

    def on_pause(self):
        return True

    def on_stop(self):
        self.bt.disconnect()


if __name__ == "__main__":
    KeyboardApp().run()
