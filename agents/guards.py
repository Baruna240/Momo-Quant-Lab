"""Agent penjaga: Safety, Market Regime, dan Risk & Portfolio (hak veto)."""
import json
import time

from agents.base import Agent


class SafetyAgent(Agent):
    """Tanggung jawab: skor risiko berlapis untuk kandidat meme + rapor seberapa akurat skor itu terhadap label rug.
    Masukan: kandidat dari meme_scanner. Keluaran: {scored: [{mint, symbol, score, reasons, partial}], evaluation:
    {labeled, buckets: [{lo, hi, n, bad}]}}. Metode: safety.assess (lapis pembuat & peluncuran; lapis aliran bila ada
    di flow.db) dan perbandingan skor detik-60 dengan label 20 menit."""
    name = "safety"
    role = "Skor risiko rug berlapis + rapor akurasinya"
    reads = ("meme_scanner (state)", "flow.db: token_flow")

    def act(self, state):
        from safety import assess
        cands = self.upstream(state, "meme_scanner").get("candidates", [])
        scored = []
        for c in cands:
            score, why = assess({}, c, None)
            scored.append({"mint": c["mint"], "symbol": c["symbol"], "score": score, "reasons": why, "partial": True})
        warn, ev = [], {"labeled": 0, "buckets": []}
        try:
            db = self.ro("flow.db")
            rows = [dict(r) for r in db.execute("SELECT label, safety60 FROM token_flow WHERE finalized IS NOT NULL")]
            db.close()
            ev["labeled"] = len(rows)
            for lo, hi in ((0, 20), (20, 40), (40, 60), (60, 101)):
                b = [r for r in rows if r["safety60"] is not None and lo <= r["safety60"] < hi]
                ev["buckets"].append({"lo": lo, "hi": min(hi, 100), "n": len(b), "bad": sum(r["label"] in ("rug", "dump") for r in b)})
        except FileNotFoundError:
            warn.append("flow.db belum ada: akurasi skor belum bisa dinilai")
        if ev["labeled"] < 300:
            warn.append(f"skor belum boleh dipakai sebagai filter ({ev['labeled']}/300 token berlabel)")
        risky = sum(s["score"] >= 40 for s in scored)
        return ({"scored": scored, "evaluation": ev}, f"{len(scored)} kandidat dinilai · {risky} berisiko · {ev['labeled']} token berlabel", warn)


class MarketRegimeAgent(Agent):
    """Tanggung jawab: kondisi pasar BTC sebagai saklar strategi.
    Keluaran: {btc_1d, btc_4h: {direction, volatility, adx, ...}, stance}. stance: 'trend' (boleh trend following),
    'reversi' (sideways: hanya beli murah jangka pendek), 'diam' (bear: spot tidak membuka posisi).
    Metode: indicators.regime pada candle BTC harian & 4 jam (sama dengan logika hipotesis H8)."""
    name = "market_regime"
    role = "Kondisi pasar BTC sebagai saklar strategi"
    reads = ("alt_history.db: ohlcv BTC",)

    def act(self, state):
        from alt_data import load
        import indicators as ind
        db = self.ro("alt_history.db")
        out = {}
        for tf in ("1d", "4h"):
            d = load("BTC", tf, db, 1200)
            if d is None:
                raise ValueError(f"data BTC {tf} belum ada (jalankan python alt_data.py)")
            out[f"btc_{tf}"] = ind.regime(d["c"], d["h"], d["l"], tf=tf)
            out[f"btc_{tf}"]["close"] = float(d["c"][-1])
            out[f"btc_{tf}"]["age_h"] = round((time.time() - d["t"][-1]) / 3600, 1)
        db.close()
        day = out["btc_1d"]["direction"]
        out["stance"] = {"trend naik": "trend", "sideways": "reversi", "trend turun": "diam"}[day]
        warn = [f"data harian BTC berumur {out['btc_1d']['age_h']} jam (perbarui: python alt_data.py)"] if out["btc_1d"]["age_h"] > 50 else []
        return out, f"BTC harian {day} · 4 jam {out['btc_4h']['direction']} · sikap: {out['stance']}", warn


class RiskAgent(Agent):
    """Tanggung jawab: gerbang risiko deterministik dengan HAK VETO; tidak bisa ditimpa agent / AI mana pun.
    Masukan: state['proposals'] (opsional) = [{strategy, key, cost, slippage, liquidity}].
    Keluaran: {mode, kill, events24, breaches: {strategi: [alasan]}, decisions: [{..., allowed, blocked_by, shadow}]}.
    Metode: risk.RiskManager dipulihkan dari riwayat posisi meme; batas = RiskLimits (beku)."""
    name = "risk"
    role = "Gerbang risiko deterministik (veto)"
    reads = ("simulasi.db: positions, risk_events", "KILL_SWITCH")

    def act(self, state):
        from config import load_settings
        from risk import KILL_FILE, RiskLimits, RiskManager
        s = load_settings()
        db = self.ro("simulasi.db")
        closed = db.execute("SELECT strategy, closed, cost_usd, pnl_usd FROM positions WHERE closed IS NOT NULL ORDER BY closed").fetchall()
        has_ev = db.execute("SELECT 1 FROM sqlite_master WHERE name='risk_events'").fetchone()
        ev = db.execute("SELECT COUNT(*), COALESCE(SUM(action='blokir'),0) FROM risk_events WHERE ts >= ?",
                        (time.time() - 86400,)).fetchone() if has_ev else (0, 0)
        db.close()
        rm = RiskManager(RiskLimits(), mode=s.risk_mode)
        rm.load_history([tuple(r) for r in closed])
        breaches = {}
        for strat in rm.books:
            d = rm.check_entry(strat, "__cek__", 0.0)
            reasons = [r for r in d.blocked_by + d.shadow if not r.startswith("order ganda")]
            if reasons:
                breaches[strat] = reasons
        decisions = []
        for p in state.get("proposals", []):
            d = rm.check_entry(p["strategy"], p["key"], p.get("cost", 0.0), p.get("slippage", 0.0), p.get("liquidity"))
            decisions.append({**p, "allowed": d.allowed, "blocked_by": d.blocked_by, "shadow": d.shadow})
        kill = KILL_FILE.exists()
        warn = ["KILL SWITCH AKTIF: semua entry baru diblokir"] if kill else []
        return ({"mode": s.risk_mode, "kill": kill, "events24": ev[0], "blocked24": ev[1], "breaches": breaches,
                 "limits": json.loads(json.dumps(RiskLimits().__dict__)), "decisions": decisions},
                f"mode {s.risk_mode} · {len(breaches)} strategi melewati batas · {ev[1]} blokir/24j", warn)
