// Tradebot-Vergleich – Scriptable-Widget (iPhone/iPad)
//
// Zeigt für alle 3 Bots die Equity-Kurve und die Änderung in % seit
// Anfang | Monat | Woche | Tag.
//
// Einrichtung:
//   1. Passwort aus dem Container holen:  docker compose exec -T auth printenv API_TOKEN
//      (bzw. den Wert aus .env – beides ist identisch; das Passwort ist zugleich der API-Token)
//   2. Unten bei PASSWORD eintragen.
//   3. Datei in Scriptable öffnen (oder als iCloud-Datei importieren).
//   4. Home-/Sperrbildschirm -> Widget hinzufügen -> Scriptable -> Script wählen.
//      Empfohlen: mittel, groß oder (iPad) extragroß. Klein zeigt eine Kompaktansicht.
//
// Das Script liest nur /api/summary. Bei Netzfehlern zeigt es den letzten
// erfolgreichen Stand aus dem lokalen Cache.

const API_BASE = "https://sioxd.duckdns.org";
const PASSWORD = "HIER_PASSWORT_EINTRAGEN";

const CACHE_FILE = "tradebot-summary.json";
const fm = FileManager.local();
const cachePath = fm.joinPath(fm.documentsDirectory(), CACHE_FILE);

const GREEN = new Color("#2ecc71");
const RED = new Color("#ff5c5c");
const MUTED = new Color("#9aa4b2");
const FAINT = new Color("#3a4252");
const BG = new Color("#12151c");
const CARD = new Color("#1a1f29");

const PERIODS = [
  ["start", "Anfang"],
  ["month", "Monat"],
  ["week", "Woche"],
  ["day", "Tag"],
];
const SHORT = { wsb: "WSB", truth: "Truth", armstrong: "Armstrong" };

// ---------- Formatierung ----------
function fmtMoney(v) {
  if (v === null || v === undefined) return "–";
  return "$" + Number(v).toLocaleString("de-DE", { maximumFractionDigits: 0 });
}

function fmtPct(v, digits = 2) {
  if (v === null || v === undefined || isNaN(v)) return "–";
  const n = Number(v);
  return (n > 0 ? "+" : "") + n.toFixed(digits).replace(".", ",") + "%";
}

function pctColor(v) {
  if (v === null || v === undefined || isNaN(v) || Math.abs(v) < 0.005) return MUTED;
  return v > 0 ? GREEN : RED;
}

function fmtAge(min) {
  if (min === null || min === undefined) return "";
  if (min < 2) return "gerade eben";
  if (min < 60) return `vor ${min} min`;
  const h = Math.floor(min / 60);
  if (h < 48) return `vor ${h} h`;
  return `vor ${Math.floor(h / 24)} d`;
}

// Fallback für ältere API ohne "changes"
function changesOf(bot) {
  return bot.changes || { start: bot.return_pct, month: null, week: null, day: null };
}

// ---------- Daten ----------
async function loadSummary() {
  const req = new Request(`${API_BASE}/api/summary`);
  req.headers = { Authorization: `Bearer ${PASSWORD}` };
  req.timeoutInterval = 25;
  const data = await req.loadJSON();
  if (req.response && req.response.statusCode && req.response.statusCode >= 400) {
    throw new Error(`HTTP ${req.response.statusCode}${data && data.detail ? ": " + data.detail : ""}`);
  }
  fm.writeString(cachePath, JSON.stringify(data));
  return data;
}

function loadCache() {
  if (!fm.fileExists(cachePath)) return null;
  try {
    return JSON.parse(fm.readString(cachePath));
  } catch (e) {
    return null;
  }
}

// ---------- Sparkline ----------
// points: [[unix_ts, pct_seit_start], ...]
function sparkline(points, width, height, color) {
  const ctx = new DrawContext();
  ctx.size = new Size(width, height);
  ctx.opaque = false;
  ctx.respectScreenScale = true;

  if (!points || points.length < 2) {
    ctx.setFillColor(FAINT);
    ctx.fillRect(new Rect(0, height / 2, width, 1));
    return ctx.getImage();
  }

  const pad = 2;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  // 0-Linie immer im Bild, damit man sieht, ob der Bot über/unter dem Start liegt
  let minY = Math.min(0, ...ys);
  let maxY = Math.max(0, ...ys);
  if (maxY - minY < 0.2) {
    const mid = (maxY + minY) / 2;
    minY = mid - 0.1;
    maxY = mid + 0.1;
  }
  const sx = (x) => pad + ((x - minX) / Math.max(1, maxX - minX)) * (width - 2 * pad);
  const sy = (y) => pad + (1 - (y - minY) / (maxY - minY)) * (height - 2 * pad);

  // Null-Linie (gestrichelt)
  const zeroY = sy(0);
  ctx.setFillColor(FAINT);
  for (let x = 0; x < width; x += 6) ctx.fillRect(new Rect(x, zeroY, 3, 1));

  // Fläche unter der Kurve
  const area = new Path();
  area.move(new Point(sx(xs[0]), zeroY));
  points.forEach((p) => area.addLine(new Point(sx(p[0]), sy(p[1]))));
  area.addLine(new Point(sx(xs[xs.length - 1]), zeroY));
  area.closeSubpath();
  ctx.addPath(area);
  ctx.setFillColor(new Color(color.hex, 0.18));
  ctx.fillPath();

  // Linie
  const line = new Path();
  line.move(new Point(sx(xs[0]), sy(ys[0])));
  points.slice(1).forEach((p) => line.addLine(new Point(sx(p[0]), sy(p[1]))));
  ctx.addPath(line);
  ctx.setStrokeColor(color);
  ctx.setLineWidth(1.6);
  ctx.strokePath();

  // Endpunkt
  const lx = sx(xs[xs.length - 1]);
  const ly = sy(ys[ys.length - 1]);
  ctx.setFillColor(color);
  ctx.fillEllipse(new Rect(lx - 2, ly - 2, 4, 4));

  return ctx.getImage();
}

function addSpark(parent, bot, width, height) {
  const ch = changesOf(bot);
  const img = parent.addImage(sparkline(bot.spark, width, height, ch.start >= 0 ? GREEN : RED));
  img.imageSize = new Size(width, height);
  return img;
}

// ---------- Bausteine ----------
function addText(parent, text, font, color, opts = {}) {
  const t = parent.addText(text);
  t.font = font;
  t.textColor = color;
  t.lineLimit = 1;
  t.minimumScaleFactor = opts.scale ?? 0.6;
  if (opts.right) t.rightAlignText();
  if (opts.center) t.centerAlignText();
  return t;
}

// Eine Zelle "Label / Wert" mit fester Breite
function addPctCell(parent, label, value, width, valueFont, digits) {
  const cell = parent.addStack();
  cell.layoutVertically();
  cell.size = new Size(width, 0);
  if (label) {
    const l = cell.addStack();
    l.addSpacer();
    addText(l, label, Font.systemFont(8), MUTED);
  }
  const v = cell.addStack();
  v.addSpacer();
  addText(v, fmtPct(value, digits), valueFont, pctColor(value));
  return cell;
}

function addHeader(w, data, family) {
  const head = w.addStack();
  head.layoutHorizontally();
  head.centerAlignContent();
  addText(head, "Tradingbots", Font.boldSystemFont(family === "small" ? 11 : 13), Color.white());
  head.addSpacer();
  const bots = (data && data.bots ? data.bots : []).filter((b) => b.has_data);
  const ages = bots.map((b) => b.age_minutes).filter((a) => a !== null && a !== undefined);
  addText(head, ages.length ? fmtAge(Math.min(...ages)) : "–", Font.systemFont(9), MUTED);
  return bots;
}

// ---------- Layouts ----------
// Klein: pro Bot Name + Mini-Kurve + Anfang (groß) / Tag (klein)
function layoutSmall(w, bots) {
  for (const bot of bots) {
    const ch = changesOf(bot);
    const row = w.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();
    row.spacing = 4;

    const name = row.addStack();
    name.size = new Size(38, 0);
    addText(name, SHORT[bot.bot] || bot.label, Font.semiboldSystemFont(10), Color.white());

    addSpark(row, bot, 40, 20);
    row.addSpacer();

    const vals = row.addStack();
    vals.layoutVertically();
    const a = vals.addStack();
    a.addSpacer();
    addText(a, fmtPct(ch.start, 1), Font.boldMonospacedSystemFont(11), pctColor(ch.start));
    const d = vals.addStack();
    d.addSpacer();
    addText(d, "T " + fmtPct(ch.day, 1), Font.monospacedSystemFont(8), pctColor(ch.day));
    w.addSpacer(5);
  }
}

// Mittel: Kopfzeile mit Zeitraum-Labels, pro Bot eine Zeile Name | Kurve | 4 Werte
function layoutMedium(w, bots) {
  const NAME_W = 58, SPARK_W = 70, SPARK_H = 24, CELL_W = 36;

  const hdr = w.addStack();
  hdr.layoutHorizontally();
  hdr.spacing = 4;
  hdr.addSpacer(NAME_W + SPARK_W + 4);
  for (const [, label] of PERIODS) {
    const c = hdr.addStack();
    c.size = new Size(CELL_W, 0);
    c.addSpacer();
    addText(c, label, Font.systemFont(8), MUTED);
  }
  w.addSpacer(2);

  for (const bot of bots) {
    const ch = changesOf(bot);
    const row = w.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();
    row.spacing = 4;

    const name = row.addStack();
    name.layoutVertically();
    name.size = new Size(NAME_W, 0);
    addText(name, SHORT[bot.bot] || bot.label, Font.semiboldSystemFont(11), Color.white());
    addText(name, fmtMoney(bot.equity), Font.systemFont(8), MUTED);

    addSpark(row, bot, SPARK_W, SPARK_H);

    for (const [key] of PERIODS) {
      addPctCell(row, null, ch[key], CELL_W, Font.semiboldMonospacedSystemFont(9), 1);
    }
    w.addSpacer(4);
  }
}

// Groß: pro Bot eine Karte mit Name/Equity, breiter Kurve und 4 Werten darunter
function layoutLarge(w, bots, width) {
  const inner = width - 28 - 16;
  for (const bot of bots) {
    const ch = changesOf(bot);
    const card = w.addStack();
    card.layoutVertically();
    card.backgroundColor = CARD;
    card.cornerRadius = 10;
    card.setPadding(5, 8, 5, 8);

    const top = card.addStack();
    top.layoutHorizontally();
    top.centerAlignContent();
    addText(top, bot.label, Font.semiboldSystemFont(12), Color.white());
    top.addSpacer();
    addText(top, `${fmtMoney(bot.equity)} · ${bot.trades ?? 0} Trades`, Font.systemFont(9), MUTED);

    card.addSpacer(2);
    addSpark(card, bot, inner, 28);
    card.addSpacer(2);

    const vals = card.addStack();
    vals.layoutHorizontally();
    const cellW = inner / PERIODS.length;
    for (const [key, label] of PERIODS) {
      addPctCell(vals, label, ch[key], cellW, Font.semiboldMonospacedSystemFont(11), 2);
    }
    w.addSpacer(5);
  }
}

// Extragroß (iPad): 3 Karten nebeneinander
function layoutExtraLarge(w, bots, width) {
  const row = w.addStack();
  row.layoutHorizontally();
  row.spacing = 10;
  const cardW = (width - 28 - 20) / Math.max(1, bots.length);
  const inner = cardW - 20;
  for (const bot of bots) {
    const ch = changesOf(bot);
    const card = row.addStack();
    card.layoutVertically();
    card.size = new Size(cardW, 0);
    card.backgroundColor = CARD;
    card.cornerRadius = 12;
    card.setPadding(10, 10, 10, 10);

    addText(card, bot.label, Font.semiboldSystemFont(14), Color.white());
    addText(card, `${fmtMoney(bot.equity)} · ${bot.trades ?? 0} Trades`, Font.systemFont(10), MUTED);
    card.addSpacer(6);
    addSpark(card, bot, inner, 110);
    card.addSpacer(8);

    for (const pair of [PERIODS.slice(0, 2), PERIODS.slice(2)]) {
      const r = card.addStack();
      r.layoutHorizontally();
      for (const [key, label] of pair) {
        addPctCell(r, label, ch[key], inner / 2, Font.semiboldMonospacedSystemFont(14), 2);
      }
      card.addSpacer(4);
    }
  }
}

function widgetWidth(family) {
  // ungefähre Breiten in Punkten (variieren je nach Gerät leicht)
  const wide = Device.isPad() ? 310 : Math.min(Device.screenSize().width - 55, 338);
  return { small: 158, medium: wide, large: wide, extraLarge: Device.isPad() ? 715 : wide }[family] || wide;
}

function buildWidget(data, family) {
  const w = new ListWidget();
  w.backgroundColor = BG;
  w.setPadding(12, 14, 12, 14);
  w.url = `${API_BASE}/trade/`;
  w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);

  const bots = addHeader(w, data, family);
  w.addSpacer(family === "small" ? 6 : 8);

  if (!bots.length) {
    addText(w, "Noch keine Daten", Font.systemFont(11), MUTED);
  } else if (family === "small") {
    layoutSmall(w, bots);
  } else if (family === "medium") {
    layoutMedium(w, bots);
  } else if (family === "extraLarge") {
    layoutExtraLarge(w, bots, widgetWidth(family));
  } else {
    layoutLarge(w, bots, widgetWidth(family));
  }

  w.addSpacer();
  if (family === "extraLarge") {
    addText(w, "Paper Trading · keine Anlageberatung", Font.systemFont(8), MUTED);
  }
  return w;
}

// ---------- Main ----------
let data = null;
let fromCache = false;
let loadError = null;
try {
  data = await loadSummary();
} catch (e) {
  loadError = e;
  data = loadCache();
  fromCache = true;
}

// In der App: Vorschau in der gewünschten Größe (zum Testen hier ändern)
const family = config.widgetFamily || "large";

if (!data) {
  const err = new ListWidget();
  err.backgroundColor = BG;
  addText(err, "Keine Verbindung", Font.boldSystemFont(12), RED);
  addText(err, loadError ? String(loadError.message || loadError) : "API nicht erreichbar", Font.systemFont(9), MUTED, { scale: 0.5 });
  Script.setWidget(err);
  if (!config.runsInWidget) await err.presentMedium();
} else {
  const widget = buildWidget(data, family);
  if (fromCache) {
    addText(widget, "Offline – letzter Stand", Font.systemFont(8), RED);
  }
  Script.setWidget(widget);
  if (!config.runsInWidget) {
    if (family === "small") await widget.presentSmall();
    else if (family === "medium") await widget.presentMedium();
    else if (family === "extraLarge") await widget.presentExtraLarge();
    else await widget.presentLarge();
  }
}

Script.complete();
