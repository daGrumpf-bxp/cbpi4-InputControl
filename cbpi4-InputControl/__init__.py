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
GPIO_BLACKLIST_HARDCODED = {0, 1, 2, 3, 4}
_PLUGIN_VERSION = "0.0.5"


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
    Bookworm. gpiozero gère l'anti-rebond et le pull-up/down nativement,
    et appelle `when_pressed` / `when_released` dans son thread interne.
    """

    def __init__(self, button_config, on_press_callback, loop):
        super().__init__(button_config, on_press_callback)
        src = button_config.get("source", {})
        self.pin = int(src.get("pin"))
        self.pull = src.get("pull", "up").lower()
        self.edge = src.get("edge", "falling").lower()
        self.debounce_ms = int(src.get("debounce_ms", 300))
        self._loop = loop
        self._button: Optional["Button"] = None
        self._armed = False
        self._press_count = 0
        self._last_press_ts: Optional[float] = None

    async def start(self, loop):
        if Button is None:
            raise RuntimeError("gpiozero indisponible — impossible d'armer GPIO%d" % self.pin)

        # Mapping pull
        # gpiozero : pull_up=True/False/None
        # None nécessite active_state explicite (rare ; pull externe)
        if self.pull == "up":
            pull_up = True
            active_state = None
        elif self.pull == "down":
            pull_up = False
            active_state = None
        else:  # "none"
            pull_up = None
            active_state = True  # par défaut on considère 1 = "pressé"

        # gpiozero bounce_time est en SECONDES (None = pas d'anti-rebond)
        bounce_s = (self.debounce_ms / 1000.0) if self.debounce_ms > 0 else None

        try:
            self._button = Button(
                self.pin,
                pull_up=pull_up,
                active_state=active_state,
                bounce_time=bounce_s,
            )

            # Mapping edge → quel événement on écoute
            # En logique gpiozero :
            #  - pull_up=True  : pressed quand pin → LOW  (falling edge)
            #  - pull_up=False : pressed quand pin → HIGH (rising edge)
            # Donc "falling" = when_pressed si pull_up, when_released si pull_down
            # Pour rester intuitif, on mappe directement :
            if self.edge == "falling":
                # front descendant = bouton qui tire à GND → on attache à pressed
                # (avec pull_up) ou à released (avec pull_down)
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
                "[%s] GPIO%d armé (gpiozero) pull=%s edge=%s deb=%dms",
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
                # close() détache les callbacks et libère la pin proprement.
                # On ne set PAS when_pressed=None (gpiozero émet un warning).
                self._button.close()
                logger.info("[%s] GPIO%d désarmé", self.name, self.pin)
        except Exception as e:
            logger.warning("[%s] close() KO: %s", self.name, e)
        finally:
            self._button = None
            self._armed = False

    def _on_event(self):
        """Callback appelé par gpiozero dans son thread interne."""
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
            "armed": self._armed, "press_count": self._press_count,
            "last_press_ts": self._last_press_ts,
            "backend": _GPIO_BACKEND,
        }


SOURCE_CLASSES = {"gpio": GPIOInputSource}


def build_source(button_config, on_press_callback, loop):
    src_type = button_config.get("source", {}).get("type")
    cls = SOURCE_CLASSES.get(src_type)
    if cls is None:
        return None
    try:
        return cls(button_config, on_press_callback, loop)
    except Exception as e:
        logger.error("Build source %s KO: %s", src_type, e)
        return None


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
  table { border-collapse: collapse; width: 100%; margin: 1em 0; }
  th, td { border: 1px solid #ddd; padding: 6px 8px; text-align: left;
           vertical-align: middle; }
  th { background: #f5f5f5; font-weight: 600; }
  input[type=text], input[type=number], select {
    width: 100%; padding: 4px 6px; box-sizing: border-box;
    border: 1px solid #ccc; border-radius: 3px; font-size: 14px;
  }
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
      <th>GPIO</th>
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

async function loadStatus() {
  const s = await fetchJSON('/inputcontrol/status');
  blacklist = new Set(s.blacklist || []);
  (s.busy_output_gpios || []).forEach(g => busyGPIOs.add(g));

  updateStatusDisplay(s);

  const blPart = [...blacklist].sort((a,b)=>a-b).join(', ');
  const busyPart = [...busyGPIOs].sort((a,b)=>a-b).join(', ') || 'aucun';
  document.getElementById('bl-info').innerHTML =
    `<b>GPIO système (réservés)</b> : ${blPart} &nbsp; · &nbsp; ` +
    `<b>GPIO utilisés par actors output</b> : ${busyPart} &nbsp; · &nbsp; ` +
    `<b>Backend GPIO</b> : ${s.gpio_backend || '?'}`;
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
      lines.push(`  • ${src.name}  pin=${src.pin}  armé=${src.armed}  presses=${src.press_count}  dernière=${ts}`);
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
    source: {type: 'gpio', pin: null, pull: 'up', edge: 'falling', debounce_ms: 300},
    action: {actor: '', do: 'toggle'},
    notify: true,
  };
  const tr = document.createElement('tr');

  function td(child) { const t = document.createElement('td'); t.appendChild(child); tr.appendChild(t); return t; }

  const inpName = document.createElement('input');
  inpName.type = 'text';
  inpName.value = b.name || '';
  inpName.placeholder = 'ex: stop_pompe';
  td(inpName);

  const selGpio = document.createElement('select');
  selGpio.appendChild(new Option('— choisir —', ''));
  allGPIOs.forEach(g => {
    let label = `GPIO ${g}`;
    let disabled = false;
    if (blacklist.has(g)) { label += ' (système)'; disabled = true; }
    else if (busyGPIOs.has(g)) { label += ' (utilisé en sortie)'; disabled = true; }
    const opt = new Option(label, g);
    if (disabled) opt.disabled = true;
    if (b.source && b.source.pin === g) opt.selected = true;
    selGpio.appendChild(opt);
  });
  td(selGpio);

  const selPull = document.createElement('select');
  ['up','down','none'].forEach(v => {
    const o = new Option(v, v);
    if (b.source && b.source.pull === v) o.selected = true;
    selPull.appendChild(o);
  });
  td(selPull);

  const selEdge = document.createElement('select');
  ['falling','rising','both'].forEach(v => {
    const o = new Option(v, v);
    if (b.source && b.source.edge === v) o.selected = true;
    selEdge.appendChild(o);
  });
  td(selEdge);

  const inpDeb = document.createElement('input');
  inpDeb.type = 'number';
  inpDeb.min = 0;
  inpDeb.value = (b.source && b.source.debounce_ms != null) ? b.source.debounce_ms : 300;
  td(inpDeb);

  const selActor = document.createElement('select');
  selActor.appendChild(new Option('— choisir —', ''));
  actors.forEach(a => {
    const o = new Option(a.name, a.id);
    if (b.action && b.action.actor === a.id) o.selected = true;
    selActor.appendChild(o);
  });
  td(selActor);

  const selDo = document.createElement('select');
  ['on','off','toggle'].forEach(v => {
    const o = new Option(v, v);
    if (b.action && b.action.do === v) o.selected = true;
    selDo.appendChild(o);
  });
  td(selDo);

  const inpNotif = document.createElement('input');
  inpNotif.type = 'checkbox';
  inpNotif.checked = !!b.notify;
  const tdNotif = td(inpNotif);
  tdNotif.style.textAlign = 'center';

  const btnDel = document.createElement('button');
  btnDel.textContent = '✕';
  btnDel.className = 'danger';
  btnDel.onclick = () => tr.remove();
  td(btnDel);

  document.getElementById('rows').appendChild(tr);
}

function readRows() {
  const out = [];
  document.querySelectorAll('#rows tr').forEach(tr => {
    const tds = tr.querySelectorAll('td');
    const name = tds[0].querySelector('input').value.trim();
    const pinRaw = tds[1].querySelector('select').value;
    const pull = tds[2].querySelector('select').value;
    const edge = tds[3].querySelector('select').value;
    const deb = parseInt(tds[4].querySelector('input').value, 10);
    const actor = tds[5].querySelector('select').value;
    const doVal = tds[6].querySelector('select').value;
    const notify = tds[7].querySelector('input').checked;
    if (!name || !pinRaw || !actor) return;
    out.push({
      name,
      source: {type: 'gpio', pin: parseInt(pinRaw, 10), pull, edge, debounce_ms: deb},
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
        self.last_load_errors = []
        # Enregistrement officiel CBPi (lit @request_mapping sur les méthodes
        # et monte un sub-app aiohttp à /inputcontrol/*). Doit être fait
        # synchrone dans __init__, avant le freeze du router.
        self.cbpi.register(self, url_prefix="/inputcontrol")
        self._task = asyncio.create_task(self.run())

    async def run(self):
        ui_url = f"http://{_get_local_ip()}:8000/inputcontrol/ui"
        logger.warning("=" * 60)
        logger.warning("🎛 cbpi4-InputControl V%s — coucou je suis bien là", _PLUGIN_VERSION)
        logger.warning("    Backend GPIO : %s  (running on Pi: %s)",
                       _GPIO_BACKEND, _ON_PI)
        logger.warning("    UI de config : %s", ui_url)
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
        used_pins = set()
        busy_output = self._busy_output_gpios()

        for idx, btn in enumerate(buttons):
            try:
                self._validate_button(btn, used_pins, busy_output)
            except ValueError as e:
                err = f"#{idx} ({btn.get('name','?')}) ignoré : {e}"
                logger.error(err)
                errors.append(err)
                continue
            source = build_source(btn, self._handle_press, loop)
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

        self.sources = new_sources
        self.last_load_errors = errors
        # WARNING level → visible même sans -d 20, rare donc pas spam
        if errors:
            logger.warning("InputControl reload : %d source(s) ✓  %d erreur(s) ✗",
                           len(self.sources), len(errors))
            for err in errors:
                logger.warning("  ✗ %s", err)
        else:
            logger.warning("InputControl reload : %d source(s) ✓  aucune erreur",
                           len(self.sources))
        return {"loaded": len(self.sources), "errors": errors}

    def _validate_button(self, btn, used_pins, busy_output):
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
            if pin in GPIO_BLACKLIST_HARDCODED:
                raise ValueError(f"GPIO {pin} système")
            if pin in busy_output:
                raise ValueError(f"GPIO {pin} déjà utilisé par un actor output")
            if pin in used_pins:
                raise ValueError(f"GPIO {pin} déjà utilisé par un autre bouton")
            used_pins.add(pin)
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
        try:
            if do == "on":
                await self.cbpi.actor.on(actor_id)
            elif do == "off":
                await self.cbpi.actor.off(actor_id)
            elif do == "toggle":
                await self.cbpi.actor.toogle(actor_id)  # typo officielle cbpi
            logger.info("[%s] %s sur '%s' OK", name, do, actor_ref)
            if notify:
                try:
                    self.cbpi.notify("InputControl",
                                     f"Bouton '{name}' → {do} sur '{actor_ref}'")
                except Exception:
                    pass
        except Exception as e:
            logger.error("[%s] %s sur '%s' KO: %s", name, do, actor_ref, e)

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
        for s in self.sources:
            try:
                await s.stop()
            except Exception as e:
                logger.warning("Stop %s KO: %s", s.name, e)
        self.sources = []

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
            "last_load_errors": self.last_load_errors,
            "running_on_pi": _ON_PI,
            "gpio_backend": _GPIO_BACKEND,
            "blacklist": sorted(GPIO_BLACKLIST_HARDCODED),
            "busy_output_gpios": sorted(self._busy_output_gpios()),
        })

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
