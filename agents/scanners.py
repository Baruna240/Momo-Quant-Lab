"""Agent pemindai: Meme Scanner dan Altcoin Scanner (+ Technical Analysis)."""
import time

from agents.base import Agent


class MemeScannerAgent(Agent):
    """Tanggung jawab: melaporkan token pump.fun baru yang sedang diamati simulator dan kandidat terkuatnya.
    Masukan: - . Keluaran: {alive, tokens_seen, recorded_1h, candidates: [{mint, symbol, age_min, m60, s60, max_mult,
    dev_sol, dev_pct, creator_n24, name_n1h, tw}]}. Metode: membaca token_features (jejak harga & ciri saat lahir)."""
    name = "meme_scanner"
    role = "Token pump.fun baru & kandidat terkuat"
    reads = ("simulasi.db: stats, token_features",)
    TOP = 10

    def act(self, state):
        db = self.ro("simulasi.db")
        now = time.time()
        stats = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM stats")}
        alive = bool(stats.get("heartbeat") and now - stats["heartbeat"] < 60)
        rows = [dict(r) for r in db.execute(
            """SELECT mint, symbol, created, m60, s60, max_mult, dev_sol, dev_pct, creator_n24, name_n1h, tw
               FROM token_features WHERE created >= ? ORDER BY created DESC""", (now - 3600,))]
        db.close()
        cands = sorted((r for r in rows if r["m60"] is not None), key=lambda r: -(r["m60"] or 0))[:self.TOP]
        for r in cands:
            r["age_min"] = round((now - r.pop("created")) / 60, 1)
        warn = [] if alive else ["simulator meme tidak aktif (heartbeat > 60 dtk)"]
        return ({"alive": alive, "tokens_seen": int(stats.get("tokens_seen", 0)), "recorded_1h": len(rows), "candidates": cands},
                f"{len(rows)} token direkam/jam · {len(cands)} kandidat", warn)


class AltcoinScannerAgent(Agent):
    """Tanggung jawab: analisis teknikal multi-timeframe semua altcoin yang dipantau.
    Keluaran: {coins: [{base, mtf, trend{1h,4h,1d}, regime4h, rsi1h, adx4h, rvol1h, btc_corr, breakout4h}], aligned_up,
    aligned_down}. Metode: indicators.analyze pada candle alt_history.db (tanpa melihat masa depan)."""
    name = "altcoin_scanner"
    role = "Analisis teknikal multi-timeframe altcoin"
    reads = ("alt_history.db: ohlcv",)

    def act(self, state):
        from indicators import analyze
        db = self.ro("alt_history.db")
        bases = [r[0].split("/")[0] for r in db.execute("SELECT DISTINCT symbol FROM ohlcv WHERE tf='1d'")]
        coins, failed = [], []
        for b in bases:
            try:
                a = analyze(b, db)
            except Exception as e:      # satu koin bermasalah tidak menghentikan pemindaian
                failed.append(f"{b}: {type(e).__name__}")
                continue
            if not a:
                continue
            t = a["tf"]
            coins.append({"base": b, "mtf": a["mtf_align"], "trend": {tf: t[tf]["trend"] for tf in t},
                          "regime4h": t.get("4h", {}).get("regime"), "rsi1h": t.get("1h", {}).get("rsi"),
                          "adx4h": t.get("4h", {}).get("adx"), "rvol1h": t.get("1h", {}).get("rvol"),
                          "btc_corr": t.get("1d", {}).get("btc_corr"), "breakout4h": t.get("4h", {}).get("breakout")})
        db.close()
        up = sum(c["mtf"] == "naik" for c in coins)
        down = sum(c["mtf"] == "turun" for c in coins)
        last = time.time()
        return ({"coins": coins, "aligned_up": up, "aligned_down": down, "at": last},
                f"{len(coins)} koin · selaras naik {up} · selaras turun {down}", failed)
