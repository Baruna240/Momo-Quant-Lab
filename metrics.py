"""Metrik standar strategi: satu tempat perhitungan untuk leaderboard, research engine, dan dashboard.

Masukan: daftar transaksi selesai (dict) dengan kunci
    closed (waktu tutup), pnl (USD bersih), cost (modal USD), fees (USD), slip (USD, biaya slippage)
Semua angka dihitung dari PnL BERSIH (sudah dipotong fee, slippage, biaya jaringan, funding).

Catatan penting:
- Sharpe & Sortino dihitung PER TRANSAKSI (rata-rata return / simpangan), bukan disetahunkan:
  data kita masih hitungan hari, menyetahunkan akan memberi angka besar yang menyesatkan.
- t-stat expectancy: > 2 baru bisa dianggap "kemungkinan bukan kebetulan".
- Out-of-sample: 70% transaksi terlama vs 30% terbaru (strategi yang sungguhan harus untung di keduanya).
- Walk-forward: transaksi dibagi 5 periode berurutan; stabilitas = berapa periode yang expectancy-nya positif.
"""
import math

OOS_SPLIT = 0.7
WF_FOLDS = 5
MIN_TRADES_VERDICT = 100   # di bawah ini hasilnya belum bisa disimpulkan


def _pf(pnls):
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    if gl == 0:
        return None if gw == 0 else math.inf
    return gw / gl


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def max_drawdown(pnls):
    """Penurunan terdalam kurva PnL kumulatif dari puncaknya (USD, angka negatif atau 0)."""
    cum = peak = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def summarize(trades):
    """Ringkasan lengkap satu strategi. trades boleh dalam urutan apa pun."""
    tr = sorted(trades, key=lambda t: t["closed"])
    n = len(tr)
    if n == 0:
        return {"n": 0}
    pnls = [t["pnl"] for t in tr]
    rets = [t["pnl"] / t["cost"] for t in tr if t.get("cost")]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win, avg_loss = _mean(wins), _mean(losses)
    sd = _std(rets)
    down = [r for r in rets if r < 0]
    down_dev = math.sqrt(sum(r * r for r in down) / len(rets)) if rets and down else None
    exp = _mean(pnls)
    sd_pnl = _std(pnls)
    out = {
        "n": n,
        "win_rate": len(wins) / n,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "avg_rr": (avg_win / -avg_loss) if avg_win and avg_loss and avg_loss < 0 else None,
        "profit_factor": _pf(pnls),
        "net": sum(pnls),
        "expectancy": exp,
        "expectancy_pct": _mean(rets),
        "t_stat": (exp / (sd_pnl / math.sqrt(n))) if sd_pnl else None,
        "max_dd": max_drawdown(pnls),
        "sharpe": (_mean(rets) / sd) if sd else None,
        "sortino": (_mean(rets) / down_dev) if down_dev else None,
        "fees": sum(t.get("fees") or 0 for t in tr),
        "slippage": sum(t.get("slip") or 0 for t in tr),
        "volume": sum(t.get("cost") or 0 for t in tr),
        "first": tr[0]["closed"], "last": tr[-1]["closed"],
    }
    # out-of-sample: 30% transaksi terbaru tidak boleh dipakai untuk memilih strategi
    cut = int(n * OOS_SPLIT)
    ins, oos = pnls[:cut], pnls[cut:]
    out["is_pf"], out["oos_pf"] = _pf(ins), _pf(oos)
    out["is_exp"], out["oos_exp"] = _mean(ins), _mean(oos)
    out["oos_n"] = len(oos)
    # walk-forward: apakah hasilnya konsisten dari periode ke periode?
    k = min(WF_FOLDS, n)
    folds = [pnls[i * n // k:(i + 1) * n // k] for i in range(k)]
    out["wf_exp"] = [_mean(f) for f in folds if f]
    out["wf_pos"] = sum(e > 0 for e in out["wf_exp"])
    out["wf_folds"] = len(out["wf_exp"])
    out["verdict"] = verdict(out)
    return out


def verdict(m):
    """Label jujur untuk leaderboard. Tidak pernah memilih berdasarkan win rate saja."""
    if m["n"] < MIN_TRADES_VERDICT:
        return "data kurang"
    pf, opf = m["profit_factor"], m["oos_pf"]
    if pf is None or pf < 1:
        return "rugi"
    if opf is None or opf < 1:
        return "rugi di out-of-sample"
    if m["wf_pos"] < m["wf_folds"] - 1:
        return "tidak stabil"
    if (m["t_stat"] or 0) < 2:
        return "untung, belum signifikan"
    return "kandidat"


# ---------------------------------------------------------------- sumber data
def meme_trades(conn, settings):
    """Posisi meme selesai + perkiraan fee & slippage dari tabel trades (dalam USD).
    Beli: fee platform + biaya jaringan + slippage (harga masuk = harga pasar x (1 + slip)).
    Jual: slippage & fee dihitung dari nilai pasar token yang dijual."""
    costs = {}
    for r in conn.execute("SELECT strategy, batch, mint, side, price_sol, qty, sol, usd, slip FROM trades"):
        key = (r["strategy"], r["batch"], r["mint"])
        c = costs.setdefault(key, [0.0, 0.0, None])
        slip = r["slip"] or 0.0
        if r["side"] == "BELI":
            sol_usd = r["usd"] / r["sol"] if r["sol"] else 0.0
            c[2] = sol_usd
            net = r["sol"] * (1 - settings.fee_pct) - settings.tx_cost_sol
            c[0] += r["usd"] * settings.fee_pct + settings.tx_cost_sol * sol_usd
            c[1] += net * slip / (1 + slip) * sol_usd
        else:
            sol_usd = r["usd"] / r["sol"] if r["sol"] else (c[2] or 0.0)
            gross = r["qty"] * r["price_sol"] * sol_usd
            c[0] += gross * (1 - slip) * settings.fee_pct + settings.tx_cost_sol * sol_usd
            c[1] += gross * slip
    out = {}
    for r in conn.execute("SELECT strategy, batch, mint, opened, closed, cost_usd, pnl_usd FROM positions"
                          " WHERE closed IS NOT NULL"):
        fees, slip, _ = costs.get((r["strategy"], r["batch"], r["mint"]), (0.0, 0.0, None))
        out.setdefault(r["strategy"], []).append({"opened": r["opened"], "closed": r["closed"], "pnl": r["pnl_usd"],
                                                  "cost": r["cost_usd"], "fees": fees, "slip": slip})
    return out


def alt_trades(conn, settings, big=()):
    """Posisi altcoin/futures selesai. Fee & slippage adalah PERKIRAAN dari pengaturan (tidak disimpan per transaksi):
    spot dihitung dari modal & hasil jual; futures dari nilai kontrak (margin x leverage) dua sisi."""
    out = {}
    for r in conn.execute("SELECT strategy, symbol, opened, closed, cost, proceeds, pnl, lev FROM alt_positions"
                          " WHERE closed IS NOT NULL"):
        slip = settings.slip_big if r["symbol"] in big else settings.slip_small
        lev = r["lev"] or 1
        if lev > 1:
            notional = r["cost"] * lev
            fees, sl = notional * settings.fut_fee_pct * 2, notional * slip * 2
        else:
            fees = (r["cost"] + r["proceeds"]) * settings.fee_pct
            sl = (r["cost"] + r["proceeds"]) * slip
        out.setdefault(r["strategy"], []).append({"opened": r["opened"], "closed": r["closed"], "pnl": r["pnl"],
                                                  "cost": r["cost"], "fees": fees, "slip": sl})
    return out


def leaderboard(groups, limits=None):
    """{nama: [transaksi]} -> daftar baris leaderboard, diurutkan dari expectancy tertinggi.
    limits (RiskLimits): tambahkan hasil "seandainya batas rugi harian & drawdown ditegakkan"."""
    rows = []
    for name, tr in groups.items():
        row = {"strategy": name, **summarize(tr)}
        if limits is not None and tr:
            from risk import replay
            kept, blocked = replay(tr, limits)
            row["risk_net"], row["risk_blocked"] = sum(t["pnl"] for t in kept), blocked
        rows.append(row)
    return sorted(rows, key=lambda r: (r["n"] > 0, r.get("expectancy") or -1e9), reverse=True)
