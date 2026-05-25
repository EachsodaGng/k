import { useEffect, useState } from "react";
import axios from "axios";
import "@/App.css";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

function Stat({ label, value, accent }) {
  return (
    <div
      data-testid={`stat-${label.toLowerCase().replace(/\s+/g, "-")}`}
      className="border border-neutral-800 rounded-md px-5 py-4 bg-neutral-950"
    >
      <div className="text-[11px] uppercase tracking-[0.18em] text-neutral-500">
        {label}
      </div>
      <div
        className={`mt-2 font-mono text-2xl ${accent ? "text-emerald-400" : "text-neutral-100"}`}
      >
        {value}
      </div>
    </div>
  );
}

export default function App() {
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState(null);

  const load = async () => {
    try {
      const r = await axios.get(`${API}/status`);
      setStatus(r.data);
      setErr(null);
    } catch (e) {
      setErr(e?.message || "Failed to load status");
    }
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  return (
    <div
      data-testid="app-root"
      className="min-h-screen bg-neutral-950 text-neutral-100 font-mono"
    >
      <div className="max-w-3xl mx-auto px-6 py-16">
        <div className="flex items-center gap-3 mb-2">
          <div className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
          <span className="text-[11px] uppercase tracking-[0.22em] text-neutral-500">
            roblox · artist · monitor
          </span>
        </div>
        <h1
          data-testid="page-title"
          className="text-4xl sm:text-5xl font-semibold tracking-tight text-neutral-100 mt-3"
          style={{ fontFamily: "ui-serif, Georgia, serif" }}
        >
          Discord bot — live.
        </h1>
        <p className="mt-4 text-sm text-neutral-400 leading-relaxed max-w-xl">
          Add Roblox Creator Store artists with{" "}
          <code className="text-neutral-200">/add</code>; the bot drops every
          existing audio into your chosen channel with thumbnail, OGG download,
          waveform image and EBU&nbsp;R128 LUFS. New uploads from monitored
          artists are auto-posted within seconds.
        </p>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-10">
          <Stat
            label="Bot"
            value={status?.bot?.ready ? "READY" : "BOOTING"}
            accent={status?.bot?.ready}
          />
          <Stat label="Guilds" value={status?.bot?.guilds ?? "—"} />
          <Stat label="Monitors" value={status?.monitors_total ?? "—"} />
          <Stat label="Processed" value={status?.processed_total ?? "—"} />
        </div>

        <div className="mt-10 border-t border-neutral-900 pt-8">
          <div className="text-[11px] uppercase tracking-[0.22em] text-neutral-500 mb-4">
            commands
          </div>
          <ul className="space-y-3 text-sm text-neutral-300">
            <li>
              <code className="text-emerald-400">/add</code>{" "}
              <span className="text-neutral-500">
                artist:&lt;name&gt; channel:#audio
              </span>{" "}
              — start monitoring + post existing.
            </li>
            <li>
              <code className="text-emerald-400">/list</code>{" "}
              <span className="text-neutral-500">
                — show every artist tracked in this server.
              </span>
            </li>
            <li>
              <code className="text-emerald-400">/remove</code>{" "}
              <span className="text-neutral-500">artist:&lt;name&gt;</span> —
              stop tracking.
            </li>
            <li>
              <code className="text-emerald-400">/status</code>{" "}
              <span className="text-neutral-500">— quick health view.</span>
            </li>
          </ul>
        </div>

        <div className="mt-10 text-[11px] text-neutral-600">
          {status?.bot?.user && (
            <>
              connected as{" "}
              <span className="text-neutral-400">{status.bot.user}</span> ·
              polling every {status.bot.poll_interval_seconds}s
            </>
          )}
          {err && <span className="text-red-400">api error: {err}</span>}
        </div>
      </div>
    </div>
  );
}
