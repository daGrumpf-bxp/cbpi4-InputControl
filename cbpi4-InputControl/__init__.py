# -*- coding: utf-8 -*-
"""
cbpi4-InputControl  V0.0.2
==========================
V0.0.2 :
- Routes HTTP via mécanisme officiel CBPi (@request_mapping + register)
- Page UI HTML standalone à GET /inputcontrol/ui
- Endpoint POST /inputcontrol/config pour sauvegarde via UI
- Introspection auto des GPIO utilisés par les actors-output (blacklistés)
"""

import asyncio
import json
import logging
import time
from typing import Optional

from aiohttp import web

from cbpi.api import CBPiExtension, request_mapping
from cbpi.api.config import ConfigType

logger = logging.getLogger(__name__)

# Backend GPIO : gpiozero (recommandé Bookworm/Pi5, marche aussi sur Pi <= 4).
# gpiozero choisit automatiquement le meilleur backend disponible :
# lgpio sur Bookworm, RPi.GPIO sur Bullseye, etc.
# Hors-Pi (CI, dev sur laptop) on bascule sur MockFactory pour pouvoir
# tester import + validation sans matériel.
Button = None
Device = None
_GPIO_BACKEND = "none"
_ON_PI = False

try:
    from gpiozero import Button, Device
    # Essai 1 : lgpio (le bon backend sur Bookworm)
    try:
        from gpiozero.pins.lgpio import LGPIOFactory
        Device.pin_factory = LGPIOFactory()
        _GPIO_BACKEND = "lgpio"
        _ON_PI = True
    except Exception as e_lgpio:
        # Essai 2 : laisser gpiozero choisir (sera RPi.GPIO sur Bullseye)
        try:
            # Touch the default factory ; raises if no real backend available
            _ = Device.pin_factory.pin_class  # type: ignore[attr-defined]
            _GPIO_BACKEND = type(Device.pin_factory).__name__
            _ON_PI = True
        except Exception:
            # Pas de backend réel → mock (dev hors Pi)
            from gpiozero.pins.mock import MockFactory
            Device.pin_factory = MockFactory()
            _GPIO_BACKEND = "mock"
            _ON_PI = False
            logger.warning("Aucun backend GPIO réel — MockFactory activée (mode dev)")
except ImportError:
    logger.error(
        "gpiozero non installé. Sur Bookworm : sudo apt install python3-gpiozero python3-lgpio"
    )


DEFAULT_CONFIG_JSON = "[]"
CONFIG_KEY = "input_control_config"
_PLUGIN_VERSION = "0.0.13"

# GPIO 0 et 1 sont TOUJOURS bloqués (EEPROM HAT, ID_SD/ID_SC). Pas négociable.
# Le reste (I2C, 1-Wire, SPI, UART) est détecté dynamiquement au boot.
GPIO_ALWAYS_BLACKLISTED = {0, 1}

# Mapping bus système → pins BCM concernées
# (utilisé pour construire la blacklist dynamique selon ce qui est activé)
_BUS_PINS = {
    "i2c": {2, 3},      # GPIO 2 (SDA), GPIO 3 (SCL)
    "1wire": {4},       # GPIO 4 par défaut Pi (peut varier via dtoverlay)
    "spi": {7, 8, 9, 10, 11},  # CE1, CE0, MISO, MOSI, SCLK
    "uart": {14, 15},   # TXD, RXD
}


def _detect_active_buses() -> dict:
    """
    Détecte au boot quels bus système sont actifs sur ce Pi.

    Méthode : on vérifie l'existence des device files que le kernel crée
    quand le bus est activé via dtparam/dtoverlay. C'est plus fiable que de
    parser /boot/firmware/config.txt (chemin variable, edge cases).

    Retourne un dict { "i2c": bool, "1wire": bool, "spi": bool, "uart": bool }
    """
    import os
    import glob

    i2c_on = any(os.path.exists(f"/dev/i2c-{n}") for n in (0, 1, 2, 3))

    onewire_on = (
        os.path.exists("/sys/bus/w1/devices")
        or os.path.exists("/sys/module/w1_gpio")
    )

    # spidev0.0 / spidev0.1 sont créés quand SPI est armé
    spi_on = bool(glob.glob("/dev/spidev*"))

    # /dev/serial0 (lien) est créé si enable_uart=1 ; /dev/ttyAMA0 sinon
    uart_on = (
        os.path.exists("/dev/serial0")
        or os.path.exists("/dev/ttyAMA0")
        or os.path.exists("/dev/ttyS0")
    )

    return {
        "i2c": i2c_on,
        "1wire": onewire_on,
        "spi": spi_on,
        "uart": uart_on,
    }


def _build_dynamic_blacklist(active_buses: dict) -> set:
    """
    Construit la blacklist effective : GPIO toujours bloqués
    + ceux des bus système actifs.
    """
    bl = set(GPIO_ALWAYS_BLACKLISTED)
    for bus, on in active_buses.items():
        if on:
            bl |= _BUS_PINS.get(bus, set())
    return bl


def _get_local_ip() -> str:
    """
    Best-effort détection de l'IP LAN du Pi. On ouvre un socket UDP vers
    une IP publique (sans envoyer de paquet) pour que l'OS sélectionne
    l'interface sortante, puis on lit son addr locale. Fallback : hostname.
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 53))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "<IP_CBPi>"


# --------------------------------------------------------------------------- #
# InputSource abstrait + GPIOInputSource
# --------------------------------------------------------------------------- #
class InputSource:
    def __init__(self, button_config, on_press_callback):
        self.config = button_config
        self.name = button_config.get("name", "<unnamed>")
        self.on_press = on_press_callback

    async def start(self, loop):
        raise NotImplementedError

    async def stop(self):
        raise NotImplementedError

    def describe(self):
        return {"name": self.name, "type": "abstract"}


class GPIOInputSource(InputSource):
    """
    Source bouton via gpiozero.Button. Compatible Pi 1 → Pi 5, Bullseye et
    Bookworm.

    Notes sur le debounce (V0.0.7+) :
    - On NE passe PAS bounce_time à gpiozero : le backend lgpio implémente le
      debounce de manière "pré-trigger" (le signal doit rester stable pendant
      tout le bounce_time AVANT que l'event soit émis). Pour un click humain
      court, ça donne l'impression que rien ne se passe.
    - À la place, on déclenche IMMÉDIATEMENT sur le premier edge, puis on
      ignore tous les events suivants pendant `debounce_ms` millisecondes.
      C'est le comportement intuitif type RPi.GPIO `bouncetime`.
    - Le debounce est lu au niveau `button.debounce_ms` (nouveau, V0.0.7+),
      avec fallback `source.debounce_ms` pour rétro-compat.
    """

    def __init__(self, button_config, on_press_callback, loop):
        super().__init__(button_config, on_press_callback)
        src = button_config.get("source", {})
        self.pin = int(src.get("pin"))
        self.pull = src.get("pull", "up").lower()
        self.edge = src.get("edge", "falling").lower()
        # V0.0.7 : debounce remonte au niveau bouton, fallback source.debounce_ms
        self.debounce_ms = int(
            button_config.get("debounce_ms",
                              src.get("debounce_ms", 300))
        )
        self._loop = loop
        self._button: Optional["Button"] = None
        self._armed = False
        self._press_count = 0
        self._ignored_count = 0    # nombre d'events filtrés par debounce
        self._last_press_ts: Optional[float] = None
        self._last_event_monotonic: float = 0.0  # pour le filtre debounce

    async def start(self, loop):
        if Button is None:
            raise RuntimeError("gpiozero indisponible — impossible d'armer GPIO%d" % self.pin)

        # Mapping pull
        if self.pull == "up":
            pull_up = True
            active_state = None
        elif self.pull == "down":
            pull_up = False
            active_state = None
        else:  # "none"
            pull_up = None
            active_state = True

        # IMPORTANT : on passe bounce_time=None à gpiozero pour avoir une
        # détection IMMÉDIATE. Le debounce est fait dans _on_event en Python.
        try:
            self._button = Button(
                self.pin,
                pull_up=pull_up,
                active_state=active_state,
                bounce_time=None,
            )

            if self.edge == "falling":
                if self.pull == "up":
                    self._button.when_pressed = self._on_event
                else:
                    self._button.when_released = self._on_event
            elif self.edge == "rising":
                if self.pull == "up":
                    self._button.when_released = self._on_event
                else:
                    self._button.when_pressed = self._on_event
            else:  # both
                self._button.when_pressed = self._on_event
                self._button.when_released = self._on_event

            self._armed = True
            logger.info(
                "[%s] GPIO%d armé (gpiozero) pull=%s edge=%s deb=%dms (post-trigger)",
                self.name, self.pin, self.pull, self.edge, self.debounce_ms,
            )
        except Exception as e:
            logger.error("[%s] Armement GPIO%d KO: %s", self.name, self.pin, e)
            self._armed = False
            if self._button is not None:
                try:
                    self._button.close()
                except Exception:
                    pass
                self._button = None
            raise

    async def stop(self):
        try:
            if self._button is not None:
                self._button.close()
                logger.info("[%s] GPIO%d désarmé", self.name, self.pin)
        except Exception as e:
            logger.warning("[%s] close() KO: %s", self.name, e)
        finally:
            self._button = None
            self._armed = False

    def _on_event(self):
        """
        Callback appelé par gpiozero dans son thread interne.
        Applique le debounce "post-trigger" : on déclenche immédiatement
        au premier event, puis on ignore tout pendant debounce_ms.
        """
        import time as _t
        now = _t.monotonic()
        elapsed_ms = (now - self._last_event_monotonic) * 1000.0
        if self._last_event_monotonic > 0 and elapsed_ms < self.debounce_ms:
            self._ignored_count += 1
            logger.debug("[%s] GPIO%d rebond ignoré (%.0fms < %dms)",
                         self.name, self.pin, elapsed_ms, self.debounce_ms)
            return
        self._last_event_monotonic = now
        self._press_count += 1
        self._last_press_ts = time.time()
        logger.debug("[%s] GPIO%d event (count=%d)",
                     self.name, self.pin, self._press_count)
        try:
            asyncio.run_coroutine_threadsafe(self.on_press(self.config), self._loop)
        except Exception as e:
            logger.error("[%s] Schedule on_press KO: %s", self.name, e)

    def describe(self):
        return {
            "name": self.name, "type": "gpio", "pin": self.pin,
            "pull": self.pull, "edge": self.edge, "debounce_ms": self.debounce_ms,
            "armed": self._armed,
            "press_count": self._press_count,
            "ignored_count": self._ignored_count,
            "last_press_ts": self._last_press_ts,
            "backend": _GPIO_BACKEND,
        }


SOURCE_CLASSES = {"gpio": GPIOInputSource}


def build_source(button_config, on_press_callback, loop, mux_registry=None):
    src_type = button_config.get("source", {}).get("type")
    cls = SOURCE_CLASSES.get(src_type)
    if cls is None:
        return None
    try:
        # PCF8574InputSource a besoin du registry pour partager les muxers
        if src_type == "pcf8574":
            return cls(button_config, on_press_callback, loop, mux_registry)
        return cls(button_config, on_press_callback, loop)
    except Exception as e:
        logger.error("Build source %s KO: %s", src_type, e)
        return None


# --------------------------------------------------------------------------- #
# PCF8574 — Multiplexer partagé + InputSource léger par bouton
# --------------------------------------------------------------------------- #
class PCF8574Multiplexer:
    """
    Service partagé entre tous les boutons attachés au même PCF8574.

    Responsabilités :
    - Tient une connexion I2C ouverte vers le chip (smbus2)
    - Arme le GPIO INT du Pi (via gpiozero.Button, falling edge)
    - Au déclenchement INT : lit l'octet I2C, diff avec l'état précédent,
      dispatch vers les callbacks des pins concernées
    - Au boot : met toutes les pins du PCF en mode INPUT (= écrit 0xFF, ce
      qui active le quasi-pull-up interne sur chaque pin)

    Note debounce : le multiplexer N'APPLIQUE PAS de debounce. Chaque
    PCF8574InputSource applique son propre debounce post-trigger côté
    Python sur son pin, ce qui est cohérent avec GPIOInputSource.
    Le GPIO INT est armé SANS debounce non plus (le PCF8574 déclenche INT
    une fois par état stable, pas par rebond mécanique).
    """

    # Adresses I2C valides pour PCF8574 (0x20-0x27) et PCF8574A (0x38-0x3F)
    VALID_ADDRESSES = set(range(0x20, 0x28)) | set(range(0x38, 0x40))

    def __init__(self, address: int, int_gpio: int, i2c_bus: int = 1):
        self.address = address
        self.int_gpio = int_gpio
        self.i2c_bus = i2c_bus
        self._smbus = None
        self._int_button = None
        self._last_state: int = 0xFF  # Au repos : tout est HIGH (pull-up)
        # callbacks par pin : { pin_index (0-7) : [(button_config, on_press_cb), ...] }
        self._pin_callbacks: dict = {}
        self._loop = None
        self._read_count = 0
        self._last_read_ts = None
        self._armed = False

    def register_pin(self, pin: int, button_config: dict, on_press_cb):
        """Enregistre un bouton sur une pin P0-P7. À appeler avant start()."""
        if not (0 <= pin <= 7):
            raise ValueError(f"PCF8574 pin doit être 0-7, reçu {pin}")
        self._pin_callbacks.setdefault(pin, []).append((button_config, on_press_cb))

    async def start(self, loop):
        """Ouvre I2C, met les pins en input, arme l'INT GPIO."""
        self._loop = loop
        try:
            import smbus2
        except ImportError:
            raise RuntimeError(
                "smbus2 manquant pour PCF8574 — "
                "sudo apt install python3-smbus2"
            )
        # Ouvre le bus I2C
        try:
            self._smbus = smbus2.SMBus(self.i2c_bus)
        except Exception as e:
            raise RuntimeError(
                f"Impossible d'ouvrir /dev/i2c-{self.i2c_bus} : {e}"
            )
        # Met toutes les pins en mode "input" en écrivant 0xFF
        # (quasi-bidirectional : un 1 écrit active le pull-up interne et la
        # pin devient une entrée, prête à être tirée à GND par le bouton)
        try:
            self._smbus.write_byte(self.address, 0xFF)
            # Lecture initiale pour synchroniser _last_state
            self._last_state = self._smbus.read_byte(self.address)
        except Exception as e:
            raise RuntimeError(
                f"PCF8574 @0x{self.address:02x} ne répond pas sur /dev/i2c-{self.i2c_bus} : {e}"
            )
        # Arme le GPIO INT — falling edge, pull-up interne du Pi, SANS debounce
        # car le PCF8574 latch déjà l'INT (pas de rebond côté chip).
        if Button is None:
            raise RuntimeError("gpiozero indisponible")
        self._int_button = Button(
            self.int_gpio,
            pull_up=True,
            active_state=None,
            bounce_time=None,
        )
        self._int_button.when_pressed = self._on_int
        self._armed = True
        logger.info(
            "PCF8574 mux armé : @0x%02x sur /dev/i2c-%d, INT=GPIO%d, %d bouton(s)",
            self.address, self.i2c_bus, self.int_gpio,
            sum(len(v) for v in self._pin_callbacks.values()),
        )

    async def stop(self):
        if self._int_button is not None:
            try:
                self._int_button.close()
            except Exception as e:
                logger.warning("PCF8574 INT close KO: %s", e)
            self._int_button = None
        if self._smbus is not None:
            try:
                self._smbus.close()
            except Exception as e:
                logger.warning("PCF8574 I2C close KO: %s", e)
            self._smbus = None
        self._armed = False
        logger.info("PCF8574 mux désarmé : @0x%02x", self.address)

    def _on_int(self):
        """
        Callback de l'INT GPIO (thread gpiozero). Lit I2C, diff, dispatch.
        Tourne dans le thread de gpiozero — toute action sur les callbacks
        des boutons doit être schedulée sur le loop asyncio.
        """
        import time as _t
        try:
            current = self._smbus.read_byte(self.address)
        except Exception as e:
            logger.error("PCF8574 @0x%02x read KO: %s", self.address, e)
            return
        self._read_count += 1
        self._last_read_ts = _t.time()
        # XOR pour trouver les bits qui ont changé
        changed = self._last_state ^ current
        # Pour chaque bit qui a basculé de 1 → 0 (= front descendant = pression)
        for pin in range(8):
            bit_mask = 1 << pin
            if changed & bit_mask:
                # Le bit a changé. Front descendant si current bit == 0
                bit_now = (current >> pin) & 1
                if bit_now == 0:
                    # Pression détectée sur ce pin
                    for (btn_cfg, cb) in self._pin_callbacks.get(pin, []):
                        try:
                            asyncio.run_coroutine_threadsafe(cb(btn_cfg, pin), self._loop)
                        except Exception as e:
                            logger.error("PCF8574 dispatch P%d KO: %s", pin, e)
        self._last_state = current

    def describe(self):
        return {
            "type": "pcf8574_mux",
            "address": f"0x{self.address:02x}",
            "i2c_bus": self.i2c_bus,
            "int_gpio": self.int_gpio,
            "armed": self._armed,
            "registered_pins": sorted(self._pin_callbacks.keys()),
            "last_state_hex": f"0x{self._last_state:02x}",
            "read_count": self._read_count,
            "last_read_ts": self._last_read_ts,
        }


class PCF8574InputSource(InputSource):
    """
    Source bouton via une pin P0-P7 d'un PCF8574, déclenchée par INT.

    Cette classe est légère : elle s'enregistre auprès d'un PCF8574Multiplexer
    partagé (un par couple address+int_gpio). Le multiplexer fait tout le
    boulot I2C ; cette classe gère uniquement l'identité du bouton et le
    debounce post-trigger Python.

    Config attendue :
        {
            "source": {
                "type": "pcf8574",
                "address": "0x21",       (ou int 0x21)
                "int_gpio": 24,           (GPIO BCM du Pi câblé sur INT)
                "pin": 3,                 (P0-P7 sur le chip)
                "i2c_bus": 1              (optionnel, défaut /dev/i2c-1)
            },
            "debounce_ms": 300,
            ...
        }
    """

    def __init__(self, button_config, on_press_callback, loop, mux_registry):
        super().__init__(button_config, on_press_callback)
        src = button_config.get("source", {})
        # Parse address (accepte "0x21", 33, "33")
        addr_raw = src.get("address")
        if isinstance(addr_raw, str):
            self.address = int(addr_raw, 16) if addr_raw.startswith("0x") else int(addr_raw)
        else:
            self.address = int(addr_raw)
        self.int_gpio = int(src.get("int_gpio"))
        self.pin = int(src.get("pin"))
        self.i2c_bus = int(src.get("i2c_bus", 1))
        self.debounce_ms = int(
            button_config.get("debounce_ms", src.get("debounce_ms", 300))
        )
        self._loop = loop
        self._mux_registry = mux_registry
        self._mux: Optional[PCF8574Multiplexer] = None
        self._armed = False
        self._press_count = 0
        self._ignored_count = 0
        self._last_press_ts: Optional[float] = None
        self._last_event_monotonic: float = 0.0

    async def start(self, loop):
        # Find or create the multiplexer for (address, int_gpio, i2c_bus)
        key = (self.address, self.int_gpio, self.i2c_bus)
        mux = self._mux_registry.get(key)
        if mux is None:
            mux = PCF8574Multiplexer(self.address, self.int_gpio, self.i2c_bus)
            self._mux_registry[key] = mux
        self._mux = mux
        mux.register_pin(self.pin, self.config, self._dispatch_press)
        # mux.start() est appelé par le plugin globalement après tous les register_pin
        self._armed = True
        logger.info(
            "[%s] PCF8574 input @0x%02x P%d (INT=GPIO%d, deb=%dms)",
            self.name, self.address, self.pin, self.int_gpio, self.debounce_ms,
        )

    async def stop(self):
        # Le démontage du mux est géré globalement par le plugin
        self._mux = None
        self._armed = False

    async def _dispatch_press(self, button_config, pin):
        """
        Coroutine appelée par le mux quand sa pin a vu un front descendant.
        Applique le debounce post-trigger et schedule le callback utilisateur.
        """
        import time as _t
        now = _t.monotonic()
        elapsed_ms = (now - self._last_event_monotonic) * 1000.0
        if self._last_event_monotonic > 0 and elapsed_ms < self.debounce_ms:
            self._ignored_count += 1
            logger.debug("[%s] PCF8574 P%d rebond ignoré (%.0fms)",
                         self.name, pin, elapsed_ms)
            return
        self._last_event_monotonic = now
        self._press_count += 1
        self._last_press_ts = _t.time()
        await self.on_press(button_config)

    def describe(self):
        return {
            "name": self.name, "type": "pcf8574",
            "address": f"0x{self.address:02x}",
            "int_gpio": self.int_gpio,
            "pin": self.pin,
            "i2c_bus": self.i2c_bus,
            "debounce_ms": self.debounce_ms,
            "armed": self._armed,
            "press_count": self._press_count,
            "ignored_count": self._ignored_count,
            "last_press_ts": self._last_press_ts,
        }


# Ajoute la nouvelle source au factory
SOURCE_CLASSES["pcf8574"] = PCF8574InputSource


# --------------------------------------------------------------------------- #
# UI HTML
# --------------------------------------------------------------------------- #
UI_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>InputControl — Config</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 1100px;
         margin: 1em auto; padding: 0 1em; color: #222; }
  h1 { margin-bottom: 0.2em; }
  .subtitle { color: #666; margin-bottom: 1.5em; }
  table { border-collapse: collapse; width: 100%; margin: 1em 0; table-layout: auto; }
  th, td { border: 1px solid #ddd; padding: 6px 8px; text-align: left;
           vertical-align: middle; }
  th { background: #f5f5f5; font-weight: 600; }
  input[type=text], input[type=number], select {
    width: 100%; padding: 4px 6px; box-sizing: border-box;
    border: 1px solid #ccc; border-radius: 3px; font-size: 14px;
  }
  /* Largeurs minimales par colonne pour éviter la troncature des selects */
  #buttons th:nth-child(1), #buttons td:nth-child(1) { min-width: 120px; }  /* Nom */
  #buttons th:nth-child(2), #buttons td:nth-child(2) { min-width: 95px; }   /* Type */
  #buttons th:nth-child(3), #buttons td:nth-child(3) { min-width: 110px; }  /* GPIO/PCF */
  #buttons th:nth-child(4), #buttons td:nth-child(4) { min-width: 105px; }  /* Pull/INT */
  #buttons th:nth-child(5), #buttons td:nth-child(5) { min-width: 80px; }   /* Edge */
  #buttons th:nth-child(6), #buttons td:nth-child(6) { min-width: 85px; }   /* Debounce */
  #buttons th:nth-child(7), #buttons td:nth-child(7) { min-width: 200px; }  /* Actor */
  #buttons th:nth-child(8), #buttons td:nth-child(8) { min-width: 85px; }   /* Action */
  #buttons th:nth-child(9), #buttons td:nth-child(9) { width: 50px; text-align: center; }
  #buttons th:nth-child(10), #buttons td:nth-child(10) { width: 40px; }
  button { padding: 6px 12px; font-size: 14px; cursor: pointer;
           border: 1px solid #888; background: #fafafa; border-radius: 3px; }
  button.primary { background: #2a7; color: white; border-color: #285; }
  button.danger { background: #d44; color: white; border-color: #b22; }
  .actions { margin: 1em 0; display: flex; gap: 0.5em; }
  .status { background: #f0f4f8; border-left: 4px solid #4a90e2;
            padding: 0.6em 1em; margin: 1em 0; font-family: monospace;
            font-size: 13px; white-space: pre-wrap; }
  .blacklist-info { background: #fef9e7; border-left: 4px solid #f39c12;
                    padding: 0.6em 1em; margin: 1em 0; font-size: 13px; }
  .alert-error { background: #fdecea; border-left: 4px solid #d44;
                 padding: 0.8em 1em; margin: 1em 0; font-weight: 500; }
  .flash-press { background-color: #c8f7c5 !important;
                 transition: background-color 0.4s ease-out; }
  small.help { color: #888; font-size: 12px; display: block; margin-top: 0.5em; }
</style>
</head>
<body>
<h1>🎛 InputControl</h1>
<div class="subtitle">Mappe des boutons physiques (GPIO) vers des actions CBPi</div>

<div id="alert" class="alert-error" style="display:none"></div>
<div id="bl-info" class="blacklist-info">Chargement…</div>

<table id="buttons">
  <thead>
    <tr>
      <th>Nom</th>
      <th>Type</th>
      <th>GPIO / PCF</th>
      <th>Pull</th>
      <th>Edge</th>
      <th>Debounce&nbsp;ms</th>
      <th>Actor</th>
      <th>Action</th>
      <th>Notif</th>
      <th></th>
    </tr>
  </thead>
  <tbody id="rows"></tbody>
</table>

<div class="actions">
  <button onclick="addRow()">➕ Ajouter un bouton</button>
  <button class="primary" onclick="save()">💾 Sauvegarder &amp; appliquer</button>
  <button onclick="loadAll()">🔄 Recharger</button>
</div>

<h3>État courant</h3>
<div id="status" class="status">Chargement…</div>

<script>
let actors = [];
let busyGPIOs = new Set();
let blacklist = new Set();
const allGPIOs = [...Array(28).keys()];

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(url + ' → ' + r.status);
  return r.json();
}

async function loadActors() {
  try {
    const resp = await fetchJSON('/actor/');
    // CBPi renvoie {data: [...], types: [...]}
    const arr = Array.isArray(resp) ? resp : (resp && resp.data) || [];
    actors = arr.map(a => ({
      id: a.id, name: a.name, type: a.type,
      gpio: a.props && a.props.GPIO
    }));
    console.log('Loaded actors:', actors.length);
  } catch (e) {
    console.error('Actors KO', e);
    actors = [];
  }
  busyGPIOs = new Set(
    actors.filter(a => a.type === 'GPIOActor' && typeof a.gpio === 'number')
          .map(a => a.gpio)
  );
}

let blacklistReasons = {};  // {"4": ["1wire"], "2": ["i2c"], ...}
let activeBuses = {};

async function loadStatus() {
  const s = await fetchJSON('/inputcontrol/status');
  blacklist = new Set(s.blacklist || []);
  blacklistReasons = s.blacklist_reasons || {};
  activeBuses = s.active_buses || {};
  (s.busy_output_gpios || []).forEach(g => busyGPIOs.add(g));

  updateStatusDisplay(s);

  // Bandeau d'info enrichi : GPIO bloqués avec raison, bus actifs, etc.
  const blParts = [...blacklist].sort((a,b)=>a-b).map(g => {
    const reasons = blacklistReasons[String(g)] || [];
    return reasons.length ? `${g}(${reasons.join('+')})` : String(g);
  });
  const busyPart = [...busyGPIOs].sort((a,b)=>a-b).join(', ') || 'aucun';
  const busesPart = Object.entries(activeBuses)
    .map(([n, on]) => `${n}=${on ? '✓' : '✗'}`).join(' ');
  document.getElementById('bl-info').innerHTML =
    `<b>GPIO bloqués</b> : ${blParts.join(', ')} &nbsp; · &nbsp; ` +
    `<b>Actors output</b> : ${busyPart} &nbsp; · &nbsp; ` +
    `<b>Bus système</b> : ${busesPart} &nbsp; · &nbsp; ` +
    `<b>Backend</b> : ${s.gpio_backend || '?'}`;
}

function updateStatusDisplay(s) {
  const lines = [];
  lines.push(`Active sources : ${s.active_sources}`);
  lines.push(`Running on Pi  : ${s.running_on_pi}`);
  lines.push(`Backend GPIO   : ${s.gpio_backend}`);
  if (s.sources && s.sources.length) {
    lines.push('');
    s.sources.forEach(src => {
      const ts = src.last_press_ts
        ? new Date(src.last_press_ts * 1000).toLocaleTimeString()
        : 'jamais';
      const ign = (src.ignored_count != null) ? `  ignorés=${src.ignored_count}` : '';
      lines.push(`  • ${src.name}  pin=${src.pin}  armé=${src.armed}  presses=${src.press_count}${ign}  dernière=${ts}`);
    });
  }
  if (s.last_load_errors && s.last_load_errors.length) {
    lines.push('');
    lines.push('Erreurs :');
    s.last_load_errors.forEach(e => lines.push('  ✗ ' + e));
  }
  document.getElementById('status').textContent = lines.join('\n');
}

async function loadConfig() {
  const c = await fetchJSON('/inputcontrol/config');
  const rows = document.getElementById('rows');
  rows.innerHTML = '';
  (c.buttons || []).forEach(b => addRow(b));
}

function addRow(b) {
  b = b || {
    name: '',
    source: {type: 'gpio', pin: null, pull: 'up', edge: 'falling'},
    debounce_ms: 300,
    action: {actor: '', do: 'toggle'},
    notify: true,
  };
  const tr = document.createElement('tr');
  // On stocke le type courant sur la <tr> pour readRows et availability
  tr.dataset.sourceType = (b.source && b.source.type) || 'gpio';

  function td(child) { const t = document.createElement('td'); t.appendChild(child); tr.appendChild(t); return t; }

  // --- Colonne 0 : Nom ---
  const inpName = document.createElement('input');
  inpName.type = 'text';
  inpName.value = b.name || '';
  inpName.placeholder = 'ex: stop_pompe';
  inpName.addEventListener('input', refreshActorAvailability);
  td(inpName);

  // --- Colonne 1 : Type de source ---
  const selType = document.createElement('select');
  [['gpio', 'GPIO'], ['pcf8574', 'PCF8574']].forEach(([v, label]) => {
    const o = new Option(label, v);
    if (tr.dataset.sourceType === v) o.selected = true;
    selType.appendChild(o);
  });
  selType.addEventListener('change', () => {
    tr.dataset.sourceType = selType.value;
    renderSourceCells();
    refreshGPIOAvailability();
  });
  td(selType);

  // --- Colonne 2 : Source (variable selon type) ---
  // Pour GPIO : un <select> GPIO 0-27
  // Pour PCF8574 : un mini-form avec address + INT GPIO + Pin P0-P7
  const tdSource = document.createElement('td');
  tr.appendChild(tdSource);

  // --- Colonne 3 : Pull (utilisée seulement pour GPIO) ---
  const tdPull = document.createElement('td');
  tr.appendChild(tdPull);

  // --- Colonne 4 : Edge (utilisée seulement pour GPIO) ---
  const tdEdge = document.createElement('td');
  tr.appendChild(tdEdge);

  // Helper : crée le contenu des cellules 2-4 selon le type courant
  function renderSourceCells() {
    tdSource.innerHTML = '';
    tdPull.innerHTML = '';
    tdEdge.innerHTML = '';
    if (tr.dataset.sourceType === 'gpio') {
      // GPIO direct
      const selGpio = document.createElement('select');
      selGpio.className = 'gpio-select';
      selGpio.appendChild(new Option('— choisir —', ''));
      allGPIOs.forEach(g => {
        const opt = new Option(`GPIO ${g}`, g);
        if (b.source && b.source.pin === g) opt.selected = true;
        selGpio.appendChild(opt);
      });
      selGpio.addEventListener('change', refreshGPIOAvailability);
      tdSource.appendChild(selGpio);

      const selPull = document.createElement('select');
      ['up','down','none'].forEach(v => {
        const o = new Option(v, v);
        if (b.source && b.source.pull === v) o.selected = true;
        selPull.appendChild(o);
      });
      tdPull.appendChild(selPull);

      const selEdge = document.createElement('select');
      ['falling','rising','both'].forEach(v => {
        const o = new Option(v, v);
        if (b.source && b.source.edge === v) o.selected = true;
        selEdge.appendChild(o);
      });
      tdEdge.appendChild(selEdge);
    } else if (tr.dataset.sourceType === 'pcf8574') {
      // PCF8574 : addr + INT GPIO + Pin P0-P7 dans la même cellule source
      // (cellule source contient un wrapper avec 3 sous-inputs)
      const wrap = document.createElement('div');
      wrap.style.display = 'flex';
      wrap.style.gap = '4px';
      wrap.style.flexDirection = 'column';

      // Address PCF
      const selAddr = document.createElement('select');
      selAddr.className = 'pcf-addr';
      ['0x20','0x21','0x22','0x23','0x24','0x25','0x26','0x27',
       '0x38','0x39','0x3a','0x3b','0x3c','0x3d','0x3e','0x3f'].forEach(a => {
        const o = new Option(`@${a}`, a);
        // pré-sélection (normalisation hex)
        const cur = (b.source && b.source.address) ? String(b.source.address).toLowerCase() : '';
        if (cur === a.toLowerCase()) o.selected = true;
        selAddr.appendChild(o);
      });
      selAddr.addEventListener('change', refreshGPIOAvailability);
      wrap.appendChild(selAddr);

      // Pin P0-P7
      const selPin = document.createElement('select');
      selPin.className = 'pcf-pin';
      for (let i = 0; i < 8; i++) {
        const o = new Option(`P${i}`, i);
        if (b.source && b.source.pin === i) o.selected = true;
        selPin.appendChild(o);
      }
      selPin.addEventListener('change', refreshGPIOAvailability);
      wrap.appendChild(selPin);

      tdSource.appendChild(wrap);

      // Colonne Pull : INT GPIO (réutilisée car libre pour PCF)
      const selInt = document.createElement('select');
      selInt.className = 'pcf-int';
      selInt.appendChild(new Option('INT GPIO ?', ''));
      allGPIOs.forEach(g => {
        const opt = new Option(`GPIO ${g}`, g);
        if (b.source && b.source.int_gpio === g) opt.selected = true;
        selInt.appendChild(opt);
      });
      selInt.addEventListener('change', refreshGPIOAvailability);
      tdPull.appendChild(selInt);

      // Colonne Edge : libellé "—" (non applicable)
      const span = document.createElement('span');
      span.textContent = '—';
      span.style.color = '#aaa';
      span.style.display = 'block';
      span.style.textAlign = 'center';
      span.title = 'Non applicable : le PCF8574 gère lui-même le front via INT';
      tdEdge.appendChild(span);
    }
  }

  renderSourceCells();

  // --- Colonne 5 : Debounce ---
  const inpDeb = document.createElement('input');
  inpDeb.type = 'number';
  inpDeb.min = 0;
  let debValue = 300;
  if (b.debounce_ms != null) debValue = b.debounce_ms;
  else if (b.source && b.source.debounce_ms != null) debValue = b.source.debounce_ms;
  inpDeb.value = debValue;
  td(inpDeb);

  // --- Colonne 6 : Actor ---
  const selActor = document.createElement('select');
  selActor.appendChild(new Option('— choisir —', ''));
  actors.forEach(a => {
    const o = new Option(a.name, a.id);
    if (b.action && b.action.actor === a.id) o.selected = true;
    selActor.appendChild(o);
  });
  selActor.addEventListener('change', refreshActorAvailability);
  td(selActor);

  // --- Colonne 7 : Action ---
  const selDo = document.createElement('select');
  ['on','off','toggle'].forEach(v => {
    const o = new Option(v, v);
    if (b.action && b.action.do === v) o.selected = true;
    selDo.appendChild(o);
  });
  td(selDo);

  // --- Colonne 8 : Notif ---
  const inpNotif = document.createElement('input');
  inpNotif.type = 'checkbox';
  inpNotif.checked = !!b.notify;
  const tdNotif = td(inpNotif);
  tdNotif.style.textAlign = 'center';

  // --- Colonne 9 : Delete ---
  const btnDel = document.createElement('button');
  btnDel.textContent = '✕';
  btnDel.className = 'danger';
  btnDel.onclick = () => {
    tr.remove();
    refreshActorAvailability();
    refreshGPIOAvailability();
  };
  td(btnDel);

  document.getElementById('rows').appendChild(tr);
  refreshActorAvailability();
  refreshGPIOAvailability();
}

/**
 * Met à jour les <option> des selects GPIO/PCF pour griser les pins
 * déjà utilisées par d'autres boutons. Travaille sur :
 * - selects GPIO direct (.gpio-select) → grise GPIO occupés ailleurs
 * - selects INT GPIO PCF8574 (.pcf-int) → grise GPIO occupés ailleurs
 * - selects pin PCF8574 (.pcf-pin)     → grise pins occupées sur même address
 */
function refreshGPIOAvailability() {
  const rows = document.querySelectorAll('#rows tr');

  // Collecter les usages
  const usedGPIO = {};        // gpio_pin → row_name
  const usedPcfPin = {};      // "0x21:P3" → row_name

  rows.forEach(tr => {
    const myName = tr.querySelectorAll('td')[0].querySelector('input').value.trim() || '?';
    const type = tr.dataset.sourceType;
    if (type === 'gpio') {
      const sel = tr.querySelector('.gpio-select');
      const v = sel && sel.value;
      if (v !== '' && v != null) usedGPIO[parseInt(v, 10)] = myName;
    } else if (type === 'pcf8574') {
      const intSel = tr.querySelector('.pcf-int');
      const v = intSel && intSel.value;
      if (v !== '' && v != null) {
        // L'INT GPIO peut être partagé entre plusieurs boutons sur le même chip
        // (legitime), donc on ne grise que pour LES AUTRES boutons. On marque
        // comme partagé.
        const key = parseInt(v, 10);
        usedGPIO[key] = usedGPIO[key] ? usedGPIO[key] + ',' + myName : myName;
      }
      const addrSel = tr.querySelector('.pcf-addr');
      const pinSel = tr.querySelector('.pcf-pin');
      const addr = addrSel && addrSel.value;
      const pin = pinSel && pinSel.value;
      if (addr && pin !== '' && pin != null) {
        usedPcfPin[`${addr}:P${pin}`] = myName;
      }
    }
  });

  // Maintenant pour chaque ligne, update les selects
  rows.forEach(tr => {
    const myName = tr.querySelectorAll('td')[0].querySelector('input').value.trim() || '?';
    const type = tr.dataset.sourceType;

    if (type === 'gpio') {
      const sel = tr.querySelector('.gpio-select');
      if (!sel) return;
      Array.from(sel.options).forEach(opt => {
        if (!opt.value) return;
        const g = parseInt(opt.value, 10);
        let label = `GPIO ${g}`;
        let disabled = false;
        // Système (blacklist)
        if (blacklist.has(g)) {
          const reasons = blacklistReasons[String(g)] || [];
          label += reasons.length ? ` (${reasons.join('+')})` : ' (système)';
          disabled = true;
        }
        // Actor output existant
        else if (busyGPIOs.has(g)) { label += ' (utilisé en sortie)'; disabled = true; }
        // Utilisé par un autre bouton/INT (on s'exclut)
        else if (usedGPIO[g] && usedGPIO[g] !== myName) {
          label += ` (utilisé par ${usedGPIO[g]})`;
          disabled = true;
        }
        opt.textContent = label;
        opt.disabled = disabled;
      });
    } else if (type === 'pcf8574') {
      // INT GPIO : grise ceux occupés par un GPIO direct ou un autre INT de
      // PCF différent. Pour le MÊME PCF (même address), l'INT peut être
      // partagé entre tous les boutons → pas griser.
      const intSel = tr.querySelector('.pcf-int');
      const myAddrSel = tr.querySelector('.pcf-addr');
      const myAddr = myAddrSel && myAddrSel.value;
      if (intSel) {
        Array.from(intSel.options).forEach(opt => {
          if (!opt.value) return;
          const g = parseInt(opt.value, 10);
          let label = `GPIO ${g}`;
          let disabled = false;
          if (blacklist.has(g)) {
            const reasons = blacklistReasons[String(g)] || [];
            label += reasons.length ? ` (${reasons.join('+')})` : ' (système)';
            disabled = true;
          } else if (busyGPIOs.has(g)) { label += ' (utilisé en sortie)'; disabled = true; }
          else {
            // Vérifier si utilisé par un GPIO direct ailleurs (toujours conflit)
            // ou par un PCF d'address différente (conflit)
            let conflictNames = [];
            rows.forEach(otherTr => {
              if (otherTr === tr) return;
              const otherType = otherTr.dataset.sourceType;
              const otherName = otherTr.querySelectorAll('td')[0].querySelector('input').value.trim() || '?';
              if (otherType === 'gpio') {
                const sel = otherTr.querySelector('.gpio-select');
                if (sel && parseInt(sel.value, 10) === g) conflictNames.push(otherName);
              } else if (otherType === 'pcf8574') {
                const oInt = otherTr.querySelector('.pcf-int');
                const oAddr = otherTr.querySelector('.pcf-addr');
                if (oInt && parseInt(oInt.value, 10) === g
                    && oAddr && oAddr.value !== myAddr) {
                  conflictNames.push(otherName + ' [autre PCF]');
                }
              }
            });
            if (conflictNames.length > 0) {
              label += ` (utilisé par ${conflictNames.join(', ')})`;
              disabled = true;
            }
          }
          opt.textContent = label;
          opt.disabled = disabled;
        });
      }
      // Pin P0-P7 : grise ceux pris sur la MÊME address
      const pinSel = tr.querySelector('.pcf-pin');
      if (pinSel && myAddr) {
        Array.from(pinSel.options).forEach(opt => {
          const p = parseInt(opt.value, 10);
          const key = `${myAddr}:P${p}`;
          const taker = usedPcfPin[key];
          if (taker && taker !== myName) {
            opt.textContent = `P${p} (utilisé par ${taker})`;
            opt.disabled = true;
          } else {
            opt.textContent = `P${p}`;
            opt.disabled = false;
          }
        });
      }
    }
  });
}

/**
 * Parcourt toutes les lignes, repère les actor IDs utilisés par 2+ boutons,
 * et met à jour les labels des <option> dans chaque <select> d'actor pour
 * indiquer "(déjà utilisé)" sur les options correspondantes.
 * Non bloquant : l'utilisateur peut quand même choisir le même actor pour
 * 2 boutons (cas légitime : un bouton ON + un bouton OFF sur le même actor).
 */
function refreshActorAvailability() {
  const rows = document.querySelectorAll('#rows tr');
  // Compter qui utilise quoi
  const usage = {};  // actor_id → [row_name, row_name, ...]
  rows.forEach(tr => {
    const tds = tr.querySelectorAll('td');
    const name = tds[0].querySelector('input').value.trim() || '?';
    // tds[6] = Actor avec la nouvelle structure (Nom, Type, Source, Pull, Edge, Deb, Actor, Action, Notif, Del)
    const sel = tds[6] && tds[6].querySelector('select');
    const actor = sel ? sel.value : '';
    if (actor) {
      usage[actor] = usage[actor] || [];
      usage[actor].push(name);
    }
  });
  // Maintenant pour chaque ligne, met à jour le label des options
  rows.forEach(tr => {
    const tds = tr.querySelectorAll('td');
    const sel = tds[6] && tds[6].querySelector('select');
    if (!sel) return;
    Array.from(sel.options).forEach(opt => {
      if (!opt.value) return;
      const actorObj = actors.find(a => a.id === opt.value);
      if (!actorObj) return;
      const myName = tds[0].querySelector('input').value.trim() || '?';
      const others = (usage[opt.value] || []).filter(n => n !== myName);
      if (others.length > 0) {
        opt.textContent = `${actorObj.name} (déjà utilisé par ${others.join(', ')})`;
        opt.style.color = '#c80';
      } else {
        opt.textContent = actorObj.name;
        opt.style.color = '';
      }
    });
  });
}

function readRows() {
  const out = [];
  document.querySelectorAll('#rows tr').forEach(tr => {
    const tds = tr.querySelectorAll('td');
    const name = tds[0].querySelector('input').value.trim();
    const type = tr.dataset.sourceType || 'gpio';
    const deb = parseInt(tds[5].querySelector('input').value, 10);
    const actor = tds[6].querySelector('select').value;
    const doVal = tds[7].querySelector('select').value;
    const notify = tds[8].querySelector('input').checked;

    if (!name || !actor) return;

    let source;
    if (type === 'gpio') {
      const pinRaw = tr.querySelector('.gpio-select').value;
      const pull = tds[3].querySelector('select').value;
      const edge = tds[4].querySelector('select').value;
      if (!pinRaw) return;  // ligne incomplète
      source = {type: 'gpio', pin: parseInt(pinRaw, 10), pull, edge};
    } else if (type === 'pcf8574') {
      const addr = tr.querySelector('.pcf-addr').value;
      const intRaw = tr.querySelector('.pcf-int').value;
      const pinRaw = tr.querySelector('.pcf-pin').value;
      if (!addr || !intRaw || pinRaw === '' || pinRaw == null) return;
      source = {
        type: 'pcf8574',
        address: addr,
        int_gpio: parseInt(intRaw, 10),
        pin: parseInt(pinRaw, 10),
      };
    } else {
      return;
    }

    out.push({
      name,
      source,
      debounce_ms: deb,
      action: {actor, do: doVal},
      notify,
    });
  });
  return out;
}

async function save() {
  const buttons = readRows();
  try {
    const r = await fetch('/inputcontrol/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({buttons}),
    });
    const data = await r.json();
    if (r.ok) {
      alert(`✓ Sauvegardé. ${data.loaded} bouton(s) actif(s). ` +
            (data.errors && data.errors.length ? data.errors.length + ' erreur(s) — voir État.' : ''));
      await loadStatus();
    } else {
      alert('✗ ' + (data.error || r.status));
    }
  } catch (e) {
    alert('✗ Réseau : ' + e.message);
  }
}

async function loadAll() {
  await loadActors();
  await loadStatus();
  await loadConfig();
  // Alerte si pas d'actors trouvés
  const alert = document.getElementById('alert');
  if (actors.length === 0) {
    alert.style.display = 'block';
    alert.textContent = "⚠️ Aucun actor trouvé sur ce CBPi. Configure d'abord des actors (Hardware → Actor) avant d'ajouter des boutons ici.";
  } else {
    alert.style.display = 'none';
  }
}

// Auto-refresh du status toutes les 2 secondes pour voir le press_count monter en live
let _lastPressCounts = {};
async function refreshStatusOnly() {
  try {
    const s = await fetchJSON('/inputcontrol/status');
    updateStatusDisplay(s);
    // Flash visuel sur les boutons qui viennent d'être pressés
    (s.sources || []).forEach(src => {
      const prev = _lastPressCounts[src.name] || 0;
      if (src.press_count > prev) {
        flashRowByName(src.name);
      }
      _lastPressCounts[src.name] = src.press_count;
    });
  } catch (e) { /* silencieux pour éviter spam console */ }
}

function flashRowByName(name) {
  document.querySelectorAll('#rows tr').forEach(tr => {
    const inp = tr.querySelector('td:first-child input');
    if (inp && inp.value === name) {
      tr.classList.add('flash-press');
      setTimeout(() => tr.classList.remove('flash-press'), 400);
    }
  });
}

loadAll();
setInterval(refreshStatusOnly, 2000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Le plugin
# --------------------------------------------------------------------------- #
class InputControl(CBPiExtension):

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.sources = []
        self.mux_registry: dict = {}  # PCF8574 muxers : (addr, int_gpio, bus) → mux
        self.last_load_errors = []
        # Détection des bus système actifs au boot (figée pour la durée de
        # vie du plugin ; un changement de config.txt → reboot CBPi pour
        # re-détecter, c'est rare et acceptable).
        self.active_buses = _detect_active_buses()
        self.gpio_blacklist = _build_dynamic_blacklist(self.active_buses)
        # Enregistrement officiel CBPi (lit @request_mapping sur les méthodes
        # et monte un sub-app aiohttp à /inputcontrol/*). Doit être fait
        # synchrone dans __init__, avant le freeze du router.
        self.cbpi.register(self, url_prefix="/inputcontrol")
        self._task = asyncio.create_task(self.run())

    async def run(self):
        ui_url = f"http://{_get_local_ip()}:8000/inputcontrol/ui"
        bus_summary = ", ".join(
            f"{name}={'on' if on else 'off'}"
            for name, on in self.active_buses.items()
        )
        logger.warning("=" * 60)
        logger.warning("🎛 cbpi4-InputControl V%s — coucou je suis bien là", _PLUGIN_VERSION)
        logger.warning("    Backend GPIO  : %s  (running on Pi: %s)",
                       _GPIO_BACKEND, _ON_PI)
        logger.warning("    Bus système   : %s", bus_summary)
        logger.warning("    GPIO bloqués  : %s",
                       sorted(self.gpio_blacklist))
        logger.warning("    UI de config  : %s", ui_url)
        logger.warning("=" * 60)
        await self._ensure_config_exists(ui_url)
        await self.reload_from_config()

    async def _ensure_config_exists(self, ui_url):
        """
        Crée le setting global s'il n'existe pas, sinon force la mise à jour de
        sa description (description évolue d'une version du plugin à l'autre).
        On s'appuie sur le fait que config.add est idempotent : si la clé
        existe déjà, il met juste à jour les métadonnées (description, etc.)
        sans toucher à la valeur.
        """
        desc = (
            f"⚠️ Édition via l'UI dédiée : {ui_url}  — "
            "Le contenu de ce champ est un JSON brut généré par l'UI ; "
            "édition à la main à tes risques. "
            "POST /inputcontrol/reload pour appliquer."
        )
        current = self.cbpi.config.get(CONFIG_KEY, None)
        try:
            await self.cbpi.config.add(
                CONFIG_KEY,
                current if current is not None else DEFAULT_CONFIG_JSON,
                type=ConfigType.STRING,
                description=desc,
                source="cbpi4-InputControl",
            )
            if current is None:
                logger.warning("InputControl : setting '%s' créé", CONFIG_KEY)
        except Exception as e:
            logger.error("Création/MAJ setting KO: %s", e)

    def _busy_output_gpios(self):
        busy = set()
        try:
            for actor in self.cbpi.actor.data:
                if getattr(actor, "type", None) == "GPIOActor":
                    props = getattr(actor, "props", {}) or {}
                    g = props.get("GPIO") if isinstance(props, dict) else None
                    if isinstance(g, int):
                        busy.add(g)
        except Exception as e:
            logger.warning("Liste actors output KO: %s", e)
        return busy

    async def reload_from_config(self):
        await self._stop_all_sources()
        raw = self.cbpi.config.get(CONFIG_KEY, DEFAULT_CONFIG_JSON)
        try:
            buttons = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(buttons, list):
                raise ValueError("doit être une liste JSON")
        except Exception as e:
            err = f"JSON invalide : {e}"
            logger.error(err)
            self.last_load_errors = [err]
            return {"loaded": 0, "errors": self.last_load_errors}

        loop = asyncio.get_event_loop()
        errors = []
        new_sources = []
        used_pins = set()              # GPIO pins direct utilisés
        used_int_gpios = set()         # GPIO utilisés comme INT pour PCF8574
        used_pcf_pins = set()          # (address, pin) déjà alloués
        busy_output = self._busy_output_gpios()
        new_mux_registry: dict = {}    # (address, int_gpio, bus) → mux

        for idx, btn in enumerate(buttons):
            try:
                self._validate_button(btn, used_pins, busy_output,
                                      used_int_gpios, used_pcf_pins)
            except ValueError as e:
                err = f"#{idx} ({btn.get('name','?')}) ignoré : {e}"
                logger.error(err)
                errors.append(err)
                continue
            source = build_source(btn, self._handle_press, loop, new_mux_registry)
            if source is None:
                errors.append(f"#{idx} : build_source a échoué")
                continue
            try:
                await source.start(loop)
                new_sources.append(source)
            except Exception as e:
                err = f"#{idx} ({btn.get('name','?')}) start KO: {e}"
                logger.error(err)
                errors.append(err)

        # Maintenant que toutes les PCF8574InputSource ont enregistré leurs
        # pins auprès des multiplexers, on démarre les multiplexers (ouvre
        # I2C, arme INT GPIO). Un mux qui échoue invalide tous ses boutons.
        for key, mux in list(new_mux_registry.items()):
            try:
                await mux.start(loop)
            except Exception as e:
                err = f"PCF8574 @0x{mux.address:02x} INT=GPIO{mux.int_gpio} start KO: {e}"
                logger.error(err)
                errors.append(err)
                # Retire les sources qui dépendaient de ce mux
                new_sources = [s for s in new_sources
                               if not (isinstance(s, PCF8574InputSource)
                                       and s.address == mux.address
                                       and s.int_gpio == mux.int_gpio
                                       and s.i2c_bus == mux.i2c_bus)]
                new_mux_registry.pop(key, None)

        self.sources = new_sources
        self.mux_registry = new_mux_registry
        self.last_load_errors = errors
        if errors:
            logger.warning("InputControl reload : %d source(s) ✓  %d erreur(s) ✗",
                           len(self.sources), len(errors))
            for err in errors:
                logger.warning("  ✗ %s", err)
        else:
            logger.warning("InputControl reload : %d source(s) ✓  aucune erreur",
                           len(self.sources))
        return {"loaded": len(self.sources), "errors": errors}

    def _validate_button(self, btn, used_pins, busy_output,
                         used_int_gpios=None, used_pcf_pins=None):
        if used_int_gpios is None:
            used_int_gpios = set()
        if used_pcf_pins is None:
            used_pcf_pins = set()
        if not isinstance(btn, dict):
            raise ValueError("doit être un objet JSON")
        name = btn.get("name")
        if not name or not isinstance(name, str):
            raise ValueError("'name' manquant")
        src = btn.get("source")
        if not isinstance(src, dict):
            raise ValueError("'source' manquant")
        src_type = src.get("type")
        if src_type not in SOURCE_CLASSES:
            raise ValueError(f"source.type='{src_type}' non supporté")
        if src_type == "gpio":
            pin = src.get("pin")
            if not isinstance(pin, int):
                raise ValueError("source.pin doit être un entier")
            if pin in self.gpio_blacklist:
                bus_reason = []
                for bus, pins in _BUS_PINS.items():
                    if pin in pins and self.active_buses.get(bus):
                        bus_reason.append(bus)
                why = f"bus {','.join(bus_reason)}" if bus_reason else "système"
                raise ValueError(f"GPIO {pin} réservé ({why})")
            if pin in busy_output:
                raise ValueError(f"GPIO {pin} déjà utilisé par un actor output")
            if pin in used_pins:
                raise ValueError(f"GPIO {pin} déjà utilisé par un autre bouton")
            if pin in used_int_gpios:
                raise ValueError(f"GPIO {pin} déjà utilisé comme INT PCF8574")
            used_pins.add(pin)
        elif src_type == "pcf8574":
            # I2C must be active
            if not self.active_buses.get("i2c"):
                raise ValueError(
                    "I2C non activé sur ce Pi — "
                    "ajoute 'dtparam=i2c_arm=on' dans /boot/firmware/config.txt"
                )
            # address
            addr_raw = src.get("address")
            if addr_raw is None:
                raise ValueError("source.address manquant")
            try:
                addr = int(addr_raw, 16) if (isinstance(addr_raw, str)
                                             and addr_raw.startswith("0x")) \
                    else int(addr_raw)
            except (ValueError, TypeError):
                raise ValueError(f"source.address '{addr_raw}' invalide")
            if addr not in PCF8574Multiplexer.VALID_ADDRESSES:
                raise ValueError(
                    f"address 0x{addr:02x} hors plage PCF8574 "
                    f"(0x20-0x27 ou PCF8574A 0x38-0x3F)"
                )
            # int_gpio
            int_gpio = src.get("int_gpio")
            if not isinstance(int_gpio, int):
                raise ValueError("source.int_gpio doit être un entier")
            if int_gpio in self.gpio_blacklist:
                raise ValueError(f"GPIO {int_gpio} (INT) réservé système")
            if int_gpio in busy_output:
                raise ValueError(f"GPIO {int_gpio} (INT) utilisé par actor output")
            if int_gpio in used_pins:
                raise ValueError(f"GPIO {int_gpio} (INT) déjà utilisé comme bouton GPIO")
            used_int_gpios.add(int_gpio)
            # pin P0-P7
            pin = src.get("pin")
            if not isinstance(pin, int) or not (0 <= pin <= 7):
                raise ValueError("source.pin doit être un entier 0-7 (P0-P7)")
            key = (addr, pin)
            if key in used_pcf_pins:
                raise ValueError(
                    f"PCF8574 @0x{addr:02x} P{pin} déjà utilisé par un autre bouton"
                )
            used_pcf_pins.add(key)
        action = btn.get("action")
        if not isinstance(action, dict):
            raise ValueError("'action' manquant")
        if not action.get("actor"):
            raise ValueError("action.actor manquant")
        if action.get("do") not in ("on", "off", "toggle"):
            raise ValueError(f"action.do invalide")

    async def _handle_press(self, button_config):
        name = button_config.get("name", "<unnamed>")
        action = button_config.get("action", {})
        actor_ref = action.get("actor")
        do = action.get("do", "toggle")
        notify = bool(button_config.get("notify", False))
        actor_id = self._resolve_actor_id(actor_ref)
        if actor_id is None:
            logger.error("[%s] actor '%s' introuvable", name, actor_ref)
            return
        # Résolution nom lisible (pour notif + log). Fallback sur actor_ref
        # (qui peut être un id ou déjà un nom selon la config user).
        actor_name = self._get_actor_name(actor_id) or actor_ref
        try:
            if do == "on":
                await self.cbpi.actor.on(actor_id)
            elif do == "off":
                await self.cbpi.actor.off(actor_id)
            elif do == "toggle":
                # NB : cbpi.actor.toogle() est buggé pour beaucoup d'actor
                # types (la méthode instance.toggle() n'est pas implémentée
                # sur CBPiActor de base, ni sur MQTTActor, etc). L'exception
                # est avalée silencieusement → no-op.
                # On fait le toggle nous-même en lisant l'état courant.
                current_state = self._get_actor_state(actor_id)
                if current_state:
                    await self.cbpi.actor.off(actor_id)
                else:
                    await self.cbpi.actor.on(actor_id)
                logger.info("[%s] toggle (état %s → %s) sur '%s' OK",
                            name, current_state, not current_state, actor_name)
            else:
                logger.info("[%s] %s sur '%s' OK", name, do, actor_name)
            if notify:
                try:
                    self.cbpi.notify("InputControl",
                                     f"Bouton '{name}' → {do} sur '{actor_name}'")
                except Exception:
                    pass
        except Exception as e:
            logger.error("[%s] %s sur '%s' KO: %s", name, do, actor_name, e)

    def _get_actor_name(self, actor_id) -> Optional[str]:
        """Retourne le nom lisible d'un actor par son id, ou None si introuvable."""
        try:
            item = self.cbpi.actor.find_by_id(actor_id)
            if item is None:
                return None
            n = getattr(item, "name", None)
            if n:
                return n
            if isinstance(item, dict):
                return item.get("name")
        except Exception:
            pass
        return None

    def _get_actor_state(self, actor_id) -> bool:
        """
        Lit l'état (on/off) d'un actor par son id.

        Note CBPi : le dataclass Actor a un attribut `state` mais qui n'est
        PAS mis à jour automatiquement (reste à False par défaut). Le vrai
        état est sur `item.instance.state`. C'est ce que fait `to_dict()` :
        il appelle `self.instance.get_state()` à chaque sérialisation.
        """
        try:
            item = self.cbpi.actor.find_by_id(actor_id)
            if item is None:
                return False
            # Cas standard : Actor dataclass avec .instance
            instance = getattr(item, "instance", None)
            if instance is not None:
                # Préférer get_state() qui est l'API publique, fallback sur .state
                if hasattr(instance, "get_state"):
                    state_dict = instance.get_state()
                    if isinstance(state_dict, dict) and "state" in state_dict:
                        return bool(state_dict["state"])
                if hasattr(instance, "state"):
                    return bool(instance.state)
            # Cas dict (versions plus anciennes ou exotiques de CBPi)
            if isinstance(item, dict):
                if "instance" in item and item["instance"] is not None:
                    return bool(getattr(item["instance"], "state", False))
                return bool(item.get("state", False))
            # Fallback : .state direct sur l'item
            return bool(getattr(item, "state", False))
        except Exception as e:
            logger.warning("Lecture état actor '%s' KO: %s", actor_id, e)
            return False

    def _resolve_actor_id(self, ref):
        if not ref:
            return None
        try:
            if self.cbpi.actor.find_by_id(ref) is not None:
                return ref
        except Exception:
            pass
        try:
            for actor in self.cbpi.actor.data:
                if getattr(actor, "name", None) == ref:
                    return actor.id
        except Exception:
            pass
        return None

    async def _stop_all_sources(self):
        # Sources d'abord, mux ensuite (les sources se désenregistrent juste,
        # le mux ferme l'I2C et libère le GPIO INT)
        for s in self.sources:
            try:
                await s.stop()
            except Exception as e:
                logger.warning("Stop %s KO: %s", s.name, e)
        self.sources = []
        for key, mux in list(self.mux_registry.items()):
            try:
                await mux.stop()
            except Exception as e:
                logger.warning("Stop mux @0x%02x KO: %s", mux.address, e)
        self.mux_registry = {}

    # --------------------------- Routes HTTP --------------------------- #

    @request_mapping(path="/", method="GET", auth_required=False)
    async def http_root(self, request):
        """Redirige / vers /ui pour confort utilisateur."""
        raise web.HTTPFound(location="/inputcontrol/ui")

    @request_mapping(path="/ui", method="GET", auth_required=False)
    async def http_ui(self, request):
        return web.Response(text=UI_HTML, content_type="text/html")

    @request_mapping(path="/status", method="GET", auth_required=False)
    async def http_status(self, request):
        return web.json_response({
            "active_sources": len(self.sources),
            "sources": [s.describe() for s in self.sources],
            "pcf8574_muxers": [m.describe() for m in self.mux_registry.values()],
            "last_load_errors": self.last_load_errors,
            "running_on_pi": _ON_PI,
            "gpio_backend": _GPIO_BACKEND,
            "active_buses": self.active_buses,
            "blacklist": sorted(self.gpio_blacklist),
            "blacklist_reasons": self._blacklist_with_reasons(),
            "busy_output_gpios": sorted(self._busy_output_gpios()),
        })

    def _blacklist_with_reasons(self):
        """Pour l'UI : pour chaque pin bloquée, dit pourquoi."""
        reasons = {}
        for pin in sorted(self.gpio_blacklist):
            why = []
            if pin in GPIO_ALWAYS_BLACKLISTED:
                why.append("eeprom-hat")
            for bus, pins in _BUS_PINS.items():
                if pin in pins and self.active_buses.get(bus):
                    why.append(bus)
            reasons[str(pin)] = why
        return reasons

    @request_mapping(path="/config", method="GET", auth_required=False)
    async def http_get_config(self, request):
        raw = self.cbpi.config.get(CONFIG_KEY, DEFAULT_CONFIG_JSON)
        try:
            buttons = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            buttons = []
        return web.json_response({"buttons": buttons})

    @request_mapping(path="/config", method="POST", auth_required=False)
    async def http_set_config(self, request):
        try:
            payload = await request.json()
        except Exception as e:
            return web.json_response({"error": f"JSON invalide : {e}"}, status=400)
        buttons = payload.get("buttons")
        if not isinstance(buttons, list):
            return web.json_response({"error": "payload.buttons doit être une liste"}, status=400)
        logger.warning("InputControl : sauvegarde de %d bouton(s) via UI", len(buttons))
        try:
            new_raw = json.dumps(buttons, ensure_ascii=False)
            await self.cbpi.config.set(CONFIG_KEY, new_raw)
        except Exception as e:
            return web.json_response({"error": f"Sauvegarde KO: {e}"}, status=500)
        result = await self.reload_from_config()
        return web.json_response(result)

    @request_mapping(path="/reload", method="POST", auth_required=False)
    async def http_reload(self, request):
        logger.warning("InputControl : reload demandé via HTTP")
        result = await self.reload_from_config()
        return web.json_response(result)


def setup(cbpi):
    cbpi.plugin.register("InputControl", InputControl)
