// Tradebot-Vergleich – Scriptable-Widget (iPhone/iPad)
//
// Einrichtung:
//   1. Passwort aus dem Container holen:  docker compose exec -T auth printenv API_TOKEN
//      (bzw. den Wert aus .env – beides ist identisch)
//      (das Passwort ist gleichzeitig der API-Token)
//   2. Unten bei PASSWORD eintragen.
//   3. Datei in Scriptable öffnen (oder als iCloud-Datei importieren).
//   4. Home-/Sperrbildschirm -> Widget hinzufügen -> Scriptable -> Script wählen.
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
const BG = new Color("#12151c");

function fmtMoney(v) {
  if (v === null || v === undefined) return "–";
  return "$" + Number(v).toLocaleString("de-DE", { maximumFractionDigits: 0 });
}

function fmtPct(v) {
  if (v === null || v === undefined) return "–";
  const n = Number(v);
  return (n > 0 ? "+" : "") + n.toFixed(2) + " %";
}

function fmtAge(min) {
  if (min === null || min === undefined) return "";
  if (min < 60) return `vor ${min} min`;
  const h = Math.floor(min / 60);
  if (h < 48) return `vor ${h} h`;
  return `vor ${Math.floor(h / 24)} d`;
}

async function loadSummary() {
  const req = new Request(`${API_BASE}/api/summary`);
  req.headers = { Authorization: `Bearer ${PASSWORD}` };
  req.timeoutInterval = 20;
  const data = await req.loadJSON();
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

function addRow(parent, bot, compact) {
  const row = parent.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();
  row.spacing = 6;

  const name = row.addText(bot.label);
  name.font = Font.semiboldSystemFont(compact ? 11 : 12);
  name.textColor = Color.white();
  name.lineLimit = 1;

  row.addSpacer();

  const ret = row.addText(bot.has_data ? fmtPct(bot.return_pct) : "–");
  ret.font = Font.boldMonospacedSystemFont(compact ? 12 : 13);
  ret.textColor = bot.has_data ? (bot.return_pct >= 0 ? GREEN : RED) : MUTED;
  return row;
}

function buildWidget(data, family) {
  const w = new ListWidget();
  w.backgroundColor = BG;
  w.setPadding(12, 14, 12, 14);
  w.url = `${API_BASE}/trade/`;

  const head = w.addStack();
  head.layoutHorizontally();
  head.centerAlignContent();

  const title = head.addText("Tradingbots");
  title.font = Font.boldSystemFont(family === "small" ? 11 : 13);
  title.textColor = Color.white();

  head.addSpacer();

  const bots = (data && data.bots ? data.bots : []).filter((b) => b.has_data);
  const stamp = head.addText(bots.length ? fmtAge(Math.min(...bots.map((b) => b.age_minutes ?? 0))) : "–");
  stamp.font = Font.systemFont(9);
  stamp.textColor = MUTED;

  w.addSpacer(6);

  if (family === "small") {
    const leader = bots.find((b) => b.bot === data.leader) || bots[0];
    if (leader) {
      const name = w.addText(leader.label);
      name.font = Font.semiboldSystemFont(12);
      name.textColor = Color.white();
      name.lineLimit = 1;

      const val = w.addText(fmtPct(leader.return_pct));
      val.font = Font.boldMonospacedSystemFont(22);
      val.textColor = leader.return_pct >= 0 ? GREEN : RED;

      const eq = w.addText(fmtMoney(leader.equity));
      eq.font = Font.systemFont(10);
      eq.textColor = MUTED;
    } else {
      const none = w.addText("Noch keine Daten");
      none.font = Font.systemFont(11);
      none.textColor = MUTED;
    }
  } else {
    for (const bot of bots) {
      addRow(w, bot, family === "medium");
      w.addSpacer(4);
    }

    if (family === "large") {
      w.addSpacer(4);
      const line = w.addText("");
      line.font = Font.systemFont(1);
      for (const bot of bots) {
        const d = w.addText(
          `${bot.label}: ${fmtMoney(bot.equity)} · DD ${fmtPct(bot.max_drawdown_pct)} · ${bot.trades} Trades`
        );
        d.font = Font.systemFont(9);
        d.textColor = MUTED;
        d.lineLimit = 1;
      }
    }
  }

  if (data && data.generated_at) {
    w.addSpacer();
    const foot = w.addText("Paper Trading · keine Anlageberatung");
    foot.font = Font.systemFont(8);
    foot.textColor = MUTED;
  }

  return w;
}

let data = null;
let fromCache = false;
try {
  data = await loadSummary();
} catch (e) {
  data = loadCache();
  fromCache = true;
}

if (!data) {
  const err = new ListWidget();
  err.backgroundColor = BG;
  const t = err.addText("Keine Verbindung");
  t.font = Font.boldSystemFont(12);
  t.textColor = RED;
  const hint = err.addText(e ? String(e.message || e) : "API nicht erreichbar");
  hint.font = Font.systemFont(9);
  hint.textColor = MUTED;
  Script.setWidget(err);
} else {
  const widget = buildWidget(data, config.widgetFamily || "medium");
  if (fromCache) {
    const c = widget.addText("Offline – letzter Stand");
    c.font = Font.systemFont(8);
    c.textColor = RED;
  }
  Script.setWidget(widget);
}

Script.complete();
