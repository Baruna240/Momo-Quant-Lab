"""Agent Smart Money dan Strategy Research (+ Strategy Challenger)."""
import json
import time

from agents.base import Agent


class SmartMoneyAgent(Agent):
    """Tanggung jawab: memantau dompet sniper/trader yang dikunci untuk forward test copy-trade (hipotesis H1).
    Keluaran: {leaders: [{wallet, picked_h, roi, pnl_sol, trips, style}], profiled}. Metode: wallet_lab.db (hasil
    discover/profile/pick). Tidak memanggil RPC di siklus rutin; uji forward dijalankan terpisah (wallet_lab.py forward)."""
    name = "smart_money"
    role = "Dompet leader untuk forward test copy-trade"
    reads = ("wallet_lab.db: leaders, profiles",)

    def act(self, state):
        db = self.ro("wallet_lab.db")
        now = time.time()
        leaders = []
        has = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "leaders" in has:
            for r in db.execute("SELECT wallet, picked, stats FROM leaders"):
                st = json.loads(r["stats"] or "{}")
                leaders.append({"wallet": r["wallet"], "picked_h": round((now - r["picked"]) / 3600, 1), "roi": st.get("roi"),
                                "pnl_sol": st.get("pnl_sol"), "trips": st.get("n_closed"), "style": st.get("style")})
        profiled = db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] if "profiles" in has else 0
        db.close()
        warn = [] if leaders else ["belum ada leader dikunci (python wallet_lab.py pick)"]
        oldest = max((x["picked_h"] for x in leaders), default=0)
        return ({"leaders": leaders, "profiled": profiled}, f"{len(leaders)} leader · dipantau {oldest:.0f} jam · {profiled} dompet diprofil", warn)


class ResearchAgent(Agent):
    """Tanggung jawab: rapor hipotesis dari Research Engine + siklus Champion vs Challenger.
    Masukan: state['challenge'] = True untuk menjalankan Challenger (berat, cukup sekali sehari).
    Keluaran: {hypotheses: [{key, name, passed, stages, verdict, oos_pf}], champion, trials_total, t_need, challenge}.
    Metode: research.latest (hasil terakhir) dan research.challenge (pilih challenger dari in-sample saja, uji di semua
    tahap, catat di buku percobaan). Kebijakan: rekomendasi hanya untuk strategi yang lolos SEMUA tahap."""
    name = "research"
    role = "Rapor hipotesis + Champion vs Challenger"
    reads = ("research.db: runs, trials", "alt_history.db (saat Challenger)")

    def act(self, state):
        import research
        rows, ts = research.latest(self.base / "research.db")
        if not rows:
            raise FileNotFoundError("belum ada hasil riset (python research.py)")
        hyps = [{"key": r["key"], "name": r["name"], "passed": r["passed"], "stages": len(r["stages"]), "verdict": r["verdict"],
                 "oos_pf": (r.get("oos") or {}).get("pf"), "params": r["params"]} for r in rows]
        champ = max(hyps, key=lambda h: (h["passed"], h["oos_pf"] or 0))
        db = research.ledger()
        trials = research.total_trials(db)
        db.close()
        out = {"hypotheses": hyps, "champion": champ, "trials_total": trials, "t_need": research.t_needed(trials),
               "ran_at": ts, "challenge": None}
        warn = []
        if state.get("challenge"):
            from alt_data import connect
            env = research.build_context(connect())
            res = research.challenge(env, champ["key"], champ)
            out["challenge"] = res
            out["trials_total"], out["t_need"] = res.get("trials_total", trials), res.get("t_need", out["t_need"])
            if res["promoted"]:
                warn.append(f"challenger {res['champion']['params']} lolos semua tahap -> calon paper trading")
        ready = [h["key"] for h in hyps if h["passed"] == h["stages"]]
        summ = (f"{len(ready)} hipotesis siap paper trading" if ready else "belum ada hipotesis lolos semua tahap") + \
               f" · juara: {champ['key']} ({champ['passed']}/{champ['stages']} tahap) · syarat t ≥ {out['t_need']:.2f}"
        return out, summ, warn
